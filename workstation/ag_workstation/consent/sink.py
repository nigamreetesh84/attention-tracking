"""`GatedEvidenceSink`: the engine's `EvidenceSink`, with the capture gate in front.

Every finished burst goes through `CaptureGate`. Closed: nothing reaches the
store, no reference to the burst is kept, and the outcome says
`images_withheld = <reason>`. Open: the store writes the frames with the
gate's permit, re-checking the gate before each frame; a store refusal
(`disk_full`) is passed through. The outcome feeds the event's `image_ids`
and `images_withheld` (contract 4.3) through `on_outcome`.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ag_shared.api import ImagesWithheld

from ag_workstation.consent.decision import GateDecision, GateReason
from ag_workstation.consent.gate import CaptureGate
from ag_workstation.consent.permit import CapturePermit
from ag_workstation.engine_api import EvidenceBurst

logger = logging.getLogger(__name__)


class SavedBurst(Protocol):
    """What the store reports (`ag_workstation.evidence.SaveResult`)."""

    @property
    def image_ids(self) -> tuple[UUID, ...]:
        """Saved image ids in burst order."""
        ...

    @property
    def images_withheld(self) -> ImagesWithheld:
        """Why images are missing, or `none`."""
        ...


class PermittedBurstStore(Protocol):
    """The evidence store's write API (`EvidenceStore`)."""

    def save_burst(
        self, burst: EvidenceBurst, event_id: UUID | None = None, *, permit: CapturePermit
    ) -> SavedBurst:
        """Store the frames the permit allows."""
        ...


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """What happened to one burst's images."""

    event_id: UUID
    image_ids: tuple[UUID, ...]
    images_withheld: ImagesWithheld
    gate_reason: GateReason
    """The gate's answer at submit time (OPEN when the store was asked)."""
    frames: int
    """Frames the burst carried."""


class GatedEvidenceSink:
    """An `EvidenceSink` whose only path to disk is the capture gate. Never raises."""

    def __init__(
        self,
        gate: CaptureGate,
        store: PermittedBurstStore,
        *,
        on_outcome: Callable[[CaptureOutcome], None] | None = None,
    ) -> None:
        self._gate = gate
        self._store = store
        self._on_outcome = on_outcome

    def submit(self, burst: EvidenceBurst) -> None:
        """`EvidenceSink.submit`: gate, store, report."""
        self.save(burst)

    def save(self, burst: EvidenceBurst) -> CaptureOutcome:
        """Gate and store `burst`; returns what happened to its images."""
        outcome = self._save(burst)
        if self._on_outcome is not None:
            try:
                self._on_outcome(outcome)
            except Exception as exc:
                logger.error("evidence outcome listener failed: %s", type(exc).__name__)
        return outcome

    def _save(self, burst: EvidenceBurst) -> CaptureOutcome:
        event_id, frames = burst.event_id, len(burst.frames)
        try:
            grant = self._gate.permit(burst)
        except Exception as exc:  # the gate never raises; belt and braces
            logger.error("capture gate failed (%s); images withheld", type(exc).__name__)
            grant = GateDecision(GateReason.ERROR, None)
        if isinstance(grant, GateDecision):
            logger.info("images of event %s withheld: %s", event_id, grant.reason.value)
            return CaptureOutcome(event_id, (), grant.images_withheld, grant.reason, frames)
        try:
            result = self._store.save_burst(burst, permit=grant)
        except Exception as exc:
            # The store fails open for disk problems; anything else still loses
            # only images. disk_full is the wire value for "could not be stored".
            logger.error("evidence store failed (%s); images not saved", type(exc).__name__)
            return CaptureOutcome(event_id, (), ImagesWithheld.DISK_FULL, GateReason.OPEN, frames)
        return CaptureOutcome(
            event_id, tuple(result.image_ids), result.images_withheld, GateReason.OPEN, frames
        )
