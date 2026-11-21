"""The seam between the detection engine and the rest of the workstation.

Phase 2 code (scan flow, capture gate, service host, sync) depends only on
these interfaces. The engine runtime (PLAN 1.8) implements them through
`ag_workstation.engine.adapter.EngineAdapter`, and the evidence burst
assembly (PLAN 1.9, `evidence.burst`) emits the `EvidenceBurst` defined here;
`ag_workstation.fakes` implements them for tests and `--dev`.
Sources: `docs/detection-engine.md` sections 1, 5 (scan decision) and 7
(evidence capture).

Rules every implementation and caller keeps:

- The scan is recorded before `StationStateQuery.scan_state` is called and
  is accepted whatever the query does (CLAUDE.md rule 1).
- `scan_state` answers from memory. Callers still enforce
  `SCAN_QUERY_TIMEOUT_S` themselves and treat a timeout, an `EngineError` or
  any other exception as Unknown (`StationState.unknown`), so a misbehaving
  engine cannot reach the scan path.
- The engine never decides whether frames may be stored. It hands finished
  bursts to an `EvidenceSink`; the consent and capture gate (PLAN 2.5) wraps
  the sink and is the only path to disk.
- No worker identity reaches the engine: user and session ids are attached
  by the workstation when it turns states and episodes into records.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

from ag_shared.api.events import CameraStateSnapshot
from ag_shared.api.heartbeat import CameraHeartbeat
from ag_shared.config import EngineConfig
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState, ScanOutcome, scan_outcome

SCAN_QUERY_TIMEOUT_S: Final = 0.150
"""No answer within this time: the scan state is Unknown (section 5)."""
SCAN_QUERY_BUDGET_S: Final = 0.200
"""Hard budget for the scan-state query, IPC included (PLAN 1.8)."""


class EngineError(Exception):
    """The engine could not answer. Messages must not hold URIs or image data."""


class EngineUnavailableError(EngineError):
    """The engine is not running or not reachable (crashed, restarting)."""


class EngineTimeoutError(EngineError):
    """The engine did not answer in time."""


def _check_utc(value: datetime, name: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _check_unit(value: float | None, name: str) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be within 0-1")


@dataclass(frozen=True, slots=True)
class CameraState:
    """One camera's contribution at the time of a state answer."""

    slot: int
    status: CameraStatus
    state: AttentionState
    """The per-camera candidate state (section 5)."""
    quality: float | None = None
    usable: bool = False
    """Counted by fusion (not Unknown, quality ok)."""

    def __post_init__(self) -> None:
        if not 1 <= self.slot <= 4:
            raise ValueError("camera slot must be 1-4")
        _check_unit(self.quality, "quality")

    def to_snapshot(self) -> CameraStateSnapshot:
        """The wire form stored in `attention_event.camera_states`."""
        return CameraStateSnapshot(
            slot=self.slot,
            status=self.status,
            state=self.state,
            quality=self.quality,
            usable=self.usable,
        )


SignalSummary = Mapping[str, float | None]
"""Signal name -> value over the answer's window (None = invalid). Numbers
only; never landmarks, images or identity features."""


@dataclass(frozen=True, slots=True)
class StationState:
    """A station state answer: the scan decision or the current state."""

    state: AttentionState
    confidence: float | None
    low_confidence: bool
    """Fusion disagreement (section 6 rule 4)."""
    at: datetime
    """UTC wall time of the answer."""
    ts: float
    """Monotonic seconds of the answer (engine timeline)."""
    engine_version: str
    config_version: int
    signals: SignalSummary = field(default_factory=dict)
    camera_states: tuple[CameraState, ...] = ()

    def __post_init__(self) -> None:
        _check_utc(self.at, "at")
        _check_unit(self.confidence, "confidence")
        if self.config_version < 0:
            raise ValueError("config_version must not be negative")
        # Freeze a caller's dict so a shared answer cannot change later.
        object.__setattr__(self, "signals", MappingProxyType(dict(self.signals)))

    @classmethod
    def unknown(
        cls, at: datetime, ts: float, *, engine_version: str = "", config_version: int = 0
    ) -> "StationState":
        """The fail-open answer used when the engine fails or times out."""
        return cls(
            state=AttentionState.UNKNOWN,
            confidence=None,
            low_confidence=False,
            at=at,
            ts=ts,
            engine_version=engine_version,
            config_version=config_version,
        )

    @property
    def outcome(self) -> ScanOutcome:
        """The scan outcome this state gives (section 5)."""
        return scan_outcome(self.state)


