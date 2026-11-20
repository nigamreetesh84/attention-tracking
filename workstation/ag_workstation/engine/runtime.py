"""Threaded engine runtime (`docs/detection-engine.md` section 1).

Wires capture, the landmark provider and the `StationPipeline` together:

- one processing thread per camera takes the newest frame (latest-frame-wins,
  never a queue of stale frames), runs the landmark model outside any lock
  and feeds the pipeline;
- one tick thread fuses the cameras every `tick_interval_s`, advances the
  station state and publishes events;
- `scan_decision()` answers from memory with a bounded wait and returns
  Unknown rather than block, so the scan path is never held up;
- a camera whose model or thread fails is isolated: its candidate goes stale
  (Unknown) and its thread is restarted, the other cameras carry on;
- `apply_config` validates first and refuses an invalid config, keeping the
  old one.

The process-level supervisor (restart within 5 s after a crash) belongs to
the Windows service host (task 2.3).
"""

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ag_shared.config import CameraConfig, EngineConfig
from ag_shared.ids import uuid7
from ag_shared.states import AttentionState, ScanOutcome
from pydantic import ValidationError

from ag_workstation.capture.latest import LatestFrame
from ag_workstation.capture.manager import CaptureManager
from ag_workstation.capture.types import CameraHealth, CameraStatus, Frame
from ag_workstation.clock import Clock, SystemClock
from ag_workstation.engine.pipeline import PipelineOutput, StationPipeline
from ag_workstation.engine.state_machine import Bucket, Episode, ScanDecision, StationStatus
from ag_workstation.engine.types import CameraCandidate
from ag_workstation.landmarks.types import LandmarkError, LandmarkProvider

log = logging.getLogger(__name__)

SCAN_QUERY_WAIT_S = 0.1
"""Longest `scan_decision` waits for the engine lock before answering Unknown."""

UNKNOWN_DECISION = ScanDecision(AttentionState.UNKNOWN, 0.0, False, ScanOutcome.UNMONITORED)


class FrameFeed(Protocol):
    """What the engine needs from capture (`CaptureManager` implements it)."""

    @property
    def slots(self) -> tuple[int, ...]:
        """Captured camera slots."""
        ...

    def frames(self, slot: int) -> LatestFrame:
        """The latest-frame slot of one camera."""
        ...

    def health(self) -> dict[int, CameraHealth]:
        """Capture health by slot."""
        ...

    def start(self) -> None:
        """Start capturing."""
        ...

    def stop(self, timeout: float = 5.0) -> None:
        """Stop capturing. Never raises."""
        ...


FeedFactory = Callable[[Sequence[CameraConfig]], FrameFeed]
ProviderFactory = Callable[[CameraConfig], LandmarkProvider]
FrameListener = Callable[[Frame], None]
"""Sees every processed frame (the evidence ring, task 1.9). Must be quick."""


def _default_feed(cameras: Sequence[CameraConfig]) -> FrameFeed:
    return CaptureManager(cameras)


@dataclass(frozen=True, slots=True)
class EpisodeOpened:
    """A `state_episode` started."""

    episode: Episode


@dataclass(frozen=True, slots=True)
class EpisodeClosed:
    """A `state_episode` ended."""

    episode: Episode


@dataclass(frozen=True, slots=True)
class BucketClosed:
    """A per-minute bucket is complete."""

    bucket: Bucket


EngineEvent = EpisodeOpened | EpisodeClosed | BucketClosed


@dataclass(frozen=True, slots=True)
class CameraRuntimeHealth:
    """One camera as the engine sees it."""

    slot: int
    capture: CameraHealth | None
    processed_fps: float
    last_quality: float | None
    candidate: AttentionState
    model_errors: int
    processing_errors: int
    thread_restarts: int


@dataclass(frozen=True, slots=True)
class EngineHealth:
    """Engine health for the heartbeat and the local health screen."""

    running: bool
    config_version: int
    status: StationStatus
    cameras: dict[int, CameraRuntimeHealth]
    tick_errors: int
    config_rejections: int
    pending_events: int


