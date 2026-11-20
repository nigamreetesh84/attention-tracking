"""A scriptable, deterministic stand-in for the engine runtime (PLAN 1.8).

`FakeStationEngine` implements `StationEngine`. Tests set the state it
reports, queue scripted answers for the next scan queries (a state, an
exception, or a delay), take it down and up, accept or refuse configs, and
drive episodes and buckets into a listener. It only blocks when a `Delayed`
step is scripted, and then through the injected `sleep`.
"""

import contextlib
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from ag_shared.config import EngineConfig
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.ids import Uuid7Generator
from ag_shared.states import AttentionState

from ag_workstation.clock import Clock, FakeClock
from ag_workstation.engine_api import (
    BurstKind,
    CameraHealthSummary,
    CameraState,
    ConfigApplyResult,
    EngineEventListener,
    EngineHealth,
    EngineTimeoutError,
    EngineUnavailableError,
    EvidenceSink,
    StateBucket,
    StateEpisode,
    StationState,
)
from ag_workstation.fakes.evidence import BurstBuilder

FAKE_ENGINE_VERSION = "fake-engine-1"


@dataclass(frozen=True, slots=True)
class Delayed:
    """A scripted step: wait `seconds` (via the injected sleep), then answer.

    `then` is a state or exception to answer with; None means the normal answer.
    """

    seconds: float
    then: StationState | BaseException | None = None


ScriptStep = StationState | BaseException | Delayed


@dataclass(frozen=True, slots=True)
class BurstRequest:
    """A recorded `request_burst` call."""

    event_id: UUID
    kind: BurstKind
    trigger_ts: float


@dataclass
class _Setting:
    state: AttentionState = AttentionState.ATTENTIVE
    confidence: float | None = 0.9
    low_confidence: bool = False
    signals: Mapping[str, float | None] = field(default_factory=dict)
    cameras: tuple[CameraState, ...] = ()


