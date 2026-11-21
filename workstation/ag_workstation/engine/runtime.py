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
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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

HANG_AFTER_S = 10.0
"""A camera thread stuck in one frame for this long is abandoned and replaced."""

MAX_ABANDONED_THREADS = 2
"""Per camera; a Python thread cannot be killed, so abandoned ones are capped."""

LOW_FPS = 2.0
"""Processed frames per second below which a camera is flagged in health."""

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
    low_frame_rate: bool
    """Online but processed below `LOW_FPS`: detection timing is unreliable."""


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
        self.abandoned = 0
        self.last_quality: float | None = None
        self.busy_since: float | None = None
        """Real monotonic time the current frame started processing (hang watchdog)."""
        self.generation = 0

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
        self._last_tick: float | None = None
        """Engine-clock time of the last successful tick."""

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
                self._status = replace(self._status, state=AttentionState.UNKNOWN, confidence=0.0)
            self._publish(closed=closed, buckets=buckets)
            self._running = False
            self._last_tick = None

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
        stats = self._stats.setdefault(slot, _CameraStats())
        stats.busy_since = None
        thread = threading.Thread(
            target=self._camera_loop,
            args=(slot, stop, stats.generation),
            name=f"engine-cam{slot}",
            daemon=True,
        )
        thread.start()
        return thread

    def _spawn_tick(self) -> threading.Thread:
        thread = threading.Thread(target=self._tick_loop, name="engine-tick", daemon=True)
        thread.start()
        return thread

    # --- camera processing ---

    def _camera_loop(self, slot: int, stop: threading.Event, generation: int) -> None:
        stats = self._stats[slot]
        while not stop.is_set() and stats.generation == generation:
            feed = self._feed
            if feed is None:
                return
            frame = feed.frames(slot).take(timeout=0.2)
            if frame is None:
                continue
            self._process(frame, generation)

    def process_frame(self, frame: Frame) -> CameraCandidate | None:
        """Run one frame through the model and the pipeline. Never raises.

        Called by the camera threads; tests call it directly.
        """
        stats = self._stats.setdefault(frame.camera_slot, _CameraStats())
        return self._process(frame, stats.generation)

    def _process(self, frame: Frame, generation: int) -> CameraCandidate | None:
        slot = frame.camera_slot
        stats = self._stats.setdefault(slot, _CameraStats())
        provider = self._providers.get(slot)
        if provider is None:
            return None
        stats.busy_since = time.monotonic()
        try:
            result = provider.detect(frame, with_pose=self._pipeline.with_pose)
        except LandmarkError:
            stats.model_errors += 1
            return None
        except Exception as exc:
            stats.model_errors += 1
            log.warning("camera %d landmark error (%s)", slot, type(exc).__name__)
            return None
        finally:
            if stats.generation == generation:
                stats.busy_since = None
        if stats.generation != generation:
            return None  # this thread was abandoned as hung; its result is stale
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
                self._last_tick = ts
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
            now = time.monotonic()
            for slot, thread in list(self._threads.items()):
                if self._camera_stop.is_set():
                    return
                stats = self._stats[slot]
                if not thread.is_alive():
                    stats.restarts += 1
                    log.error("camera %d processing thread died; restarting", slot)
                    self._threads[slot] = self._spawn_camera(slot)
                elif (
                    stats.busy_since is not None
                    and now - stats.busy_since > HANG_AFTER_S
                    and stats.abandoned < MAX_ABANDONED_THREADS
                ):
                    self._replace_hung(slot, stats)
        finally:
            self._lifecycle.release()

    def _replace_hung(self, slot: int, stats: _CameraStats) -> None:
        """Abandon a camera thread stuck in the model and start a fresh one."""
        stats.abandoned += 1
        stats.restarts += 1
        stats.generation += 1
        log.error("camera %d processing hung; starting a new thread", slot)
        camera = next(c for c in self._config.cameras if c.slot == slot)
        try:
            self._providers[slot] = self._provider_factory(camera)
        except Exception as exc:
            stats.model_errors += 1
            log.error("landmark provider for camera %d failed (%s)", slot, type(exc).__name__)
            self._providers.pop(slot, None)
            return
        self._threads[slot] = self._spawn_camera(slot)

    # --- queries ---

    def scan_decision(self) -> ScanDecision:
        """The scan-time state, from memory. Unknown if not answerable at once.

        Also Unknown when the engine is stopped or its last tick is older than
        `unknown_seconds`: a stale state must not pass for a current one.
        """
        if not self._running:
            return UNKNOWN_DECISION
        if not self._lock.acquire(timeout=SCAN_QUERY_WAIT_S):
            return UNKNOWN_DECISION
        try:
            now = self._clock.monotonic()
            last = self._last_tick
            if last is None or now - last > self._pipeline.thresholds.unknown_seconds:
                return UNKNOWN_DECISION
            return self._pipeline.scan_decision(now)
        except Exception as exc:
            log.error("scan decision error (%s)", type(exc).__name__)
            return UNKNOWN_DECISION
        finally:
            self._lock.release()

    def status(self) -> StationStatus:
        """The station state as of the last tick."""
        return self._status

    @property
    def running(self) -> bool:
        """Whether the engine was started and not stopped."""
        return self._running

    def is_current(self) -> bool:
        """Running, with a tick no older than `unknown_seconds` (lock-free).

        `status()` is only meaningful as a current state while this holds."""
        last = self._last_tick
        if not self._running or last is None:
            return False
        try:
            age = self._clock.monotonic() - last
            return age <= self._pipeline.thresholds.unknown_seconds
        except Exception:
            return False

    def tick_age(self) -> float | None:
        """Seconds since the last successful tick; None before the first one
        or while stopped (lock-free, for liveness checks)."""
        last = self._last_tick
        if not self._running or last is None:
            return None
        return self._clock.monotonic() - last

    def open_episode(self) -> Episode | None:
        """The episode in progress (for scan warnings to reference).

        None if the engine lock is not free within the scan-query wait.
        """
        if not self._lock.acquire(timeout=SCAN_QUERY_WAIT_S):
            return None
        try:
            return self._pipeline.open_episode
        finally:
            self._lock.release()

    def latest_candidates(self) -> tuple[CameraCandidate, ...]:
        """Per-camera candidates of the last tick (signals for event snapshots).

        Empty if the engine lock is not free within the scan-query wait.
        """
        if not self._lock.acquire(timeout=SCAN_QUERY_WAIT_S):
            return ()
        try:
            fused = self._pipeline.last_fused
        finally:
            self._lock.release()
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

        def low_fps(slot: int, fps: float) -> bool:
            h = capture.get(slot)
            return h is not None and h.status is CameraStatus.ONLINE and fps < LOW_FPS

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
                low_frame_rate=low_fps(
                    cam.slot, self._stats[cam.slot].fps(now) if cam.slot in self._stats else 0.0
                ),
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
