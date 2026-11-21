"""The real `Engine` behind the `engine_api` seam (2.9-pre): `EngineAdapter`."""

from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

import pytest
from ag_shared.config import CameraConfig, EngineConfig
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.capture.types import Frame, Image
from ag_workstation.clock import FakeClock
from ag_workstation.engine.adapter import (
    FLAG_ENGINE_START_FAILED,
    FLAG_MODEL_UNAVAILABLE,
    FLAG_NOT_CONFIGURED,
    EngineAdapter,
)
from ag_workstation.engine.fakes import T0, StubFeed, engine_config, face_result, frame
from ag_workstation.engine.runtime import Engine
from ag_workstation.engine_api import (
    BurstKind,
    EvidenceBurst,
    FrameRole,
    StateBucket,
    StateEpisode,
    StationEngine,
)
from ag_workstation.evidence import BurstAssembler, EvidenceRings, MemorySink
from ag_workstation.landmarks import FakeLandmarkProvider, LandmarkError
from ag_workstation.landmarks.types import LandmarkResult
from ag_workstation.service.engine_factory import FailOpenProviderFactory

A = AttentionState
EVENT = UUID(int=42)


def fake_jpeg(image: Image, quality: int) -> bytes:
    return b"\xff\xd8" + bytes([quality]) + b"\xff\xd9"


class Listener:
    def __init__(self, fail: bool = False) -> None:
        self.opened: list[StateEpisode] = []
        self.closed: list[StateEpisode] = []
        self.buckets: list[StateBucket] = []
        self.fail = fail

    def episode_opened(self, episode: StateEpisode) -> None:
        if self.fail:
            raise RuntimeError("listener bug")
        self.opened.append(episode)

    def episode_closed(self, episode: StateEpisode) -> None:
        self.closed.append(episode)

    def bucket_closed(self, bucket: StateBucket) -> None:
        self.buckets.append(bucket)


class World:
    def __init__(self) -> None:
        self.ear = {1: 0.30, 2: 0.30}
        self.feeds: list[StubFeed] = []
        self.no_model: set[int] = set()

    def provider(self, cam: CameraConfig) -> FakeLandmarkProvider:
        if cam.slot in self.no_model:
            raise LandmarkError("model file not found")

        def script(f: Frame, with_pose: bool) -> LandmarkResult:
            return face_result(f, ear=self.ear[f.camera_slot])

        return FakeLandmarkProvider(script)

    def feed(self, cameras: Sequence[CameraConfig]) -> StubFeed:
        feed = StubFeed(cameras)
        self.feeds.append(feed)
        return feed


class Rig:
    """An adapter over a real `Engine` with a stub feed, fake landmarks and real rings."""

    def __init__(
        self,
        *,
        config: EngineConfig | None = None,
        listener: Listener | None = None,
        configured: bool = True,
    ) -> None:
        self.world = World()
        self.clock = FakeClock(start_utc=T0)
        self.sink = MemorySink()
        cfg = config or engine_config()
        self.rings = EvidenceRings((1, 2, 3, 4), cfg.capture, fake_jpeg)
        self.assembler = BurstAssembler(
            self.rings,
            cfg.capture,
            self.sink,
            wall_clock=self.clock.now,
            mono_clock=self.clock.monotonic,
        )
        self.providers = FailOpenProviderFactory(self.world.provider)
        self.listener = listener or Listener()
        self.adapter = EngineAdapter(
            self.build,
            clock=self.clock,
            config=cfg if configured else None,
            assembler=self.assembler,
            listener=self.listener,
            unavailable_slots=self.providers.failed_slots,
            engine_version="test-engine",
            pump_interval_s=3600.0,  # the test pumps by hand
        )

    def build(self, cfg: EngineConfig) -> Engine:
        return Engine(
            cfg,
            provider_factory=self.providers,
            feed_factory=self.world.feed,
            clock=self.clock,
            tick_interval_s=3600.0,  # the test ticks by hand
            frame_listener=self.rings.offer,
        )

    def run(self, seconds: float, slots: Sequence[int] = (1, 2)) -> None:
        engine = self.adapter.engine
        assert engine is not None
        for _ in range(round(seconds * 10)):
            ts = self.clock.monotonic()
            seq = round(ts * 10) + 1
            for slot in slots:
                engine.process_frame(frame(slot, seq, ts))
            engine.tick()
            self.adapter.pump()
            self.clock.advance(0.1)


def test_adapter_is_a_station_engine() -> None:
    rig = Rig()
    engine: StationEngine = rig.adapter  # static check: the protocol is satisfied
    assert engine is rig.adapter


