"""The real engine behind the workstation seam (`ag_workstation.engine_api`).

`EngineAdapter` makes the threaded `Engine` (PLAN 1.8) a `StationEngine`:

- `scan_state` / `current_state` map `ScanDecision` / `StationStatus` to a
  `StationState`. The engine's own bounded wait (100 ms) stays in force; a
  missing, stopped, stalled or unconfigured engine answers Unknown, and the
  IPC client still enforces the 150 ms deadline on top.
- `health` maps the runtime health to `engine_api.EngineHealth`, with one
  summary per configured camera. A camera whose landmark model could not be
  loaded is reported offline (it produces no state) and `last_error` carries
  a health flag code such as `model_unavailable`. A running engine whose
  tick has stalled (thread dead or hung: no tick for `unknown_seconds`, or
  none at all `STARTUP_GRACE_S` after start) is `DOWN` with `tick_stalled`,
  so monitoring is shown unavailable while the cameras still deliver.
- `alive` is the engine child's liveness: False once the tick has stalled
  for `TICK_STALL_RESTART_S` (or never ticked within the start-up grace).
  The child then stops its heartbeat and the supervisor restarts it within
  seconds (scope 4.4, PROGRESS 2.11 fix). Unconfigured, starting and
  stopped engines are alive: a restart would not help them.
- `apply_config` maps the engine's bool to `ConfigApplyResult` and never
  raises. With no engine yet (the child started without a camera config) the
  first accepted config builds and starts one.
- `request_burst` asks the `BurstAssembler` for a scan burst or an episode
  key frame; it never raises into the caller.
- A pump thread drains the engine's events (`drain_events`), turns episodes
  and buckets into `StateEpisode` / `StateBucket` for the
  `EngineEventListener`, requests the key frame of Sleeping and Absent
  episodes (detection-engine section 7), and lets overdue bursts go.

No worker identity reaches the engine or this adapter; session ids are
attached by the listener that turns events into queue records.
"""

import logging
import threading
from collections.abc import Callable, Collection, Iterable
from datetime import timedelta
from functools import partial
from importlib import metadata
from typing import Final
from uuid import UUID

from ag_shared.config import CameraRole, EngineConfig
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState

from ag_workstation.clock import Clock
from ag_workstation.engine.runtime import (
    BucketClosed,
    Engine,
    EpisodeClosed,
    EpisodeOpened,
)
from ag_workstation.engine.runtime import EngineHealth as RuntimeHealth
from ag_workstation.engine.state_machine import Bucket, Episode
from ag_workstation.engine.types import CameraCandidate
from ag_workstation.engine_api import (
    BurstKind,
    CameraHealthSummary,
    CameraState,
    ConfigApplyResult,
    EngineEventListener,
    EngineHealth,
    SignalSummary,
    StateBucket,
    StateEpisode,
    StationState,
)
from ag_workstation.evidence.burst import BurstAssembler

logger = logging.getLogger(__name__)


def _engine_version() -> str:
    try:
        return f"ag-engine/{metadata.version('attentionguard')}"
    except metadata.PackageNotFoundError:  # running from a source tree
        return "ag-engine/0"


ENGINE_VERSION: Final = _engine_version()
KEYFRAME_STATES: Final = frozenset({AttentionState.SLEEPING, AttentionState.ABSENT})
"""Episodes that save one key frame (detection-engine section 7)."""
PUMP_INTERVAL_S: Final = 0.1

