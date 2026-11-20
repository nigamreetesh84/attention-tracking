"""Single-threaded station pipeline: landmarks -> signals -> candidates -> state.

Everything the engine computes after the landmark model, with no threads and
no clock of its own. The threaded `Engine` drives it from live cameras; the
replay harness drives it from video files and signal traces with frame time
as the clock, which makes replays deterministic.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ag_shared.config import CameraConfig, EngineConfig
from ag_shared.ids import uuid7
from ag_shared.signals import SignalFrame, SignalName
from ag_shared.thresholds import Thresholds

from ag_workstation.engine.calendar import CalendarStatus, ShiftCalendar
from ag_workstation.engine.estimator import CameraEstimator, EstimatorParams
from ag_workstation.engine.fusion import DegradedTracker, fuse
from ag_workstation.engine.state_machine import (
    Bucket,
    Episode,
    ScanDecision,
    StationStateMachine,
    StationStatus,
)
from ag_workstation.engine.types import CameraCandidate, FusedState
from ag_workstation.landmarks.types import LandmarkResult
from ag_workstation.signals.extractor import SignalExtractor
from ag_workstation.signals.params import ExtractionParams


@dataclass(slots=True)
class PipelineOutput:
    """Result of one fusion tick."""

    fused: FusedState
    status: StationStatus
    calendar: CalendarStatus
    opened: list[Episode] = field(default_factory=list)
    closed: list[Episode] = field(default_factory=list)
    buckets: list[Bucket] = field(default_factory=list)


@dataclass(slots=True)
class _Camera:
    config: CameraConfig
    extractor: SignalExtractor
    estimator: CameraEstimator


def needs_pose(config: EngineConfig) -> bool:
    """Whether the profile uses the pose model (posture or motion)."""
    return config.profile.is_enabled(SignalName.POSTURE) or config.profile.is_enabled(
        SignalName.MOTION
    )


class StationPipeline:
    """The engine's logic for one station. Not thread-safe: callers serialize."""

    def __init__(
        self,
        config: EngineConfig,
        *,
        id_factory: Callable[[], UUID] = uuid7,
        estimator_params: EstimatorParams | None = None,
        extraction_params: ExtractionParams | None = None,
    ) -> None:
        self._est_params = estimator_params
        self._ext_params = extraction_params
        self._config = config
        self._calendar = ShiftCalendar(config.shifts, config.timezone)
        self._thresholds = config.thresholds
        self._shift_id: UUID | None = None
        self._cameras: dict[int, _Camera] = {}
        self._build_cameras(config)
        self._degraded = DegradedTracker()
        self._machine = StationStateMachine(self._thresholds, id_factory=id_factory)
        self._last_fused: FusedState | None = None

    # --- configuration ---

    @property
    def config(self) -> EngineConfig:
        """The active configuration."""
        return self._config

    @property
    def thresholds(self) -> Thresholds:
        """The thresholds in force (they follow the active shift)."""
        return self._thresholds

    @property
    def with_pose(self) -> bool:
        """Whether frames need the pose model."""
        return needs_pose(self._config)

    def _build_cameras(self, config: EngineConfig) -> None:
        cameras: dict[int, _Camera] = {}
        for cam in config.cameras:
            previous = self._cameras.get(cam.slot)
            if previous is not None:
                previous.extractor.configure(
                    camera=cam, profile=config.profile, thresholds=self._thresholds
                )
                previous.estimator.configure(profile=config.profile, thresholds=self._thresholds)
                previous.config = cam
                cameras[cam.slot] = previous
                continue
            cameras[cam.slot] = _Camera(
                config=cam,
                extractor=SignalExtractor(cam, config.profile, self._thresholds, self._ext_params),
                estimator=CameraEstimator(
                    cam.slot, config.profile, self._thresholds, self._est_params
                ),
            )
        self._cameras = cameras

    def apply_config(self, config: EngineConfig) -> None:
        """Switch to a validated config between frames. Running timers are kept
        for cameras that stay; a new calendar is built first so a bad timezone
        fails before anything changes."""
        calendar = ShiftCalendar(config.shifts, config.timezone)
        self._config = config
        self._calendar = calendar
        self._thresholds = config.thresholds_for(self._shift_id)
        self._build_cameras(config)
        self._machine.configure(self._thresholds)

    def _set_thresholds(self, thresholds: Thresholds) -> None:
        if thresholds == self._thresholds:
            return
        self._thresholds = thresholds
        for cam in self._cameras.values():
            cam.extractor.configure(thresholds=thresholds)
            cam.estimator.configure(thresholds=thresholds)
        self._machine.configure(thresholds)

    def reset_session(self) -> None:
        """A new worker: forget per-person baselines and windows."""
        for cam in self._cameras.values():
            cam.extractor.reset_session()
            cam.estimator.reset()

    # --- per-camera input ---

    def process_landmarks(self, result: LandmarkResult) -> CameraCandidate | None:
        """Extract signals from one camera's landmarks and update its estimate.

        None when the camera is not configured or is disabled.
        """
        cam = self._cameras.get(result.camera_slot)
        if cam is None or not cam.config.is_enabled:
            return None
        return cam.estimator.update(cam.extractor.extract(result))

    def process_signals(self, frame: SignalFrame) -> CameraCandidate | None:
        """Update one camera's estimate from ready-made signals (trace replay)."""
        cam = self._cameras.get(frame.camera_slot)
        if cam is None or not cam.config.is_enabled:
            return None
        return cam.estimator.update(frame)

    # --- station ---

    def candidates(self, ts: float) -> list[CameraCandidate]:
        """Every configured camera's candidate at `ts` (Unknown when stale or off)."""
        out: list[CameraCandidate] = []
        for slot, cam in sorted(self._cameras.items()):
            if not cam.config.is_enabled:
                out.append(CameraCandidate.unknown(slot, ts))
            else:
                out.append(cam.estimator.candidate_at(ts))
        return out

    def tick(
        self, ts: float, wall: datetime, *, offline_slots: Collection[int] = ()
    ) -> PipelineOutput:
        """Fuse the cameras at `ts` and advance the station state."""
        calendar = self._calendar.status(wall)
        if calendar.shift_id != self._shift_id:
            self._shift_id = calendar.shift_id
            self._set_thresholds(self._config.thresholds_for(calendar.shift_id))
        disabled = {s for s, c in self._cameras.items() if not c.config.is_enabled}
        offline = set(offline_slots) | disabled
        candidates = self.candidates(ts)
        degraded = self._degraded.update(
            ts, [c for c in candidates if c.slot not in disabled], offline
        )
        fused = fuse(
            candidates,
            primary_slot=self._config.primary_camera.slot,
            ts=ts,
            offline_slots=offline,
            degraded_slots=degraded,
        )
        self._last_fused = fused
        out = self._machine.update(fused, wall, on_break=calendar.on_break)
        return PipelineOutput(
            fused=fused,
            status=out.status,
            calendar=calendar,
            opened=out.opened,
            closed=out.closed,
            buckets=out.buckets,
        )

    def scan_decision(self, ts: float | None = None) -> ScanDecision:
        """The scan-time state, from memory."""
        return self._machine.scan_decision(ts)

    @property
    def last_fused(self) -> FusedState | None:
        """The most recent fused state."""
        return self._last_fused

    @property
    def open_episode(self) -> Episode | None:
        """The station episode in progress."""
        return self._machine.open_episode

    def status(self) -> StationStatus:
        """The current station state."""
        return self._machine.status()

    def flush(self) -> tuple[list[Episode], list[Bucket]]:
        """Close the open episode and the open minute (engine stop, replay end)."""
        out = self._machine.flush()
        return out.closed, out.buckets