def test_scan_and_current_state_map_the_engine_answers() -> None:
    rig = Rig()
    rig.adapter.start()
    try:
        rig.run(6.0)
        state = rig.adapter.scan_state()
        assert state.state is A.ATTENTIVE
        assert state.outcome is ScanOutcome.ATTENTIVE
        assert state.engine_version == "test-engine"
        assert state.config_version == rig.adapter.config.config_version  # type: ignore[union-attr]  # configured rig
        assert [c.slot for c in state.camera_states] == [1, 2]
        assert all(c.status is CameraStatus.ONLINE and c.usable for c in state.camera_states)
        assert state.signals["face_present"] == 1.0
        assert "ear" in state.signals
        assert state.at == rig.clock.now()
        assert rig.adapter.current_state().state is A.ATTENTIVE
        rig.world.ear = {1: 0.05, 2: 0.05}
        rig.run(5.0)
        state = rig.adapter.scan_state()
        assert (state.state, state.outcome) == (A.SLEEPING, ScanOutcome.WARNED)
        assert state.confidence is not None and 0.0 <= state.confidence <= 1.0
    finally:
        rig.adapter.stop()


def test_unknown_when_stopped_stale_or_unconfigured() -> None:
    rig = Rig(configured=False)
    assert rig.adapter.scan_state().state is A.UNKNOWN
    assert rig.adapter.current_state().state is A.UNKNOWN
    health = rig.adapter.health()
    assert (health.status, health.last_error) == (EngineStatus.STARTING, FLAG_NOT_CONFIGURED)
    rig.adapter.start()
    health = rig.adapter.health()
    assert (health.status, health.last_error) == (EngineStatus.DEGRADED, FLAG_NOT_CONFIGURED)
    assert not health.monitoring_available
    assert rig.adapter.scan_state().state is A.UNKNOWN
    rig.adapter.request_burst(EVENT, BurstKind.SCAN_WARNING, 0.0)  # no engine: dropped quietly
    assert rig.sink.bursts == []

    # The first config builds and starts the engine.
    result = rig.adapter.apply_config(engine_config(config_version=4))
    assert result.accepted and result.config_version == 4
    rig.run(6.0)
    assert rig.adapter.scan_state().state is A.ATTENTIVE

    # Stale: no tick for longer than unknown_seconds.
    rig.clock.advance(30.0)
    assert rig.adapter.scan_state().state is A.UNKNOWN
    assert rig.adapter.current_state().state is A.UNKNOWN
    rig.run(1.0)
    rig.adapter.stop()
    assert rig.adapter.scan_state().state is A.UNKNOWN
    assert rig.adapter.current_state().state is A.UNKNOWN
    assert rig.adapter.health().status is EngineStatus.DOWN
    rig.adapter.stop()  # idempotent


def test_config_before_start_is_kept_and_build_failure_is_reported() -> None:
    rig = Rig(configured=False)
    assert rig.adapter.apply_config(engine_config(config_version=2)).accepted
    assert rig.adapter.config is not None and rig.adapter.config.config_version == 2

    def broken(cfg: EngineConfig) -> Engine:
        raise RuntimeError("cannot build")

    failing = EngineAdapter(broken, clock=FakeClock(), config=engine_config())
    failing.start()
    try:
        assert failing.scan_state().state is A.UNKNOWN
        assert failing.health().last_error == FLAG_ENGINE_START_FAILED
        result = failing.apply_config(engine_config(config_version=3))
        assert not result.accepted and result.reason == FLAG_ENGINE_START_FAILED
    finally:
        failing.stop()


def test_health_maps_cameras_and_flags_a_missing_model() -> None:
    rig = Rig()
    rig.world.no_model = {2}
    rig.adapter.start()
    try:
        rig.run(3.0)
        health = rig.adapter.health()
        assert health.status is EngineStatus.DEGRADED
        assert health.last_error == FLAG_MODEL_UNAVAILABLE
        by_slot = {c.slot: c for c in health.cameras}
        assert by_slot[1].status is CameraStatus.ONLINE and by_slot[1].fps > 5.0
        assert by_slot[2].status is CameraStatus.OFFLINE  # captures, but no state
        assert health.monitoring_available  # camera 1 still monitors
        assert health.engine_version == "test-engine"
        # Camera 1 alone still gives a state.
        assert rig.adapter.scan_state().state is A.ATTENTIVE
        # Capture offline is mapped too.
        rig.world.feeds[-1].status[1] = CameraStatus.OFFLINE
        health = rig.adapter.health()
        assert {c.slot: c.status for c in health.cameras}[1] is CameraStatus.OFFLINE
        assert not health.monitoring_available
    finally:
        rig.adapter.stop()


def test_all_models_missing_is_unknown_not_a_crash() -> None:
    rig = Rig()
    rig.world.no_model = {1, 2}
    rig.adapter.start()
    try:
        rig.run(3.0)
        assert rig.adapter.scan_state().state is A.UNKNOWN
        health = rig.adapter.health()
        assert health.last_error == FLAG_MODEL_UNAVAILABLE
        assert not health.monitoring_available
    finally:
        rig.adapter.stop()


