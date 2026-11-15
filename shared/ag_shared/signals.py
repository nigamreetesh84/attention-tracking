"""Signal names and the per-frame signal containers.

Source: `docs/detection-engine.md` section 2. Signals are plain numbers and
flags; no face templates, embeddings or identity features exist here.
"""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class SignalName(StrEnum):
    """Every signal the engine can extract per camera per frame."""

    FACE_PRESENT = "face_present"
    QUALITY = "quality"
    EAR = "ear"
    EYES_CLOSED = "eyes_closed"
    PERCLOS = "perclos"
    BLINK_RATE = "blink_rate"
    MAR = "mar"
    YAWN = "yawn"
    HEAD_YAW = "head_yaw"
    HEAD_PITCH = "head_pitch"
    HEAD_ROLL = "head_roll"
    HEAD_NOD = "head_nod"
    HEAD_DROPPED = "head_dropped"
    POSTURE = "posture"
    MOTION = "motion"


# The groups are the columns of the PPE profile table (section 3).
ALWAYS_ON_SIGNALS: frozenset[SignalName] = frozenset({SignalName.FACE_PRESENT, SignalName.QUALITY})
"""Needed for Absent and Unknown; no profile may disable them."""

EYE_SIGNALS: frozenset[SignalName] = frozenset(
    {SignalName.EAR, SignalName.EYES_CLOSED, SignalName.PERCLOS, SignalName.BLINK_RATE}
)
YAWN_SIGNALS: frozenset[SignalName] = frozenset({SignalName.MAR, SignalName.YAWN})
HEAD_POSE_SIGNALS: frozenset[SignalName] = frozenset(
    {
        SignalName.HEAD_YAW,
        SignalName.HEAD_PITCH,
        SignalName.HEAD_ROLL,
        SignalName.HEAD_NOD,
        SignalName.HEAD_DROPPED,
    }
)
BODY_SIGNALS: frozenset[SignalName] = frozenset({SignalName.POSTURE, SignalName.MOTION})

BOOLEAN_SIGNALS: frozenset[SignalName] = frozenset(
    {
        SignalName.FACE_PRESENT,
        SignalName.EYES_CLOSED,
        SignalName.YAWN,
        SignalName.HEAD_NOD,
        SignalName.HEAD_DROPPED,
    }
)
"""Signals carried as 1.0 / 0.0; read them with `SignalFrame.flag`."""

SIGNAL_DEPENDENCIES: dict[SignalName, SignalName] = {
    SignalName.EYES_CLOSED: SignalName.EAR,
    SignalName.PERCLOS: SignalName.EYES_CLOSED,
    SignalName.BLINK_RATE: SignalName.EYES_CLOSED,
    SignalName.YAWN: SignalName.MAR,
    SignalName.HEAD_NOD: SignalName.HEAD_PITCH,
    SignalName.HEAD_DROPPED: SignalName.HEAD_PITCH,
}
"""Derived signal -> the signal it is computed from. A profile cannot enable
a derived signal while its source is disabled."""

_Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class SignalReading(BaseModel):
    """One signal value with its validity flag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: _Finite
    valid: StrictBool = True

    @classmethod
    def flag(cls, on: bool, *, valid: bool = True) -> "SignalReading":
        """Build a reading for a boolean signal."""
        return cls(value=1.0 if on else 0.0, valid=valid)

    @classmethod
    def invalid(cls) -> "SignalReading":
        """A reading that could not be measured on this frame."""
        return cls(value=0.0, valid=False)

    @property
    def is_true(self) -> bool:
        """A valid boolean signal that is set."""
        return self.valid and self.value >= 0.5


class SignalFrame(BaseModel):
    """All signals of one camera for one frame.

    `ts` is seconds on the engine's injected clock (frame timestamps in
    replay), not wall time. Signals disabled by the PPE profile are absent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    camera_slot: Annotated[int, Field(strict=True, ge=1, le=4)]
    ts: _Finite
    signals: dict[SignalName, SignalReading]

    def reading(self, name: SignalName) -> SignalReading | None:
        """The raw reading, or None when the signal was not computed."""
        return self.signals.get(name)

    def value(self, name: SignalName) -> float | None:
        """The value when present and valid, otherwise None."""
        reading = self.signals.get(name)
        return reading.value if reading is not None and reading.valid else None

    def flag(self, name: SignalName) -> bool | None:
        """A boolean signal's value when present and valid, otherwise None."""
        reading = self.signals.get(name)
        return reading.is_true if reading is not None and reading.valid else None