FLAG_NOT_CONFIGURED: Final = "not_configured"
"""No camera config yet: the station is Unknown until one is applied."""
FLAG_MODEL_UNAVAILABLE: Final = "model_unavailable"
"""A landmark model could not be loaded (missing file, bad AG_MODEL_DIR)."""
FLAG_ENGINE_START_FAILED: Final = "engine_start_failed"
FLAG_TICK_STALLED: Final = "tick_stalled"
"""The engine runs but its tick thread stopped: the state is stale (Unknown)."""
TICK_STALL_RESTART_S: Final = 3.0
"""A tick this old makes the child stop heartbeating (the tick runs every
0.1 s; 3 s leaves room for a scheduling hiccup without a false restart).
With the supervisor's 2 s heartbeat timeout the restart follows about 5 s
after the tick stopped."""
STARTUP_GRACE_S: Final = 10.0
"""A started engine must tick within this. Model loading happens before the
engine counts as started, so this only covers thread start-up."""

EngineBuilder = Callable[[EngineConfig], Engine]
"""Builds (does not start) an `Engine` for a config; wires the frame listener."""


def _unit(value: float | None) -> float | None:
    if value is None:
        return None
    return min(1.0, max(0.0, float(value)))


def _signals(candidates: Iterable[CameraCandidate]) -> SignalSummary:
    """The deciding camera's signals: the usable one with the best quality."""
    ranked = sorted(
        (c for c in candidates if c.frame is not None),
        key=lambda c: (not c.usable, -(c.quality or 0.0), c.slot),
    )
    if not ranked or ranked[0].frame is None:
        return {}
    frame = ranked[0].frame
    return {name.value: frame.value(name) for name in frame.signals}


class _NoListener:
    def episode_opened(self, episode: StateEpisode) -> None:
        pass

    def episode_closed(self, episode: StateEpisode) -> None:
        pass

    def bucket_closed(self, bucket: StateBucket) -> None:
        pass


_NO_LISTENER: Final = _NoListener()