def test_apply_config_maps_the_result_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig()
    rig.adapter.start()
    try:
        ok = rig.adapter.apply_config(engine_config(config_version=7))
        assert ok.accepted and ok.config_version == 7
        engine = rig.adapter.engine
        assert engine is not None
        monkeypatch.setattr(engine, "apply_config", lambda config: False)
        refused = rig.adapter.apply_config(engine_config(config_version=8))
        assert (refused.accepted, refused.config_version) == (False, 7)
        assert refused.reason

        def boom(config: object) -> bool:
            raise RuntimeError("bug")

        monkeypatch.setattr(engine, "apply_config", boom)
        failed = rig.adapter.apply_config(engine_config(config_version=9))
        assert (failed.accepted, failed.config_version, failed.reason) == (
            False,
            7,
            "RuntimeError",
        )
    finally:
        rig.adapter.stop()


def test_engine_query_failures_answer_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig()
    rig.adapter.start()
    try:
        rig.run(6.0)
        engine = rig.adapter.engine
        assert engine is not None

        def boom() -> object:
            raise RuntimeError("bug")

        monkeypatch.setattr(engine, "scan_decision", boom)
        monkeypatch.setattr(engine, "status", boom)
        monkeypatch.setattr(engine, "health", boom)
        assert rig.adapter.scan_state().state is A.UNKNOWN
        assert rig.adapter.current_state().state is A.UNKNOWN
        assert rig.adapter.health().status is EngineStatus.UNKNOWN
        rig.adapter.reset_session()  # still fine
    finally:
        monkeypatch.undo()
        rig.adapter.stop()


def test_episodes_and_buckets_reach_the_listener_with_a_key_frame() -> None:
    rig = Rig()
    rig.adapter.start()
    try:
        rig.run(6.0)
        rig.world.ear = {1: 0.05, 2: 0.05}
        rig.run(6.0)
        assert [e.state for e in rig.listener.opened] == [A.SLEEPING]
        opened = rig.listener.opened[0]
        assert opened.ended_at is None and opened.engine_version == "test-engine"
        assert opened.signals and opened.camera_states
        # Sleeping saves one key frame, under the episode's id.
        keyframes = [b for b in rig.sink.bursts if b.kind is BurstKind.EPISODE_KEYFRAME]
        assert [b.event_id for b in keyframes] == [opened.id]
        assert [f.role for f in keyframes[0].frames] == [FrameRole.KEY]
        rig.world.ear = {1: 0.30, 2: 0.30}
        rig.run(4.0)
        assert [e.id for e in rig.listener.closed] == [opened.id]
        assert rig.listener.closed[0].ended_at is not None
        rig.clock.advance(60.0)
        rig.run(0.5)
    finally:
        rig.adapter.stop()
    assert rig.listener.buckets  # minutes closed (and flushed on stop)
    assert all(sum(b.seconds.values()) <= 60.001 for b in rig.listener.buckets)


def test_listener_errors_are_contained() -> None:
    rig = Rig(listener=Listener(fail=True))
    rig.adapter.start()
    try:
        rig.run(6.0)
        rig.world.ear = {1: 0.05, 2: 0.05}
        rig.run(6.0)
        assert rig.adapter.listener_errors == 1
        assert rig.adapter.scan_state().state is A.SLEEPING
    finally:
        rig.adapter.stop()


def test_scan_burst_through_the_real_assembler() -> None:
    rig = Rig()
    rig.adapter.start()
    try:
        rig.run(6.0)
        state = rig.adapter.scan_state()
        rig.adapter.request_burst(EVENT, BurstKind.SCAN_WARNING, state.ts)
        assert rig.sink.bursts == []  # waiting for post frames
        rig.run(2.0)
        (burst,) = [b for b in rig.sink.bursts if b.event_id == EVENT]
        assert isinstance(burst, EvidenceBurst)
        assert burst.kind is BurstKind.SCAN_WARNING
        assert burst.trigger_ts == state.ts
        assert burst.triggered_at == T0 + timedelta(seconds=state.ts)
        roles = [(f.camera_slot, f.role) for f in burst.frames]
        assert roles == [
            (1, FrameRole.PRE),
            (1, FrameRole.PRE),
            (1, FrameRole.KEY),
            (1, FrameRole.POST),
            (1, FrameRole.POST),
            (2, FrameRole.KEY),  # the usable secondary's key frame
        ]
        assert burst.complete and burst.missing == ()
        assert all(len(f.sha256) == 64 for f in burst.frames)
    finally:
        rig.adapter.stop()


def test_request_burst_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig()
    rig.adapter.start()
    try:
        rig.run(1.0)

        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("bug")

        monkeypatch.setattr(rig.assembler, "request_scan_burst", boom)
        rig.adapter.request_burst(EVENT, BurstKind.SCAN_WARNING, 0.5)
        no_evidence = EngineAdapter(rig.build, clock=rig.clock, config=engine_config())
        no_evidence.start()
        no_evidence.request_burst(EVENT, BurstKind.EPISODE_KEYFRAME, 0.5)
        no_evidence.stop()
    finally:
        rig.adapter.stop()


def test_on_break_reads_the_engine_status() -> None:
    rig = Rig(configured=False)
    assert not rig.adapter.on_break()
    rig.adapter.start()
    rig.adapter.apply_config(engine_config())
    try:
        rig.run(1.0)
        assert not rig.adapter.on_break()
    finally:
        rig.adapter.stop()
