"""Capture gate decisions and their `images_withheld` mapping (PLAN 2.5)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from ag_shared.api import ImagesWithheld


class GateReason(StrEnum):
    """Why the gate is open or closed (finer than the wire `images_withheld`)."""

    OPEN = "open"
    NO_SESSION = "no_session"
    """No worker is logged in (A6: consent cannot be established)."""
    SESSION_CHANGED = "session_changed"
    """The open session started after the burst's first frame: those frames
    show the previous worker or nobody, whose consent is unknown here."""
    NO_CONSENT = "no_consent"
    """The roster in force has no current consent for the worker (or no longer lists them)."""
    ON_BREAK = "on_break"
    """A scheduled break of the bundle's shifts, or the engine reports On Break."""
    ERROR = "error"
    """The decision itself failed; closed so a bug never lets an image through."""


_WITHHELD: Final[dict[GateReason, ImagesWithheld]] = {
    GateReason.OPEN: ImagesWithheld.NONE,
    GateReason.NO_SESSION: ImagesWithheld.NO_WORKER,
    GateReason.SESSION_CHANGED: ImagesWithheld.NO_WORKER,
    GateReason.NO_CONSENT: ImagesWithheld.NO_CONSENT,
    # Own wire value since schema revision 2 (owner decision 2026-11-21).
    GateReason.ON_BREAK: ImagesWithheld.ON_BREAK,
    # Consent could not be established.
    GateReason.ERROR: ImagesWithheld.NO_CONSENT,
}


def images_withheld_for(reason: GateReason) -> ImagesWithheld:
    """The `attention_event.images_withheld` value for a gate reason."""
    return _WITHHELD[reason]


@dataclass(frozen=True, slots=True)
class GateDecision:
    """One answer of the capture gate."""

    reason: GateReason
    decided_at: datetime | None
    """UTC time of the decision; None only when the clock itself failed."""
    session_id: UUID | None = None
    """The session the frames are attributed to when open."""

    @property
    def open(self) -> bool:
        """Whether frames may be written."""
        return self.reason is GateReason.OPEN

    @property
    def images_withheld(self) -> ImagesWithheld:
        """The wire value for this decision."""
        return images_withheld_for(self.reason)
