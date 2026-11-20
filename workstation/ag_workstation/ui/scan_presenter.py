"""Scan screen logic (PLAN 2.8; scope 3.1, 4.1, 4.4; risks A1, A12; contract 4.5).

`ScanPresenter` owns everything the scan screen shows and produces a
`ScanViewModel` on every `tick()` (called by the view's timer). It never
blocks the UI thread:

- A barcode goes to `ScanFlow.on_barcode` on the scan runner; the result
  comes back through an inbox drained by `tick()`. If the runner is gone the
  scan is recorded inline (it is bounded by the engine deadline) rather than
  lost (CLAUDE.md rule 1).
- State and health come from a `StatusPoller` (one poll in flight, on its
  own runner).
- Logout runs on the scan runner too.

"Monitoring unavailable" is shown when, for `unavailable_after_s`, there has
been no fresh poll answer, or the engine reports monitoring unavailable
(engine down, no camera online), or the state stayed Unknown. A shorter blip
(an engine restart) only greys the status indicator. Scanning is never
affected (rule 2).
"""

import logging
import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from ag_shared.i18n import TranslateFn, t
from ag_shared.states import AttentionState

from ag_workstation.auth import CurrentSession
from ag_workstation.clock import Clock
from ag_workstation.scan import ScanResult
from ag_workstation.ui.backends import SessionBackend
from ag_workstation.ui.runner import Runner
from ag_workstation.ui.status import StatusPoller, StatusReading

logger = logging.getLogger(__name__)


class ScanHandler(Protocol):
    """`ScanFlow.on_barcode`: records a scan, never raises, bounded time."""

    def on_barcode(self, barcode: str) -> ScanResult:
        """Record `barcode` and decide its outcome."""
        ...


class StatusLevel(StrEnum):
    """Colour of the big status indicator."""

    ATTENTIVE = "attentive"
    """Green."""
    CAUTION = "caution"
    """Amber: distracted or drowsy."""
    ALERT = "alert"
    """Red: sleeping or absent."""
    NEUTRAL = "neutral"
    """Grey: Unknown or On Break."""
    UNAVAILABLE = "unavailable"
    """Grey with the "Monitoring unavailable" banner."""


class BannerKind(StrEnum):
    """The warning banner after a scan."""

    NONE = "none"
    WARNING = "warning"
    ESCALATED = "escalated"


_LEVELS: Final = {
    AttentionState.ATTENTIVE: StatusLevel.ATTENTIVE,
    AttentionState.DISTRACTED: StatusLevel.CAUTION,
    AttentionState.DROWSY: StatusLevel.CAUTION,
    AttentionState.SLEEPING: StatusLevel.ALERT,
    AttentionState.ABSENT: StatusLevel.ALERT,
    AttentionState.ON_BREAK: StatusLevel.NEUTRAL,
    AttentionState.UNKNOWN: StatusLevel.NEUTRAL,
}

STATE_KEYS: Final = {
    AttentionState.ATTENTIVE: "ui.state.attentive",
    AttentionState.DISTRACTED: "ui.state.distracted",
    AttentionState.DROWSY: "ui.state.drowsy",
    AttentionState.SLEEPING: "ui.state.sleeping",
    AttentionState.ABSENT: "ui.state.absent",
    AttentionState.ON_BREAK: "ui.state.on_break",
    AttentionState.UNKNOWN: "ui.state.unknown",
}
"""Catalog key of each state's label."""

UNAVAILABLE_KEY: Final = "ui.state.unavailable"