class _CameraStats:
    def __init__(self) -> None:
        self.processed: deque[float] = deque(maxlen=64)
        self.model_errors = 0
        self.processing_errors = 0
        self.restarts = 0
        self.last_quality: float | None = None

    def fps(self, now: float, window_s: float = 5.0) -> float:
        recent = [t for t in self.processed if now - t <= window_s]
        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        return (len(recent) - 1) / span if span > 0 else 0.0


class Engine:
    """The detection engine for one station."""

    def __init__(
        self,
        config: EngineConfig,
        *,
        provider_factory: ProviderFactory,
        feed_factory: FeedFactory = _default_feed,
        clock: Clock | None = None,
        tick_interval_s: float = 0.1,
        frame_listener: FrameListener | None = None,
        id_factory: Callable[[], Any] = uuid7,
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._provider_factory = provider_factory
        self._feed_factory = feed_factory
        self._tick_interval = tick_interval_s
        self._frame_listener = frame_listener
        self._lock = threading.Lock()
        """Guards the pipeline and the published snapshot."""
        self._lifecycle = threading.RLock()
        """Serializes start, stop and camera reconfiguration."""
        self._pipeline = StationPipeline(config, id_factory=id_factory)
        self._config = config
        self._feed: FrameFeed | None = None
        self._providers: dict[int, LandmarkProvider] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._stats: dict[int, _CameraStats] = {}
        self._camera_stop = threading.Event()
        self._stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._events: queue.SimpleQueue[EngineEvent] = queue.SimpleQueue()
        self._tick_errors = 0
        self._rejections = 0
        self._running = False
        self._status = self._pipeline.status()

    # --- lifecycle ---

    @property
    def config(self) -> EngineConfig:
        """The active configuration."""
        return self._config

    def start(self) -> None:
        """Start capture, per-camera processing and the tick loop."""
        with self._lifecycle:
            if self._running:
                return
            self._stop.clear()
            self._start_cameras(self._config)
            self._tick_thread = self._spawn_tick()
            self._running = True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop everything and flush the open episode and minute. Never raises."""
        with self._lifecycle:
            if not self._running:
                return
            self._stop.set()
            if self._tick_thread is not None:
                self._tick_thread.join(timeout)
                self._tick_thread = None
            self._stop_cameras(timeout)
            with self._lock:
                closed, buckets = self._pipeline.flush()
            self._publish(closed=closed, buckets=buckets)
            self._running = False

    def _start_cameras(self, config: EngineConfig) -> None:
        enabled = [c for c in config.cameras if c.is_enabled]
        self._camera_stop = threading.Event()
        self._feed = self._feed_factory(enabled)
        for cam in enabled:
            self._stats.setdefault(cam.slot, _CameraStats())
            try:
                self._providers[cam.slot] = self._provider_factory(cam)
            except Exception as exc:
                # The camera stays Unknown; the others run.
                log.error(
                    "landmark provider for camera %d failed (%s)", cam.slot, type(exc).__name__
                )
                self._stats[cam.slot].model_errors += 1
                continue
            self._threads[cam.slot] = self._spawn_camera(cam.slot)
        self._feed.start()

    def _stop_cameras(self, timeout: float) -> None:
        self._camera_stop.set()
        for thread in self._threads.values():
            thread.join(timeout)
        self._threads.clear()
        if self._feed is not None:
            self._feed.stop(timeout)
            self._feed = None
        for provider in self._providers.values():
            try:
                provider.close()
            except Exception as exc:  # closing is best effort
                log.warning("closing a landmark provider failed (%s)", type(exc).__name__)
        self._providers.clear()

    def _spawn_camera(self, slot: int) -> threading.Thread:
        stop = self._camera_stop
        thread = threading.Thread(
            target=self._camera_loop, args=(slot, stop), name=f"engine-cam{slot}", daemon=True
        )
        thread.start()
        return thread

    def _spawn_tick(self) -> threading.Thread:
        thread = threading.Thread(target=self._tick_loop, name="engine-tick", daemon=True)
        thread.start()
        return thread

    # --- camera processing ---

    def _camera_loop(self, slot: int, stop: threading.Event) -> None:
        while not stop.is_set():
            feed = self._feed
            if feed is None:
                return
            frame = feed.frames(slot).take(timeout=0.2)
            if frame is None:
                continue
            self.process_frame(frame)

    def process_frame(self, frame: Frame) -> CameraCandidate | None:
        """Run one frame through the model and the pipeline. Never raises.

        Called by the camera threads; tests call it directly.
        """
        slot = frame.camera_slot
        stats = self._stats.setdefault(slot, _CameraStats())
        provider = self._providers.get(slot)
        if provider is None:
            return None
        try:
            result = provider.detect(frame, with_pose=self._pipeline.with_pose)
        except LandmarkError:
            stats.model_errors += 1
            return None
        except Exception as exc:
            stats.model_errors += 1
            log.warning("camera %d landmark error (%s)", slot, type(exc).__name__)
            return None
        try:
            with self._lock:
                candidate = self._pipeline.process_landmarks(result)
        except Exception as exc:
            stats.processing_errors += 1
            log.error("camera %d processing error (%s)", slot, type(exc).__name__)
            return None
        stats.processed.append(frame.ts)
        if candidate is not None:
            stats.last_quality = candidate.quality
        if self._frame_listener is not None:
            try:
                self._frame_listener(frame)
            except Exception as exc:
                log.error("frame listener error (%s)", type(exc).__name__)
        return candidate

    # --- ticking ---

    def _tick_loop(self) -> None:
        while not self._stop.wait(self._tick_interval):
            self.tick()
            try:
                self._supervise()
            except Exception as exc:  # the tick loop must outlive any bug
                log.error("engine supervisor error (%s)", type(exc).__name__)

    def _offline_slots(self) -> set[int]:
        feed = self._feed
        if feed is None:
            return {c.slot for c in self._config.cameras}
        try:
            health = feed.health()
        except Exception:
            return set(feed.slots)
        offline = {
            slot
            for slot, h in health.items()
            if h.status in (CameraStatus.OFFLINE, CameraStatus.STOPPED)
        }
        # Cameras whose model could not load produce no candidates either.
        offline |= {slot for slot in health if slot not in self._providers}
        return offline

    def tick(self) -> PipelineOutput | None:
        """Fuse and advance the station state once. Never raises."""
        try:
            ts, wall = self._clock.monotonic(), self._clock.now()
            offline = self._offline_slots()
            with self._lock:
                out = self._pipeline.tick(ts, wall, offline_slots=offline)
                self._status = out.status
        except Exception as exc:
            self._tick_errors += 1
            log.error("engine tick error (%s)", type(exc).__name__)
            return None
        self._publish(opened=out.opened, closed=out.closed, buckets=out.buckets)
        return out

    def _publish(
        self,
        *,
        opened: Sequence[Episode] = (),
        closed: Sequence[Episode] = (),
        buckets: Sequence[Bucket] = (),
    ) -> None:
        # Closed first: a Sleeping -> Absent change closes one and opens one.
        for episode in closed:
            self._events.put(EpisodeClosed(episode))
        for episode in opened:
            self._events.put(EpisodeOpened(episode))
        for bucket in buckets:
            self._events.put(BucketClosed(bucket))

    def _supervise(self) -> None:
        """Restart camera threads that died (a bug, not a camera fault).

        Skipped while a stop or reconfiguration holds the lifecycle lock: that
        caller may be joining this very thread.
        """
        if not self._lifecycle.acquire(blocking=False):
            return
        try:
            if self._stop.is_set():
                return
            for slot, thread in list(self._threads.items()):
                if not thread.is_alive() and not self._camera_stop.is_set():
                    self._stats[slot].restarts += 1
                    log.error("camera %d processing thread died; restarting", slot)
                    self._threads[slot] = self._spawn_camera(slot)
        finally:
            self._lifecycle.release()

    # --- queries ---

    def scan_decision(self) -> ScanDecision:
        """The scan-time state, from memory. Unknown if not answerable at once."""
        if not self._lock.acquire(timeout=SCAN_QUERY_WAIT_S):
            return UNKNOWN_DECISION
        try:
            return self._pipeline.scan_decision(self._clock.monotonic())
        except Exception as exc:
            log.error("scan decision error (%s)", type(exc).__name__)
            return UNKNOWN_DECISION
        finally:
            self._lock.release()

    def status(self) -> StationStatus:
        """The station state as of the last tick."""
        return self._status

    def open_episode(self) -> Episode | None:
        """The episode in progress (for scan warnings to reference)."""
        with self._lock:
            return self._pipeline.open_episode

    def latest_candidates(self) -> tuple[CameraCandidate, ...]:
        """Per-camera candidates of the last tick (signals for event snapshots)."""
        with self._lock:
            fused = self._pipeline.last_fused
        return fused.candidates if fused is not None else ()

    def drain_events(self) -> list[EngineEvent]:
        """Take every event published since the last call, in order."""
        events: list[EngineEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def health(self) -> EngineHealth:
        """Health metrics: capture, processing rate, quality, errors, restarts."""
        now = self._clock.monotonic()
        feed = self._feed
        try:
            capture: Mapping[int, CameraHealth] = feed.health() if feed is not None else {}
        except Exception:
            capture = {}
        status = self._status
        with self._lock:
            fused = self._pipeline.last_fused
        candidate_states = {c.slot: c.state for c in fused.candidates} if fused else {}
        cameras = {
            cam.slot: CameraRuntimeHealth(
                slot=cam.slot,
                capture=capture.get(cam.slot),
                processed_fps=self._stats[cam.slot].fps(now) if cam.slot in self._stats else 0.0,
                last_quality=self._stats[cam.slot].last_quality
                if cam.slot in self._stats
                else None,
                candidate=candidate_states.get(cam.slot, AttentionState.UNKNOWN),
                model_errors=self._stats[cam.slot].model_errors if cam.slot in self._stats else 0,
                processing_errors=(
                    self._stats[cam.slot].processing_errors if cam.slot in self._stats else 0
                ),
                thread_restarts=self._stats[cam.slot].restarts if cam.slot in self._stats else 0,
            )
            for cam in self._config.cameras
        }
        return EngineHealth(
            running=self._running,
            config_version=self._config.config_version,
            status=status,
            cameras=cameras,
            tick_errors=self._tick_errors,
            config_rejections=self._rejections,
            pending_events=self._events.qsize(),
        )

    # --- commands ---

    def reset_session(self) -> None:
        """A new worker session: forget per-person baselines."""
        with self._lock:
            self._pipeline.reset_session()

    def apply_config(self, config: EngineConfig | Mapping[str, Any]) -> bool:
        """Validate and apply a new config between frames.

        Returns False (and keeps the old config) when it is invalid. Cameras
        are restarted only if their own settings changed.
        """
        try:
            new = (
                config
                if isinstance(config, EngineConfig)
                else EngineConfig.model_validate(dict(config))
            )
            # Validate everything that can fail before touching the pipeline.
            StationPipeline(new)
        except (ValidationError, ValueError) as exc:
            self._rejections += 1
            log.warning("engine config refused (%s)", type(exc).__name__)
            return False
        with self._lifecycle:
            cameras_changed = new.cameras != self._config.cameras
            if cameras_changed and self._running:
                self._stop_cameras(5.0)
            with self._lock:
                self._pipeline.apply_config(new)
                self._config = new
            if cameras_changed and self._running:
                self._start_cameras(new)
        return True