class StationStateQuery(Protocol):
    """Read the station state from the engine's memory."""

    def scan_state(self) -> StationState:
        """The scan state by the section 5 decision (current severe state,
        else the 40% window rule, else the current state).

        Answers from memory, no inference. May raise `EngineError` (or, if
        buggy, anything) or be slow; callers enforce `SCAN_QUERY_TIMEOUT_S`.
        """
        ...

    def current_state(self) -> StationState:
        """The current station state (live wall, heartbeat, UI). Same failure rules."""
        ...


@dataclass(frozen=True, slots=True)
class CameraHealthSummary:
    """One camera in the engine's health report."""

    slot: int
    status: CameraStatus
    fps: float = 0.0
    quality: float | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.slot <= 4:
            raise ValueError("camera slot must be 1-4")
        if self.fps < 0:
            raise ValueError("fps must not be negative")
        _check_unit(self.quality, "quality")

    def to_heartbeat(self) -> CameraHeartbeat:
        """The wire form used in heartbeats and health samples."""
        return CameraHeartbeat(
            slot=self.slot, status=self.status, fps=self.fps, quality=self.quality
        )


@dataclass(frozen=True, slots=True)
class EngineHealth:
    """Engine health metrics (PLAN 1.8: fps, quality, restarts)."""

    status: EngineStatus
    cameras: tuple[CameraHealthSummary, ...] = ()
    restarts: int = 0
    engine_version: str = ""
    config_version: int = 0
    last_error: str | None = None
    """Exception type name only, never a message (it could hold a URI)."""

    @property
    def monitoring_available(self) -> bool:
        """False when no camera is online ("Monitoring unavailable")."""
        return self.status not in (EngineStatus.DOWN, EngineStatus.RESTARTING) and any(
            c.status is CameraStatus.ONLINE for c in self.cameras
        )


class EngineHealthSource(Protocol):
    """Report engine health."""

    def health(self) -> EngineHealth:
        """A snapshot; must not block and must not raise."""
        ...


@dataclass(frozen=True, slots=True)
class ConfigApplyResult:
    """Outcome of a config swap. On rejection the previous config stays active."""

    accepted: bool
    config_version: int
    """The version now active (the old one after a rejection)."""
    reason: str | None = None


class EngineConfigTarget(Protocol):
    """Apply a new engine config between frames (detection-engine section 8)."""

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        """Swap atomically. An unusable config is refused with a reason; it
        never raises for that and never leaves a half-applied state. Also used
        when a worker's PPE override replaces the station profile."""
        ...


@dataclass(frozen=True, slots=True)
class StateEpisode:
    """A `state_episode` as the engine reports it (section 5)."""

    id: UUID
    """UUIDv7 assigned when the episode opens; becomes the attention_event id."""
    state: AttentionState
    started_at: datetime
    confidence: float | None
    low_confidence: bool
    engine_version: str
    config_version: int
    ended_at: datetime | None = None
    signals: SignalSummary = field(default_factory=dict)
    camera_states: tuple[CameraState, ...] = ()

    def __post_init__(self) -> None:
        _check_utc(self.started_at, "started_at")
        if self.ended_at is not None:
            _check_utc(self.ended_at, "ended_at")
            if self.ended_at < self.started_at:
                raise ValueError("ended_at is before started_at")
        _check_unit(self.confidence, "confidence")
        object.__setattr__(self, "signals", MappingProxyType(dict(self.signals)))


@dataclass(frozen=True, slots=True)
class StateBucket:
    """Seconds per state in one closed minute."""

    bucket_start: datetime
    seconds: Mapping[AttentionState, float]

    def __post_init__(self) -> None:
        _check_utc(self.bucket_start, "bucket_start")
        if self.bucket_start.second or self.bucket_start.microsecond:
            raise ValueError("bucket_start must be on a whole minute")
        if any(v < 0 for v in self.seconds.values()) or sum(self.seconds.values()) > 60.001:
            raise ValueError("a bucket holds 0-60 seconds")
        object.__setattr__(self, "seconds", MappingProxyType(dict(self.seconds)))


class EngineEventListener(Protocol):
    """Receives episodes and buckets from the state machine (PLAN 1.7, 1.8).

    Called on an engine thread: implementations hand off quickly (queue) and
    must not raise; the engine catches and logs if they do.
    """

    def episode_opened(self, episode: StateEpisode) -> None:
        """A transition into Distracted, Drowsy, Sleeping or Absent."""
        ...

    def episode_closed(self, episode: StateEpisode) -> None:
        """The same episode with `ended_at` set."""
        ...

    def bucket_closed(self, bucket: StateBucket) -> None:
        """A minute has closed."""
        ...