@dataclass(frozen=True, slots=True)
class ScanPresenterConfig:
    """Scan screen timings (seconds)."""

    warn_banner_s: float = 4.0
    """How long the warning banner stays up."""
    escalated_banner_s: float = 8.0
    unavailable_after_s: float = 5.0
    """How long a problem must last before "Monitoring unavailable"."""
    poll_interval_s: float = 0.5

    def __post_init__(self) -> None:
        for name in ("warn_banner_s", "escalated_banner_s", "unavailable_after_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class ScanViewModel:
    """Everything the scan screen shows. Empty text means hidden."""

    status_level: StatusLevel
    status_text: str
    last_item_text: str
    message_text: str
    banner_kind: BannerKind
    banner_text: str
    sound_token: int
    """Increases once per warned scan; the view plays the sound when it changes."""
    monitoring_text: str
    """"Monitoring in progress" (worker logged in, monitoring up)."""
    unavailable_text: str
    """"Monitoring unavailable" banner."""
    live_view_text: str
    worker_text: str
    logged_in: bool
    """Show the logout action (else the login action)."""
    scans_pending: int
    """Scans handed to the scan flow whose result has not come back yet."""


class ScanPresenter:
    """Scan screen presenter. Call `tick()` from the UI thread only."""

    def __init__(
        self,
        *,
        flow: ScanHandler,
        poller: StatusPoller,
        sessions: SessionBackend,
        runner: Runner,
        clock: Clock,
        translate: TranslateFn = t,
        config: ScanPresenterConfig | None = None,
    ) -> None:
        self._flow = flow
        self._poller = poller
        self._sessions = sessions
        self._runner = runner
        self._clock = clock
        self._t = translate
        self._config = config or ScanPresenterConfig()
        self._inbox: deque[ScanResult | bool] = deque()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._last_barcode: str | None = None
        self._message = ""
        self._banner = BannerKind.NONE
        self._banner_until = 0.0
        self._sound_token = 0
        self._login_prompt = False
        self._live_view = False
        self._started_at = clock.monotonic()
        self._problem_since: float | None = None
        self._session: CurrentSession | None = None

    # -- inputs -------------------------------------------------------------

    def on_barcode(self, barcode: str) -> None:
        """A complete barcode from the scanner. Returns at once."""
        self._last_barcode = barcode
        with self._pending_lock:
            self._pending += 1
        try:
            self._runner.submit(lambda: self._record(barcode))
        except Exception as exc:
            # The scan must not be lost: record it here (bounded by the query deadline).
            logger.error("scan runner unavailable (%s); recording inline", type(exc).__name__)
            self._record(barcode)

    def _record(self, barcode: str) -> None:
        try:
            result = self._flow.on_barcode(barcode)
        except Exception as exc:
            logger.error("scan flow raised: %s", type(exc).__name__)
            with self._pending_lock:
                self._pending -= 1
            return
        self._inbox.append(result)

    def logout(self) -> None:
        """The worker's logout action."""
        try:
            self._runner.submit(self._logout)
        except Exception as exc:
            logger.error("logout not started: %s", type(exc).__name__)
            self._message = self._t("ui.logout_failed")

    def _logout(self) -> None:
        try:
            self._sessions.logout()
            ok = True
        except Exception as exc:
            logger.error("logout failed: %s", type(exc).__name__)
            ok = False
        self._inbox.append(ok)

    def set_live_view_active(self, active: bool) -> None:
        """Set by the service while live-view frames are being sent (contract 4.5)."""
        self._live_view = bool(active)

    def show_welcome(self, session: CurrentSession) -> None:
        """After a successful login."""
        self._session = session
        self._message = self._t("login.ok", name=session.display_name)

    def take_login_prompt(self) -> bool:
        """True once after an unattributed scan (A1): the app shows the login screen."""
        prompt, self._login_prompt = self._login_prompt, False
        return prompt

    @property
    def logged_in(self) -> bool:
        """A worker session was open at the last tick."""
        return self._session is not None

    # -- tick ------------------------------------------------------------------

    def tick(self) -> ScanViewModel:
        """Apply finished work and time, and return what to show. Never blocks."""
        now = self._clock.monotonic()
        self._session = self._current_session()
        while self._inbox:
            item = self._inbox.popleft()
            if isinstance(item, ScanResult):
                self._apply_scan(item, now)
            else:
                self._message = self._t("ui.logged_out" if item else "ui.logout_failed")
        if self._banner is not BannerKind.NONE and now >= self._banner_until:
            self._banner = BannerKind.NONE
        reading = self._poller.poll()
        return self._view(reading, now)

    def _current_session(self) -> CurrentSession | None:
        try:
            return self._sessions.current()
        except Exception as exc:
            logger.error("session lookup failed: %s", type(exc).__name__)
            return None

    def _apply_scan(self, result: ScanResult, now: float) -> None:
        with self._pending_lock:
            self._pending -= 1
        self._last_barcode = result.barcode
        self._message = self._t(result.message_key.value)
        if result.warn:
            escalated = result.escalated
            self._banner = BannerKind.ESCALATED if escalated else BannerKind.WARNING
            hold = self._config.escalated_banner_s if escalated else self._config.warn_banner_s
            self._banner_until = now + hold
            self._sound_token += 1
        else:
            self._banner = BannerKind.NONE
        if result.prompt_login and self._session is None:
            self._login_prompt = True

    def _view(self, reading: StatusReading | None, now: float) -> ScanViewModel:
        state, unavailable = self._monitoring(reading, now)
        if unavailable:
            level, status = StatusLevel.UNAVAILABLE, self._t(UNAVAILABLE_KEY)
        else:
            shown = state or AttentionState.UNKNOWN
            level, status = _LEVELS[shown], self._t(STATE_KEYS[shown])
        monitoring_up = (
            not unavailable
            and state is not None
            and state not in (AttentionState.UNKNOWN, AttentionState.ON_BREAK)
            and reading is not None
            and reading.health is not None
            and reading.health.monitoring_available
        )
        session = self._session
        banner_text = {
            BannerKind.NONE: "",
            BannerKind.WARNING: self._t("ui.banner.warned"),
            BannerKind.ESCALATED: self._t("ui.banner.escalated"),
        }[self._banner]
        with self._pending_lock:
            pending = self._pending
        return ScanViewModel(
            status_level=level,
            status_text=status,
            last_item_text=(
                self._t("ui.last_item", barcode=self._last_barcode)
                if self._last_barcode is not None
                else self._t("ui.last_item_none")
            ),
            message_text=self._message or self._t("ui.ready"),
            banner_kind=self._banner,
            banner_text=banner_text,
            sound_token=self._sound_token,
            monitoring_text=(
                self._t("ui.monitoring_active") if monitoring_up and session is not None else ""
            ),
            unavailable_text=self._t("ui.monitoring_unavailable") if unavailable else "",
            live_view_text=self._t("ui.live_view_active") if self._live_view else "",
            worker_text=(
                self._t("ui.worker", name=session.display_name)
                if session is not None
                else self._t("ui.no_worker")
            ),
            logged_in=session is not None,
            scans_pending=pending,
        )

    def _monitoring(
        self, reading: StatusReading | None, now: float
    ) -> tuple[AttentionState | None, bool]:
        """The fresh state (None if stale or failed) and whether monitoring is unavailable."""
        limit = self._config.unavailable_after_s
        last_answer = reading.at if reading is not None else self._started_at
        if reading is None or now - reading.at > limit:
            # Nothing fresh: the service or engine is not answering.
            self._problem_since = None
            return None, now - last_answer >= limit
        state = reading.state.state if reading.state is not None else None
        healthy = reading.health is not None and reading.health.monitoring_available
        problem = not healthy or state in (None, AttentionState.UNKNOWN)
        if not problem:
            self._problem_since = None
            return state, False
        if self._problem_since is None:
            self._problem_since = reading.at
        return state, now - self._problem_since >= limit
