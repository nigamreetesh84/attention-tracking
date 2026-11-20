"""The engine fakes later Phase 2 tests rely on: scripting, failures, configs, bursts."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from ag_shared.config import CameraConfig, CameraKind, CameraRole, CaptureConfig, EngineConfig
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.ids import is_uuid7
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState
from ag_workstation.clock import FakeClock
from ag_workstation.engine_api import (
    BurstKind,
    CameraState,
    EngineTimeoutError,
    EngineUnavailableError,
    EvidenceSink,
    FrameRole,
    StateBucket,
    StateEpisode,
    StationEngine,
    StationState,
)
from ag_workstation.fakes import (
    FAKE_ENGINE_VERSION,
    BurstBuilder,
    BurstRequest,
    FakeStationEngine,
    RecordingEvidenceSink,
)

A = AttentionState
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
EVENT = UUID("0193a0b0-0000-7000-8000-0000000000e1")


def _engine(**kw: object) -> tuple[FakeStationEngine, FakeClock]:
    clock = FakeClock(start_monotonic=100.0, start_utc=T0)
    return FakeStationEngine(clock, **kw), clock  # type: ignore[arg-type]  # test passthrough


def _config(version: int) -> EngineConfig:
    return EngineConfig(
        cameras=(CameraConfig(slot=1, role=CameraRole.PRIMARY, kind=CameraKind.USB, uri="0"),),
        profile=builtin_profile(PpeProfileKind.MASK),
        config_version=version,
    )


def test_fake_satisfies_the_station_engine_protocol() -> None:
    engine: StationEngine = FakeStationEngine()
    sink: EvidenceSink = RecordingEvidenceSink()
    assert engine is not None
    assert sink is not None


def test_default_answer_uses_the_clock_and_set_state() -> None:
    engine, clock = _engine()
    first = engine.scan_state()
    assert first.state is A.ATTENTIVE
    assert (first.at, first.ts) == (T0, 100.0)
    assert first.engine_version == FAKE_ENGINE_VERSION
    clock.advance(2.5)
    engine.set_state(A.SLEEPING, confidence=0.95, low_confidence=True, signals={"ear": None})
    now = engine.current_state()
    assert now.state is A.SLEEPING
    assert now.low_confidence
    assert now.signals == {"ear": None}
    assert now.ts == 102.5
    assert (engine.scan_queries, engine.current_queries) == (1, 1)


def test_scripted_answers_come_in_order_then_fall_back() -> None:
    engine, _ = _engine()
    scripted = StationState(A.DROWSY, 0.7, False, T0, 1.0, "x", 1)
    engine.script(scripted)
    engine.fail_next()
    engine.timeout_next()
    assert engine.scan_state() is scripted
    with pytest.raises(EngineUnavailableError):
        engine.scan_state()
    with pytest.raises(EngineTimeoutError):
        engine.scan_state()
    assert engine.scan_state().state is A.ATTENTIVE
    engine.fail_next(RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        engine.scan_state()


def test_hang_uses_the_injected_sleep() -> None:
    slept: list[float] = []
    engine, _ = _engine(sleep=slept.append)
    engine.hang_next(0.3)
    assert engine.scan_state().state is A.ATTENTIVE
    engine.hang_next(1.0, EngineTimeoutError("late"))
    with pytest.raises(EngineTimeoutError):
        engine.scan_state()
    assert slept == [0.3, 1.0]


def test_hang_with_the_real_sleep_is_measurable() -> None:
    engine, _ = _engine()
    engine.hang_next(0.01)
    assert engine.scan_state().state is A.ATTENTIVE


def test_down_and_up_drive_queries_and_health() -> None:
    engine, _ = _engine()
    assert engine.health().status is EngineStatus.RUNNING
    engine.go_down()
    with pytest.raises(EngineUnavailableError):
        engine.scan_state()
    with pytest.raises(EngineUnavailableError):
        engine.current_state()
    health = engine.health()
    assert health.status is EngineStatus.DOWN
    assert health.last_error == "EngineUnavailableError"
    assert not health.monitoring_available
    engine.come_up()
    assert engine.scan_state().state is A.ATTENTIVE
    assert engine.health().restarts == 1


def test_camera_states_feed_health() -> None:
    cams = (
        CameraState(1, CameraStatus.ONLINE, A.ATTENTIVE, 0.9, True),
        CameraState(2, CameraStatus.OFFLINE, A.UNKNOWN),
    )
    engine, _ = _engine(cameras=cams)
    health = engine.health()
    assert health.status is EngineStatus.DEGRADED
    assert [(c.slot, c.fps) for c in health.cameras] == [(1, 12.0), (2, 0.0)]
    assert engine.scan_state().camera_states == cams
    engine.set_state(A.UNKNOWN, cameras=(CameraState(1, CameraStatus.OFFLINE, A.UNKNOWN),))
    assert not engine.health().monitoring_available


def test_config_apply_and_rejection() -> None:
    engine, _ = _engine()
    assert engine.config is None
    result = engine.apply_config(_config(4))
    assert (result.accepted, result.config_version) == (True, 4)
    assert engine.scan_state().config_version == 4
    engine.reject_next_config("bad cameras")
    refused = engine.apply_config(_config(5))
    assert (refused.accepted, refused.config_version, refused.reason) == (False, 4, "bad cameras")
    assert engine.config == _config(4)
    assert engine.apply_config(_config(5)).accepted
    assert [c.config_version for c in engine.applied_configs] == [4, 5]


def test_request_burst_records_and_delivers() -> None:
    sink = RecordingEvidenceSink()
    engine, _ = _engine(sink=sink, burst_builder=BurstBuilder(secondary_slots=(2,)))
    engine.request_burst(EVENT, BurstKind.SCAN_WARNING, 100.0)
    assert engine.burst_requests == [BurstRequest(EVENT, BurstKind.SCAN_WARNING, 100.0)]
    (burst,) = sink.bursts
    assert burst.event_id == EVENT
    assert burst.complete
    assert len(burst.frames) == 6


def test_request_burst_without_sink_and_with_failing_sink() -> None:
    engine, _ = _engine()
    engine.request_burst(EVENT, BurstKind.EPISODE_KEYFRAME, 1.0)
    assert len(engine.burst_requests) == 1
    sink = RecordingEvidenceSink(fail_with=OSError("disk"))
    engine, _ = _engine(sink=sink)
    engine.request_burst(EVENT, BurstKind.EPISODE_KEYFRAME, 1.0)  # must not raise
    assert sink.bursts == []


class _Listener:
    def __init__(self) -> None:
        self.events: list[tuple[str, StateEpisode | StateBucket]] = []

    def episode_opened(self, episode: StateEpisode) -> None:
        self.events.append(("open", episode))

    def episode_closed(self, episode: StateEpisode) -> None:
        self.events.append(("close", episode))

    def bucket_closed(self, bucket: StateBucket) -> None:
        self.events.append(("bucket", bucket))


def test_episodes_and_buckets_reach_the_listener() -> None:
    listener = _Listener()
    engine, clock = _engine(listener=listener)
    first = engine.open_episode(A.DISTRACTED)
    assert is_uuid7(first.id)
    clock.advance(6)
    second = engine.open_episode(A.SLEEPING)  # closes the first
    clock.advance(10)
    closed = engine.close_episode()
    assert engine.close_episode() is None
    engine.close_bucket(T0, {A.ATTENTIVE: 44.0, A.SLEEPING: 16.0})
    kinds = [k for k, _ in listener.events]
    assert kinds == ["open", "close", "open", "close", "bucket"]
    closed_first = listener.events[1][1]
    assert isinstance(closed_first, StateEpisode)
    assert closed_first.id == first.id
    assert closed_first.ended_at == T0 + timedelta(seconds=6)
    assert closed is not None
    assert closed.id == second.id
    assert closed.ended_at == T0 + timedelta(seconds=16)
    assert first.id < second.id


def test_episode_helpers_work_without_listener() -> None:
    engine, _ = _engine()
    engine.open_episode(A.ABSENT)
    assert engine.close_episode() is not None
    assert engine.close_bucket(T0, {}).seconds == {}


def test_burst_builder_layout_and_missing_frames() -> None:
    capture = CaptureConfig(pre_frames=2, post_frames=1, frame_interval_ms=500)
    builder = BurstBuilder(capture, primary_slot=1, secondary_slots=(2, 3))
    assert builder.layout(BurstKind.SCAN_WARNING) == (
        (1, FrameRole.PRE, -1000),
        (1, FrameRole.PRE, -500),
        (1, FrameRole.KEY, 0),
        (1, FrameRole.POST, 500),
        (2, FrameRole.KEY, 0),
        (3, FrameRole.KEY, 0),
    )
    burst = builder.build(EVENT, BurstKind.SCAN_WARNING, T0, 5.0, missing={(1, 500), (3, 0)})
    assert burst.expected_frames == 6
    assert burst.missing_frames == 2
    pre = burst.frames[0]
    assert pre.captured_at == T0 - timedelta(seconds=1)
    assert len({f.sha256 for f in burst.frames}) == len(burst.frames)
    again = builder.build(EVENT, BurstKind.SCAN_WARNING, T0, 5.0, missing={(1, 500), (3, 0)})
    assert again == burst  # deterministic
    key = builder.build(EVENT, BurstKind.EPISODE_KEYFRAME, T0)
    assert [(f.camera_slot, f.role) for f in key.frames] == [(1, FrameRole.KEY)]