class BurstKind(StrEnum):
    """Why a burst was taken (section 7)."""

    SCAN_WARNING = "scan_warning"
    """Key + pre + post frames from the primary, key from each usable secondary."""
    EPISODE_KEYFRAME = "episode_keyframe"
    """One key frame for a Sleeping or Absent episode."""


class FrameRole(StrEnum):
    """A frame's place in a burst."""

    PRE = "pre"
    KEY = "key"
    POST = "post"


JPEG_SOI: Final = b"\xff\xd8"


@dataclass(frozen=True, slots=True)
class EvidenceFrame:
    """One JPEG frame taken from a camera's in-memory ring."""

    camera_slot: int
    captured_at: datetime
    offset_ms: int
    """Relative to the trigger; negative for pre-frames."""
    seq: int
    """The camera's frame sequence number."""
    role: FrameRole
    jpeg: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.camera_slot <= 4:
            raise ValueError("camera slot must be 1-4")
        _check_utc(self.captured_at, "captured_at")
        if self.seq < 0:
            raise ValueError("seq must not be negative")
        if self.role is FrameRole.PRE and self.offset_ms >= 0:
            raise ValueError("a pre-frame has a negative offset")
        if self.role is FrameRole.POST and self.offset_ms <= 0:
            raise ValueError("a post-frame has a positive offset")
        if not self.jpeg.startswith(JPEG_SOI):
            raise ValueError("frame data is not a JPEG")

    @property
    def sha256(self) -> str:
        """Lower-case hex SHA-256 of the JPEG bytes."""
        return hashlib.sha256(self.jpeg).hexdigest()

    @property
    def size_bytes(self) -> int:
        """JPEG size."""
        return len(self.jpeg)


@dataclass(frozen=True, slots=True)
class EvidenceBurst:
    """The frames for one event, assembled once its post-frames are in."""

    event_id: UUID
    """The scan warning's or episode's attention_event id."""
    kind: BurstKind
    triggered_at: datetime
    trigger_ts: float
    """Monotonic seconds of the trigger (scan time or episode start)."""
    frames: tuple[EvidenceFrame, ...]
    expected_frames: int
    """How many frames the burst should have had; more than `len(frames)`
    when a camera was offline or the ring did not hold a frame."""
    missing: tuple[str, ...] = ()
    """Optional labels of the frames that could not be taken, one per missing
    frame (`key`, `pre`, `post`, `secondary_key:<slot>`); empty when the
    producer does not name them."""

    def __post_init__(self) -> None:
        _check_utc(self.triggered_at, "triggered_at")
        keys = [(f.camera_slot, f.seq) for f in self.frames]
        if len(set(keys)) != len(keys):
            raise ValueError("a frame appears twice in the burst")
        if len(self.frames) > self.expected_frames:
            raise ValueError("more frames than expected")
        if self.missing and len(self.missing) != self.expected_frames - len(self.frames):
            raise ValueError("missing labels do not match the missing frame count")
        object.__setattr__(self, "missing", tuple(self.missing))
        ordered = tuple(sorted(self.frames, key=lambda f: (f.camera_slot, f.offset_ms, f.seq)))
        object.__setattr__(self, "frames", ordered)

    @property
    def missing_frames(self) -> int:
        """Expected frames that could not be taken."""
        return self.expected_frames - len(self.frames)

    @property
    def complete(self) -> bool:
        """Whether every expected frame is present."""
        return self.missing_frames == 0


class EvidenceSink(Protocol):
    """Receives finished bursts from the `BurstAssembler` (PLAN 1.9). The
    capture gate's `GatedEvidenceSink` (2.5) is the production sink."""

    def submit(self, burst: EvidenceBurst) -> None:
        """Take a burst. Called on an engine thread: must return quickly.
        The caller catches and logs exceptions; a failure loses only images."""
        ...


class EvidenceRequester(Protocol):
    """Ask the engine to assemble a burst from its rings."""

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        """Non-blocking: key and pre-frames are taken now, post-frames as they
        arrive; the finished burst goes to the `EvidenceSink`. Never raises
        into the scan path; a burst that cannot be built is dropped and logged."""
        ...


class StationEngine(
    StationStateQuery, EngineHealthSource, EngineConfigTarget, EvidenceRequester, Protocol
):
    """Everything the workstation needs from the engine, in one handle."""
