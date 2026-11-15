"""Engine configuration models shared by the server and the workstation.

Source: `docs/detection-engine.md` sections 7 and 8, `docs/schema.md`
(camera, shift, shift_break) and `docs/api-contract.md` section 4.2 (the
config bundle carries these shapes). The engine receives one complete,
validated `EngineConfig`; an invalid one is refused and the old one stays.
"""

from datetime import time
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from ag_shared.ppe import PpeProfile
from ag_shared.states import WARNED_STATES, AttentionState
from ag_shared.thresholds import Thresholds

_DAY_S = 24 * 3600


class CameraRole(StrEnum):
    """Fusion role. Exactly one camera per station is primary."""

    PRIMARY = "primary"
    SECONDARY = "secondary"


class CameraKind(StrEnum):
    """How the camera is reached."""

    USB = "usb"
    RTSP = "rtsp"
    ONVIF = "onvif"
    BUILTIN = "builtin"


_Unit = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
_NeutralDeg = Annotated[float, Field(strict=True, ge=-90, le=90, allow_inf_nan=False)]


class RoiPoint(BaseModel):
    """One polygon vertex in normalized image coordinates (0-1, origin top-left)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: _Unit
    y: _Unit


class Calibration(BaseModel):
    """Per-camera calibration stored by the calibration wizard.

    Head pose signals are reported relative to this neutral pose.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    neutral_yaw_deg: _NeutralDeg = 0.0
    neutral_pitch_deg: _NeutralDeg = 0.0
    neutral_roll_deg: _NeutralDeg = 0.0
    exposure_notes: str | None = None


class CameraConfig(BaseModel):
    """One camera of a station."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: Annotated[int, Field(ge=1, le=4)]
    role: CameraRole
    kind: CameraKind
    # repr=False: the URI may hold credentials and repr() ends up in logs.
    uri: Annotated[str, Field(min_length=1, repr=False)]
    roi: Annotated[tuple[RoiPoint, ...], Field(min_length=3)] | None = None
    """Polygon the worker's face must be in; None means the whole frame."""
    calibration: Calibration = Calibration()
    is_enabled: StrictBool = True


class CaptureConfig(BaseModel):
    """Evidence capture: burst shape, JPEG quality and the in-memory ring.

    The ring only ever lives in memory; nothing here enables recording.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pre_frames: Annotated[int, Field(ge=0, le=10)] = 2
    post_frames: Annotated[int, Field(ge=0, le=10)] = 2
    frame_interval_ms: Annotated[int, Field(ge=100, le=5000)] = 500
    jpeg_quality: Annotated[int, Field(ge=1, le=100)] = 80
    ring_seconds: Annotated[float, Field(strict=True, gt=0, le=60, allow_inf_nan=False)] = 3.0
    episode_keyframe_states: tuple[AttentionState, ...] = (
        AttentionState.SLEEPING,
        AttentionState.ABSENT,
    )
    """`state_episode` events of these states save one key frame."""

    @field_validator("episode_keyframe_states", mode="after")
    @classmethod
    def _only_warned_states(cls, value: tuple[AttentionState, ...]) -> tuple[AttentionState, ...]:
        if len(set(value)) != len(value):
            raise ValueError("episode_keyframe_states must not repeat a state")
        if not set(value) <= WARNED_STATES:
            raise ValueError("only Distracted, Drowsy, Sleeping and Absent open episodes")
        return value

    @model_validator(mode="after")
    def _ring_holds_the_burst(self) -> Self:
        # Post-frames arrive after the scan; pre-frames and the key frame must
        # already be in the ring when the scan happens.
        if self.ring_frames < self.pre_frames + 1:
            raise ValueError("ring_seconds is too short for pre_frames at frame_interval_ms")
        return self

    @property
    def ring_frames(self) -> int:
        """How many frames the ring holds."""
        return int(self.ring_seconds * 1000) // self.frame_interval_ms


def _seconds(t: time) -> float:
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def _naive(value: time) -> time:
    # Shift times are site-local wall times; an offset would be ambiguous.
    if value.tzinfo is not None:
        raise ValueError("shift and break times are local times without an offset")
    return value


class ShiftBreak(BaseModel):
    """A scheduled break inside a shift (site-local times)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1)]
    start_time: time
    end_time: time

    _naive_times = field_validator("start_time", "end_time", mode="after")(_naive)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if self.start_time == self.end_time:
            raise ValueError("a break must not have zero length")
        return self


