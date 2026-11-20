"""Threaded engine runtime: wiring, isolation, scan query, config swap."""

import threading
import time
from collections.abc import Sequence
from datetime import timedelta

import pytest
from ag_shared.config import CameraConfig
from ag_shared.states import AttentionState, ScanOutcome
from ag_shared.thresholds import Thresholds
from ag_workstation.capture import CameraStatus, CaptureManager, FakeCamera
from ag_workstation.capture.types import Frame
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.engine.fakes import (
    T0,
    StubFeed,
    engine_config,
    face_result,
    frame,
)
from ag_workstation.engine.runtime import (
    BucketClosed,
    Engine,
    EpisodeClosed,
    EpisodeOpened,
)
from ag_workstation.landmarks import FakeLandmarkProvider, LandmarkError
from ag_workstation.landmarks.types import LandmarkResult

A = AttentionState


class World:
    """Scripted cameras: the EAR each camera currently shows, or a failure."""

    def __init__(self) -> None:
        self.ear = {1: 0.30, 2: 0.30}
        self.fail: set[int] = set()
        self.feeds: list[StubFeed] = []

    def provider(self, cam: CameraConfig) -> FakeLandmarkProvider:
        def script(f: Frame, with_pose: bool) -> LandmarkResult:
            if f.camera_slot in self.fail:
                raise LandmarkError("scripted failure")
            return face_result(f, ear=self.ear[f.camera_slot])

        return FakeLandmarkProvider(script)

    def feed(self, cameras: Sequence[CameraConfig]) -> StubFeed:
        feed = StubFeed(cameras)
        self.feeds.append(feed)
        return feed


def _engine(world: World, clock: FakeClock, **kwargs: object) -> Engine:
    return Engine(
        engine_config(),
        provider_factory=world.provider,
        feed_factory=world.feed,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]  # test passes optional keywords through
    )


def _clock() -> FakeClock:
    return FakeClock(start_utc=T0)


def _run(engine: Engine, clock: FakeClock, seconds: float, slots: Sequence[int] = (1, 2)) -> None:
    """Deterministic drive: one frame per camera and one tick every 0.1 s."""
    for _ in range(round(seconds * 10)):
        ts = clock.monotonic()
        seq = round(ts * 10) + 1
        for slot in slots:
            engine.process_frame(frame(slot, seq, ts))
        engine.tick()
        clock.advance(0.1)


def test_frames_to_state_and_events_in_order() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        _run(engine, clock, 6.0)
        assert engine.status().state is A.ATTENTIVE
        world.ear = {1: 0.05, 2: 0.05}
        _run(engine, clock, 5.0)
        assert engine.status().state is A.SLEEPING
        decision = engine.scan_decision()
        assert (decision.state, decision.outcome) == (A.SLEEPING, ScanOutcome.WARNED)
        assert engine.open_episode() is not None
        assert len(engine.latest_candidates()) == 2
        world.ear = {1: 0.30, 2: 0.30}
        _run(engine, clock, 3.0)
        assert engine.status().state is A.ATTENTIVE
    finally:
        engine.stop()
    events = engine.drain_events()
    kinds = [type(e) for e in events if not isinstance(e, BucketClosed)]
    assert kinds == [EpisodeOpened, EpisodeClosed]
    assert any(isinstance(e, BucketClosed) for e in events)  # flushed on stop
    assert engine.drain_events() == []


def test_failing_model_on_one_camera_is_isolated() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        _run(engine, clock, 1.0)
        world.fail = {1}
        world.ear[2] = 0.05
        _run(engine, clock, 5.0)
        # Camera 1 went stale (Unknown); camera 2 alone decides.
        assert engine.status().state is A.SLEEPING
        health = engine.health()
        assert health.cameras[1].model_errors >= 49
        assert health.cameras[1].candidate is A.UNKNOWN
        assert health.cameras[2].model_errors == 0
        assert health.cameras[2].processed_fps == pytest.approx(10.0, rel=0.05)
        assert health.running
    finally:
        engine.stop()
    assert not engine.health().running


def test_provider_that_cannot_load_leaves_other_cameras_running() -> None:
    world, clock = World(), _clock()

    def provider(cam: CameraConfig) -> FakeLandmarkProvider:
        if cam.slot == 1:
            raise RuntimeError("model file missing")
        return world.provider(cam)

    engine = Engine(
        engine_config(), provider_factory=provider, feed_factory=world.feed, clock=clock
    )
    engine.start()
    try:
        _run(engine, clock, 1.0)
        assert engine.status().state is A.ATTENTIVE
        assert engine.health().cameras[1].model_errors == 1
        out = engine.tick()
        assert out is not None
        assert out.fused.source_slots == (2,)
    finally:
        engine.stop()


def test_offline_capture_is_excluded_and_all_offline_is_unavailable() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        _run(engine, clock, 1.0)
        feed = world.feeds[-1]
        feed.status = {1: CameraStatus.OFFLINE, 2: CameraStatus.OFFLINE}
        out = engine.tick()
        assert out is not None
        assert out.status.state is A.UNKNOWN
        assert out.status.monitoring_unavailable
    finally:
        engine.stop()