class EngineAdapter:
    """A `StationEngine` over the real `Engine`. Thread-safe; never raises."""

    def __init__(
        self,
        builder: EngineBuilder,
        *,
        clock: Clock,
        config: EngineConfig | None = None,
        assembler: BurstAssembler | None = None,
        listener: EngineEventListener | None = None,
        unavailable_slots: Callable[[], Collection[int]] | None = None,
        engine_version: str = ENGINE_VERSION,
        pump_interval_s: float = PUMP_INTERVAL_S,
    ) -> None:
        """`unavailable_slots` names cameras whose landmark model failed to load."""
        self._builder = builder
        self._clock = clock
        self._initial = config
        self._assembler = assembler
        self._listener = listener
        self._unavailable = unavailable_slots
        self._version = engine_version[:64]
        self._interval = pump_interval_s
        self._lock = threading.RLock()
        self._engine: Engine | None = None
        self._engine_started_at = 0.0
        self._started = False
        self._start_failed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._camera_status: dict[int, CameraStatus] = {}
        self.listener_errors = 0
        self.pump_errors = 0

    # -- lifecycle -----------------------------------------------------------

    @property
    def engine(self) -> Engine | None:
        """The running engine, if configured."""
        return self._engine

    @property
    def config(self) -> EngineConfig | None:
        """The active config (None while unconfigured)."""
        engine = self._engine
        return engine.config if engine is not None else self._initial

    def start(self) -> None:
        """Build and start the engine (if configured) and the event pump."""
        with self._lock:
            if self._started:
                return
            self._started = True
            if self._initial is not None:
                self._build_and_start(self._initial)
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="engine-pump", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the pump and the engine; deliver what is pending. Never raises."""
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop.set()
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        engine = self._engine
        if engine is not None:
            try:
                engine.stop(timeout)
            except Exception as exc:
                logger.error("engine stop failed (%s)", type(exc).__name__)
        self.pump()
        if self._assembler is not None:
            try:
                self._assembler.flush()
            except Exception as exc:
                logger.error("burst flush failed (%s)", type(exc).__name__)

    def _build_and_start(self, config: EngineConfig) -> bool:
        try:
            engine = self._builder(config)
            engine.start()
        except Exception as exc:
            self._start_failed = True
            logger.error("engine could not start (%s)", type(exc).__name__)
            return False
        self._engine_started_at = self._clock.monotonic()
        self._engine = engine
        self._start_failed = False
        return True

    # -- StationStateQuery ---------------------------------------------------

    def _unknown(self) -> StationState:
        config = self.config
        return StationState.unknown(
            self._clock.now(),
            self._clock.monotonic(),
            engine_version=self._version,
            config_version=config.config_version if config is not None else 0,
        )

    def scan_state(self) -> StationState:
        """The section 5 scan decision as a `StationState`; Unknown on any failure."""
        try:
            engine = self._engine
            if engine is None:
                return self._unknown()
            decision = engine.scan_decision()
            if decision.state is AttentionState.UNKNOWN:
                return self._unknown()
            candidates = engine.latest_candidates()
            return StationState(
                state=decision.state,
                confidence=_unit(decision.confidence),
                low_confidence=decision.low_confidence,
                at=self._clock.now(),
                ts=self._clock.monotonic(),
                engine_version=self._version,
                config_version=engine.config.config_version,
                signals=_signals(candidates),
                camera_states=self._camera_states(candidates, live=True),
            )
        except Exception as exc:
            logger.error("scan state failed (%s)", type(exc).__name__)
            return self._unknown()

    def current_state(self) -> StationState:
        """The station state of the last tick; Unknown when stale or stopped."""
        try:
            engine = self._engine
            if engine is None or not engine.is_current():
                return self._unknown()
            status = engine.status()
            candidates = engine.latest_candidates()
            return StationState(
                state=status.state,
                confidence=_unit(status.confidence),
                low_confidence=status.low_confidence,
                at=self._clock.now(),
                ts=self._clock.monotonic(),
                engine_version=self._version,
                config_version=engine.config.config_version,
                signals=_signals(candidates),
                camera_states=self._camera_states(candidates, live=True),
            )
        except Exception as exc:
            logger.error("current state failed (%s)", type(exc).__name__)
            return self._unknown()

    def on_break(self) -> bool:
        """Whether the engine reports On Break (from memory, for the capture gate)."""
        engine = self._engine
        return engine is not None and engine.status().state is AttentionState.ON_BREAK

    def _camera_states(
        self, candidates: Iterable[CameraCandidate], *, live: bool = False
    ) -> tuple[CameraState, ...]:
        """Per-camera snapshots. `live` (a state answer now): a camera that is
        not online is shown Unknown and unusable, as fusion already treats it,
        instead of its last candidate (kept for up to `unknown_seconds`)."""
        statuses = dict(self._camera_status)
        out = []
        for c in sorted(candidates, key=lambda c: c.slot):
            if not 1 <= c.slot <= 4:
                continue
            status = statuses.get(c.slot, CameraStatus.ONLINE)
            if live and status is not CameraStatus.ONLINE:
                out.append(CameraState(slot=c.slot, status=status, state=AttentionState.UNKNOWN))
                continue
            out.append(
                CameraState(
                    slot=c.slot,
                    status=status,
                    state=c.state,
                    quality=_unit(c.quality),
                    usable=c.usable,
                )
            )
        return tuple(out)

    def _online(self, slot: int) -> bool:
        return self._camera_status.get(slot, CameraStatus.ONLINE) is CameraStatus.ONLINE

    # -- EngineHealthSource --------------------------------------------------

    def _unavailable_slots(self) -> set[int]:
        if self._unavailable is None:
            return set()
        try:
            return set(self._unavailable())
        except Exception:
            return set()

    def health(self) -> EngineHealth:
        """Health mapped to `engine_api.EngineHealth`; never raises."""
        config = self.config
        version = config.config_version if config is not None else 0
        engine = self._engine
        try:
            if engine is None:
                flag = FLAG_ENGINE_START_FAILED if self._start_failed else FLAG_NOT_CONFIGURED
                status = EngineStatus.DEGRADED if self._started else EngineStatus.STARTING
                return EngineHealth(
                    status=status,
                    engine_version=self._version,
                    config_version=version,
                    last_error=flag,
                )
            health = self._map_health(engine.health())
            # Stale exactly when the state answers turn Unknown (`is_current`).
            if self._stalled(engine, engine.config.thresholds.unknown_seconds):
                return EngineHealth(
                    status=EngineStatus.DOWN,
                    cameras=health.cameras,
                    restarts=health.restarts,
                    engine_version=health.engine_version,
                    config_version=health.config_version,
                    last_error=FLAG_TICK_STALLED,
                )
            return health
        except Exception as exc:
            logger.error("engine health failed (%s)", type(exc).__name__)
            return EngineHealth(
                status=EngineStatus.UNKNOWN,
                engine_version=self._version,
                config_version=version,
                last_error=type(exc).__name__,
            )

    def _stalled(self, engine: Engine, limit: float) -> bool:
        """A running engine whose last tick is older than `limit`, or that has
        not ticked at all within `STARTUP_GRACE_S` of its start."""
        if not engine.running:
            return False
        age = engine.tick_age()
        if age is None:
            return self._clock.monotonic() - self._engine_started_at > STARTUP_GRACE_S
        return age > limit

    def alive(self) -> bool:
        """False while the tick has stalled long enough to restart the child.

        Never raises; a failing check answers True (a broken probe must not
        restart a working child; `health` reports the failure)."""
        try:
            engine = self._engine
            return engine is None or not self._stalled(engine, TICK_STALL_RESTART_S)
        except Exception as exc:
            logger.error("engine liveness check failed (%s)", type(exc).__name__)
            return True

    def _map_health(self, rt: RuntimeHealth) -> EngineHealth:
        no_model = self._unavailable_slots()
        cameras: list[CameraHealthSummary] = []
        degraded = False
        for slot in sorted(rt.cameras):
            cam = rt.cameras[slot]
            if not 1 <= slot <= 4:
                continue
            status = cam.capture.status if cam.capture is not None else CameraStatus.OFFLINE
            if slot in no_model:
                status = CameraStatus.OFFLINE  # captures, but cannot produce a state
            if status is not CameraStatus.ONLINE or cam.low_frame_rate:
                degraded = True
            cameras.append(
                CameraHealthSummary(
                    slot=slot,
                    status=status,
                    fps=max(0.0, cam.processed_fps),
                    quality=_unit(cam.last_quality),
                )
            )
        if not rt.running:
            status_ = EngineStatus.DOWN
        elif degraded or not cameras:
            status_ = EngineStatus.DEGRADED
        else:
            status_ = EngineStatus.RUNNING
        return EngineHealth(
            status=status_,
            cameras=tuple(cameras),
            restarts=sum(c.thread_restarts for c in rt.cameras.values()),
            engine_version=self._version,
            config_version=rt.config_version,
            last_error=FLAG_MODEL_UNAVAILABLE if no_model else None,
        )

    # -- EngineConfigTarget --------------------------------------------------

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        """Apply between frames; refused configs keep the old one. Never raises."""
        try:
            with self._lock:
                engine = self._engine
                if engine is None:
                    if not self._started:
                        self._initial = config
                        return ConfigApplyResult(True, config.config_version)
                    if self._build_and_start(config):
                        return ConfigApplyResult(True, config.config_version)
                    return ConfigApplyResult(False, 0, FLAG_ENGINE_START_FAILED)
                accepted = engine.apply_config(config)
                active = engine.config.config_version
                return ConfigApplyResult(accepted, active, None if accepted else "invalid config")
        except Exception as exc:
            logger.error("config apply failed (%s)", type(exc).__name__)
            current = self.config
            return ConfigApplyResult(
                False, current.config_version if current is not None else 0, type(exc).__name__
            )

    def reset_session(self) -> None:
        """A new worker session: forget per-person baselines. Never raises."""
        engine = self._engine
        if engine is None:
            return
        try:
            engine.reset_session()
        except Exception as exc:
            logger.error("session reset failed (%s)", type(exc).__name__)

    # -- EvidenceRequester ---------------------------------------------------

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        """Ask the assembler for a burst. Never raises; a failure drops the burst."""
        try:
            self._request(event_id, kind, trigger_ts)
        except Exception as exc:
            logger.error("burst request failed (%s)", type(exc).__name__)

    def _request(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        assembler, engine = self._assembler, self._engine
        if assembler is None or engine is None:
            logger.info("evidence burst not taken: no engine or no evidence path")
            return
        config = engine.config
        primary = next(c.slot for c in config.cameras if c.role is CameraRole.PRIMARY)
        at = self._clock.now() - timedelta(seconds=self._clock.monotonic() - trigger_ts)
        if kind is BurstKind.EPISODE_KEYFRAME:
            assembler.request_keyframe(event_id, trigger_ts, primary_slot=primary, triggered_at=at)
            return
        secondaries = tuple(
            c.slot
            for c in engine.latest_candidates()
            if c.usable and c.slot != primary and self._online(c.slot)
        )
        assembler.request_scan_burst(
            event_id,
            trigger_ts,
            primary_slot=primary,
            secondary_slots=secondaries,
            triggered_at=at,
        )

    # -- events ----------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.pump()

    def pump(self) -> None:
        """Drain engine events to the listener, poll bursts, cache camera status."""
        try:
            self._pump()
        except Exception as exc:  # the pump must outlive any bug
            self.pump_errors += 1
            logger.error("engine event pump error (%s)", type(exc).__name__)

    def _pump(self) -> None:
        engine = self._engine
        if engine is None:
            return
        for event in engine.drain_events():
            match event:
                case EpisodeOpened(episode=ep):
                    state_episode = self._episode(ep, engine)
                    if ep.state in KEYFRAME_STATES:
                        self.request_burst(ep.id, BurstKind.EPISODE_KEYFRAME, ep.started_ts)
                    self._notify(partial(self._listen().episode_opened, state_episode))
                case EpisodeClosed(episode=ep):
                    closed = self._episode(ep, engine)
                    self._notify(partial(self._listen().episode_closed, closed))
                case BucketClosed(bucket=b):
                    bucket = self._bucket(b)
                    if bucket is not None:
                        self._notify(partial(self._listen().bucket_closed, bucket))
        if self._assembler is not None:
            self._assembler.poll(self._clock.monotonic())
        self._refresh_camera_status(engine)

    def _listen(self) -> EngineEventListener:
        return self._listener if self._listener is not None else _NO_LISTENER

    def _notify(self, call: Callable[[], None]) -> None:
        if self._listener is None:
            return
        try:
            call()
        except Exception as exc:
            self.listener_errors += 1
            logger.error("engine event listener error (%s)", type(exc).__name__)

    def _refresh_camera_status(self, engine: Engine) -> None:
        try:
            health = engine.health()
        except Exception:
            return
        no_model = self._unavailable_slots()
        self._camera_status = {
            slot: (
                CameraStatus.OFFLINE
                if cam.capture is None or slot in no_model
                else cam.capture.status
            )
            for slot, cam in health.cameras.items()
        }

    def _episode(self, ep: Episode, engine: Engine) -> StateEpisode:
        return StateEpisode(
            id=ep.id,
            state=ep.state,
            started_at=ep.started_at,
            confidence=_unit(ep.confidence),
            low_confidence=ep.low_confidence,
            engine_version=self._version,
            config_version=engine.config.config_version,
            ended_at=ep.ended_at,
            signals=_signals(ep.candidates),
            camera_states=self._camera_states(ep.candidates),
        )

    def _bucket(self, b: Bucket) -> StateBucket | None:
        try:
            return StateBucket(bucket_start=b.bucket_start, seconds=dict(b.seconds))
        except ValueError as exc:
            logger.error("bucket dropped (%s)", type(exc).__name__)
            return None