class Shift(BaseModel):
    """A shift with its breaks.

    Times are site-local and may cross midnight (`end_time` before
    `start_time`). `days_of_week` uses 0 = Monday ... 6 = Sunday and names
    the day the shift starts on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: Annotated[str, Field(min_length=1)]
    start_time: time
    end_time: time
    days_of_week: Annotated[tuple[Annotated[int, Field(ge=0, le=6)], ...], Field(min_length=1)]
    breaks: tuple[ShiftBreak, ...] = ()

    _naive_times = field_validator("start_time", "end_time", mode="after")(_naive)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if len(set(self.days_of_week)) != len(self.days_of_week):
            raise ValueError("days_of_week must not repeat a day")
        if self.start_time == self.end_time:
            raise ValueError("a shift must not have zero length")
        start = _seconds(self.start_time)
        length = (_seconds(self.end_time) - start) % _DAY_S
        for brk in self.breaks:
            # Offsets from the shift start, so overnight shifts need no special case.
            offset = (_seconds(brk.start_time) - start) % _DAY_S
            duration = (_seconds(brk.end_time) - _seconds(brk.start_time)) % _DAY_S
            if offset >= length or offset + duration > length:
                raise ValueError(f"break {brk.name!r} is outside the shift")
        return self


class ShiftThresholds(BaseModel):
    """Fully resolved thresholds that apply while one shift is active."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shift_id: UUID
    thresholds: Thresholds


class EngineConfig(BaseModel):
    """Everything the detection engine needs, complete and validated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cameras: Annotated[tuple[CameraConfig, ...], Field(min_length=1, max_length=4)]
    """Ordered by slot."""
    profile: PpeProfile
    thresholds: Thresholds = Thresholds()
    """Resolved thresholds when no shift-specific entry applies."""
    shift_thresholds: tuple[ShiftThresholds, ...] = ()
    shifts: tuple[Shift, ...] = ()
    capture: CaptureConfig = CaptureConfig()
    timezone: Annotated[str, Field(min_length=1)] = "UTC"
    """IANA name of the site timezone; shift and break times are in it."""
    config_version: Annotated[int, Field(ge=0)] = 0

    @field_validator("cameras", mode="after")
    @classmethod
    def _check_cameras(cls, value: tuple[CameraConfig, ...]) -> tuple[CameraConfig, ...]:
        slots = [c.slot for c in value]
        if len(set(slots)) != len(slots):
            raise ValueError("camera slots must be unique")
        if sum(c.role is CameraRole.PRIMARY for c in value) != 1:
            raise ValueError("exactly one camera must be primary")
        return tuple(sorted(value, key=lambda c: c.slot))

    @model_validator(mode="after")
    def _check_shifts(self) -> Self:
        shift_ids = [s.id for s in self.shifts]
        if len(set(shift_ids)) != len(shift_ids):
            raise ValueError("shift ids must be unique")
        threshold_ids = [s.shift_id for s in self.shift_thresholds]
        if len(set(threshold_ids)) != len(threshold_ids):
            raise ValueError("at most one shift_thresholds entry per shift")
        if not set(threshold_ids) <= set(shift_ids):
            raise ValueError("shift_thresholds refers to a shift that is not in shifts")
        return self

    @property
    def scan_window_s(self) -> float:
        """Length of the rolling window read on a scan."""
        return self.thresholds.scan_window_s

    @property
    def primary_camera(self) -> CameraConfig:
        """The primary camera (there is exactly one)."""
        return next(c for c in self.cameras if c.role is CameraRole.PRIMARY)

    @property
    def secondary_cameras(self) -> tuple[CameraConfig, ...]:
        """The secondary cameras, by slot."""
        return tuple(c for c in self.cameras if c.role is CameraRole.SECONDARY)

    def thresholds_for(self, shift_id: UUID | None) -> Thresholds:
        """Thresholds to use while `shift_id` is active (None = no shift)."""
        for entry in self.shift_thresholds:
            if entry.shift_id == shift_id:
                return entry.thresholds
        return self.thresholds
