"""Data passed between the engine stages after signal extraction.

Plain frozen dataclasses: these are built per frame on the hot path, so they
skip pydantic validation. Anything that leaves the workstation is converted
to a `/shared` model first.
"""

from dataclasses import dataclass, field

from ag_shared.signals import SignalFrame
from ag_shared.states import AttentionState


@dataclass(frozen=True, slots=True)
class CameraCandidate:
    """One camera's candidate state at one moment.

    `usable` means the candidate is not Unknown: the camera is still trusted,
    possibly holding its last state through a short bad-quality burst.
    `quality_ok` means the latest frame itself was good enough; fusion
    prefers such cameras (rules 1-2). `face_seen` means the latest frame held
    a good-quality face, which rules out Absent for the station (rule 5).
    """

    slot: int
    ts: float
    state: AttentionState
    confidence: float
    quality: float | None
    usable: bool
    face_seen: bool
    quality_ok: bool = True
    fallback: bool = False
    """The eye signals were unreadable and head-pose rules were used instead."""
    frame: SignalFrame | None = field(default=None, compare=False)
    """The signals behind this candidate (snapshot for events)."""

    @classmethod
    def unknown(cls, slot: int, ts: float, quality: float | None = None) -> "CameraCandidate":
        """A camera with no usable frames."""
        return cls(
            slot=slot,
            ts=ts,
            state=AttentionState.UNKNOWN,
            confidence=0.0,
            quality=quality,
            usable=False,
            face_seen=False,
            quality_ok=False,
        )


@dataclass(frozen=True, slots=True)
class FusedState:
    """The station-level result of fusing all camera candidates."""

    ts: float
    state: AttentionState
    confidence: float
    low_confidence: bool
    """Usable cameras disagreed and the more severe state was taken."""
    source_slots: tuple[int, ...]
    """Cameras whose candidates decided the state."""
    candidates: tuple[CameraCandidate, ...]
    degraded_slots: tuple[int, ...] = ()
    """Cameras offline or below quality for longer than the degraded limit."""
    monitoring_unavailable: bool = False
    """No camera is delivering frames at all."""
