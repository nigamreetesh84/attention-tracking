"""The consent and capture gate (PLAN 2.5; CLAUDE.md rules 5 and 6).

`CaptureGate` is the single decision on whether frames of a worker may be
written. It is open only when, at decision time:

1. a worker session is open (`SessionManager.current()`); none: `no_worker`
   (risks A6: without a session consent cannot be established);
2. for a burst, that session already covered the burst's first frame
   (trigger or earliest frame); otherwise the frames show someone else or
   nobody: `no_worker`;
3. the roster in force grants the worker current consent (`has_consent`
   of `current()`, which re-reads the applied roster); otherwise
   `no_consent`. Q5: such a worker works normally, metadata only;
4. the station is not on break: no scheduled break of the bundle's shifts
   (site timezone) overlaps the span from the burst's first frame to now,
   and the optional engine signal does not report On Break. On break maps
   to `no_worker` (the wire enum has no `on_break`).

Any exception while deciding closes the gate (`no_consent`): failing safe
for privacy never touches the scan path, which does not wait on the gate.

Evidence goes to disk only through `GatedEvidenceSink` -> `EvidenceStore`
`save_burst`, which demands a `CapturePermit` minted here and asks the gate
again before every frame, so a logout or consent change mid-burst withholds
the whole burst.

Live view (contract 4.5): stills and video frames are never written to disk
by the workstation (they are sent from memory) but they are still images of
a worker, so the live path must ask `decide_live()` and send nothing while
it is closed. Decision recorded in the 2.5 summary.
"""

import logging
from collections.abc import Callable
from typing import Protocol

from ag_shared.api import ConfigBundle

from ag_workstation.auth.sessions import CurrentSession
from ag_workstation.clock import Clock
from ag_workstation.consent.breaks import on_break
from ag_workstation.consent.decision import GateDecision, GateReason
from ag_workstation.consent.permit import CapturePermit, _issue
from ag_workstation.engine_api import EvidenceBurst

logger = logging.getLogger(__name__)


class SessionSource(Protocol):
    """The open session with consent from the roster in force (`SessionManager`)."""

    def current(self) -> CurrentSession | None:
        """The open session, or None."""
        ...


class BundleSource(Protocol):
    """The applied config bundle (`RosterSource`): shifts, breaks and site timezone."""

    def current_bundle(self) -> ConfigBundle | None:
        """The bundle in force, or None before the first pull."""
        ...


class CaptureGate:
    """Decides whether frames may be written. Thread-safe; never raises."""

    def __init__(
        self,
        *,
        sessions: SessionSource,
        bundles: BundleSource,
        clock: Clock,
        engine_on_break: Callable[[], bool] | None = None,
    ) -> None:
        """`engine_on_break` reports the engine's On Break state; it must
        answer from memory (for example the last state the service saw),
        because the gate runs on the engine thread that delivers bursts."""
        self._sessions = sessions
        self._bundles = bundles
        self._clock = clock
        self._engine_on_break = engine_on_break

    def decide(self, burst: EvidenceBurst | None = None) -> GateDecision:
        """Decide now, for `burst` (its frames) or for a frame taken now (None)."""
        try:
            return self._decide(burst)
        except Exception as exc:
            # Type name only: messages could carry anything.
            logger.error("capture gate failed (%s); images withheld", type(exc).__name__)
            return GateDecision(GateReason.ERROR, None)

    def decide_live(self) -> GateDecision:
        """Whether a live still or video frame may be taken and sent now."""
        return self.decide(None)

    def permit(self, burst: EvidenceBurst) -> CapturePermit | GateDecision:
        """A permit to store `burst`, or the closed decision. Never raises."""
        decision = self.decide(burst)
        if not decision.open:
            return decision
        try:
            return _issue(burst, decision, lambda: self.decide(burst))
        except Exception as exc:
            logger.error("capture permit not issued (%s); images withheld", type(exc).__name__)
            return GateDecision(GateReason.ERROR, decision.decided_at)

    def _decide(self, burst: EvidenceBurst | None) -> GateDecision:
        now = self._clock.now()
        session = self._sessions.current()
        if session is None:
            return GateDecision(GateReason.NO_SESSION, now)
        start = now
        if burst is not None:
            start = min((burst.triggered_at, *(f.captured_at for f in burst.frames)))
            if session.started_at > start:
                return GateDecision(GateReason.SESSION_CHANGED, now)
        if session.has_consent is not True:
            return GateDecision(GateReason.NO_CONSENT, now)
        if self._engine_on_break is not None and self._engine_on_break():
            return GateDecision(GateReason.ON_BREAK, now)
        bundle = self._bundles.current_bundle()
        if bundle is not None and on_break(
            bundle.shifts, bundle.site.timezone, min(start, now), now
        ):
            return GateDecision(GateReason.ON_BREAK, now)
        return GateDecision(GateReason.OPEN, now, session_id=session.session_id)