def test_processing_errors_and_listener_errors_are_contained() -> None:
    world, clock = World(), _clock()
    seen: list[int] = []

    def listener(f: Frame) -> None:
        seen.append(f.seq)
        raise RuntimeError("ring full")

    engine = _engine(world, clock, frame_listener=listener)
    engine.start()
    try:
        _run(engine, clock, 0.5, slots=(1,))
        assert seen == [1, 2, 3, 4, 5]

        def broken(*_: object) -> None:
            raise RuntimeError("bug")

        engine._pipeline.process_landmarks = broken  # type: ignore[method-assign,assignment]
        assert engine.process_frame(frame(1, 98, clock.monotonic())) is None
        assert engine.health().cameras[1].processing_errors == 1
        # Frames for a camera without a provider are ignored.
        assert engine.process_frame(frame(3, 1, 0.0)) is None
    finally:
        engine.stop()


def test_tick_errors_are_counted_not_raised() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine._pipeline.tick = None  # type: ignore[method-assign,assignment]  # broken on purpose
    assert engine.tick() is None
    assert engine.health().tick_errors == 1


def test_scan_query_is_fast_and_never_blocks() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        _run(engine, clock, 1.0)
        started = time.perf_counter()
        for _ in range(1000):
            engine.scan_decision()
        assert (time.perf_counter() - started) / 1000 < 0.005
        # Hold the engine lock as a stuck worker would: the query answers Unknown.
        with engine._lock:
            started = time.perf_counter()
            decision = engine.scan_decision()
            waited = time.perf_counter() - started
        assert decision.state is A.UNKNOWN
        assert decision.outcome is ScanOutcome.UNMONITORED
        assert waited < 0.2
    finally:
        engine.stop()


def test_scan_decision_error_answers_unknown() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine._pipeline.scan_decision = None  # type: ignore[method-assign,assignment]
    assert engine.scan_decision().state is A.UNKNOWN


def test_invalid_config_is_refused_and_old_one_stays() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        before = engine.config
        assert not engine.apply_config({"cameras": [], "profile": {"kind": "open_face"}})
        bad_tz = before.model_dump(mode="json") | {"timezone": "Nowhere/Land"}
        assert not engine.apply_config(bad_tz)
        assert engine.config is before
        assert engine.health().config_rejections == 2
    finally:
        engine.stop()


def test_threshold_change_applies_without_restarting_cameras() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        _run(engine, clock, 6.0)
        new = engine.config.model_copy(
            update={"thresholds": Thresholds(sleeping_eyes_closed_s=1.0), "config_version": 2}
        )
        assert engine.apply_config(new)
        assert len(world.feeds) == 1
        world.ear = {1: 0.05, 2: 0.05}
        _run(engine, clock, 1.5)
        assert engine.status().state is A.SLEEPING
        assert engine.health().config_version == 2
    finally:
        engine.stop()


def test_camera_change_restarts_capture() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        assert engine.apply_config(engine_config(cameras=1))
        assert len(world.feeds) == 2
        assert world.feeds[0].stopped and world.feeds[1].started
        _run(engine, clock, 1.0, slots=(1,))
        assert engine.status().state is A.ATTENTIVE
        assert list(engine.health().cameras) == [1]
    finally:
        engine.stop()


def test_dead_camera_thread_is_restarted() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.start()
    try:
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        engine._threads[1] = dead
        engine._supervise()
        assert engine._threads[1].is_alive()
        assert engine.health().cameras[1].thread_restarts == 1
    finally:
        engine.stop()


def test_start_and_stop_are_idempotent() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock)
    engine.stop()
    engine.start()
    engine.start()
    assert len(world.feeds) == 1
    engine.stop()
    engine.stop()


def test_threaded_engine_with_real_capture_threads() -> None:
    """Real threads end to end: FakeCamera -> CaptureManager -> Engine."""
    cams = {1: FakeCamera(fps=50), 2: FakeCamera(fps=50)}

    def feed(cameras: Sequence[CameraConfig]) -> CaptureManager:
        return CaptureManager(
            cameras, source_factory=lambda c: cams[c.slot].make_source(), stale_after_s=0.5
        )

    thresholds = Thresholds(unknown_seconds=0.6, gap_tolerance_s=0.1, recovery_seconds=0.3)
    engine = Engine(
        engine_config(thresholds=thresholds),
        provider_factory=lambda cam: FakeLandmarkProvider(),
        feed_factory=feed,
        clock=SystemClock(),
        tick_interval_s=0.05,
    )
    engine.start()
    try:
        deadline = time.monotonic() + 3.0
        while engine.status().state is not A.ATTENTIVE and time.monotonic() < deadline:
            time.sleep(0.05)
        assert engine.status().state is A.ATTENTIVE
        cams[1].failing = True
        time.sleep(1.2)
        health = engine.health()
        assert health.cameras[2].processed_fps > 5
        assert health.cameras[1].candidate is A.UNKNOWN
        assert engine.status().state is A.ATTENTIVE  # camera 2 carries on
    finally:
        engine.stop()
    since = engine.status().since
    assert since is not None
    assert since - T0 > timedelta(0)


def test_stop_is_prompt_while_the_tick_thread_runs() -> None:
    world, clock = World(), _clock()
    engine = _engine(world, clock, tick_interval_s=0.01)
    engine.start()
    time.sleep(0.1)
    started = time.perf_counter()
    engine.stop()
    assert time.perf_counter() - started < 1.0