class FakeStationEngine:
    """Implements `StationEngine` for tests. Thread-safe."""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        cameras: tuple[CameraState, ...] | None = None,
        engine_version: str = FAKE_ENGINE_VERSION,
        config_version: int = 0,
        sleep: Callable[[float], None] = time.sleep,
        sink: EvidenceSink | None = None,
        burst_builder: BurstBuilder | None = None,
        listener: EngineEventListener | None = None,
        ids: Uuid7Generator | None = None,
    ) -> None:
        self._clock = clock or FakeClock()
        self._lock = threading.Lock()
        self._setting = _Setting(
            cameras=cameras
            if cameras is not None
            else (CameraState(1, CameraStatus.ONLINE, AttentionState.ATTENTIVE, 0.9, True),)
        )
        self._script: deque[ScriptStep] = deque()
        self._down: BaseException | None = None
        self._sleep = sleep
        self._sink = sink
        self._builder = burst_builder or BurstBuilder()
        self._listener = listener
        self._ids = ids or Uuid7Generator(now=self._clock.now)
        self._engine_version = engine_version
        self._config_version = config_version
        self._reject_reason: str | None = None
        self._open_episode: StateEpisode | None = None
        self.restarts = 0
        self.scan_queries = 0
        self.current_queries = 0
        self.applied_configs: list[EngineConfig] = []
        self.burst_requests: list[BurstRequest] = []

    # -- scripting -------------------------------------------------------

    def set_state(
        self,
        state: AttentionState,
        *,
        confidence: float | None = 0.9,
        low_confidence: bool = False,
        signals: Mapping[str, float | None] | None = None,
        cameras: tuple[CameraState, ...] | None = None,
    ) -> None:
        """Set what both queries answer from now on (unless scripted)."""
        with self._lock:
            self._setting = _Setting(
                state=state,
                confidence=confidence,
                low_confidence=low_confidence,
                signals=dict(signals or {}),
                cameras=self._setting.cameras if cameras is None else cameras,
            )

    def script(self, *steps: ScriptStep) -> None:
        """Queue answers for the next `scan_state` calls, in order."""
        with self._lock:
            self._script.extend(steps)

    def fail_next(self, error: BaseException | None = None, times: int = 1) -> None:
        """The next `times` scan queries raise `error` (default: unavailable)."""
        for _ in range(times):
            self.script(error or EngineUnavailableError("engine not running"))

    def timeout_next(self, times: int = 1) -> None:
        """The next `times` scan queries raise `EngineTimeoutError`."""
        for _ in range(times):
            self.script(EngineTimeoutError("no answer in time"))

    def hang_next(self, seconds: float, then: StationState | BaseException | None = None) -> None:
        """The next scan query sleeps `seconds` before answering."""
        self.script(Delayed(seconds, then))

    def go_down(self, error: BaseException | None = None) -> None:
        """Every query raises until `come_up` (a crashed engine)."""
        with self._lock:
            self._down = error or EngineUnavailableError("engine not running")

    def come_up(self) -> None:
        """Back after a restart; counts the restart."""
        with self._lock:
            self._down = None
            self.restarts += 1

    def reject_next_config(self, reason: str = "invalid config") -> None:
        """The next `apply_config` is refused."""
        with self._lock:
            self._reject_reason = reason

    # -- StationStateQuery -----------------------------------------------

    def _answer(self) -> StationState:
        s = self._setting
        return StationState(
            state=s.state,
            confidence=s.confidence,
            low_confidence=s.low_confidence,
            at=self._clock.now(),
            ts=self._clock.monotonic(),
            engine_version=self._engine_version,
            config_version=self._config_version,
            signals=s.signals,
            camera_states=s.cameras,
        )

    def scan_state(self) -> StationState:
        """Scripted step if any, else the set state."""
        with self._lock:
            self.scan_queries += 1
            if self._down is not None:
                raise self._down
            step: ScriptStep | None = self._script.popleft() if self._script else None
        if isinstance(step, Delayed):
            self._sleep(step.seconds)
            step = step.then
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, StationState):
            return step
        with self._lock:
            return self._answer()

    def current_state(self) -> StationState:
        """The set state (scripts apply to scan queries only)."""
        with self._lock:
            self.current_queries += 1
            if self._down is not None:
                raise self._down
            return self._answer()

    # -- EngineHealthSource ----------------------------------------------

    def health(self) -> EngineHealth:
        """Health derived from the configured cameras and the up/down state."""
        with self._lock:
            cameras = tuple(
                CameraHealthSummary(
                    slot=c.slot,
                    status=c.status,
                    fps=12.0 if c.status is CameraStatus.ONLINE else 0.0,
                    quality=c.quality,
                )
                for c in self._setting.cameras
            )
            if self._down is not None:
                status = EngineStatus.DOWN
            elif any(c.status is not CameraStatus.ONLINE for c in cameras):
                status = EngineStatus.DEGRADED
            else:
                status = EngineStatus.RUNNING
            return EngineHealth(
                status=status,
                cameras=cameras,
                restarts=self.restarts,
                engine_version=self._engine_version,
                config_version=self._config_version,
                last_error=type(self._down).__name__ if self._down is not None else None,
            )

    # -- EngineConfigTarget ----------------------------------------------

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        """Accept (and record) `config`, unless a rejection was scripted."""
        with self._lock:
            if self._reject_reason is not None:
                reason, self._reject_reason = self._reject_reason, None
                return ConfigApplyResult(False, self._config_version, reason)
            self.applied_configs.append(config)
            self._config_version = config.config_version
            return ConfigApplyResult(True, config.config_version)

    @property
    def config(self) -> EngineConfig | None:
        """The last accepted config."""
        with self._lock:
            return self.applied_configs[-1] if self.applied_configs else None

    # -- EvidenceRequester -----------------------------------------------

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        """Record the request and, with a sink, deliver a built burst at once.

        Like the real engine, a failing sink is swallowed here.
        """
        with self._lock:
            self.burst_requests.append(BurstRequest(event_id, kind, trigger_ts))
        if self._sink is None:
            return
        burst = self._builder.build(event_id, kind, self._clock.now(), trigger_ts)
        # Mirrors the engine: a failing sink loses images, never the caller.
        with contextlib.suppress(Exception):
            self._sink.submit(burst)

    # -- episodes and buckets --------------------------------------------

    def open_episode(self, state: AttentionState, at: datetime | None = None) -> StateEpisode:
        """Emit `episode_opened` for `state` (closing any open episode first)."""
        if self._open_episode is not None:
            self.close_episode(at)
        episode = StateEpisode(
            id=self._ids(),
            state=state,
            started_at=at or self._clock.now(),
            confidence=self._setting.confidence,
            low_confidence=self._setting.low_confidence,
            engine_version=self._engine_version,
            config_version=self._config_version,
            signals=self._setting.signals,
            camera_states=self._setting.cameras,
        )
        self._open_episode = episode
        if self._listener is not None:
            self._listener.episode_opened(episode)
        return episode

    def close_episode(self, at: datetime | None = None) -> StateEpisode | None:
        """Emit `episode_closed` for the open episode, if any."""
        episode, self._open_episode = self._open_episode, None
        if episode is None:
            return None
        closed = replace(episode, ended_at=at or self._clock.now())
        if self._listener is not None:
            self._listener.episode_closed(closed)
        return closed

    def close_bucket(
        self, bucket_start: datetime, seconds: Mapping[AttentionState, float]
    ) -> StateBucket:
        """Emit `bucket_closed`."""
        bucket = StateBucket(bucket_start=bucket_start, seconds=seconds)
        if self._listener is not None:
            self._listener.bucket_closed(bucket)
        return bucket
