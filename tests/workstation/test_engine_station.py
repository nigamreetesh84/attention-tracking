"""The engine child's real station (2.9-pre): engine -> bursts -> gate -> store, events -> queue."""

import json
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import AttentionEventKind, EventType, ImagesWithheld
from ag_shared.config import CameraConfig
from ag_shared.health import EngineStatus
from ag_shared.states import AttentionState
from ag_workstation.capture.types import Frame, Image
from ag_workstation.clock import FakeClock
from ag_workstation.consent import CaptureOutcome, GateReason
from ag_workstation.engine.adapter import FLAG_MODEL_UNAVAILABLE, FLAG_NOT_CONFIGURED
from ag_workstation.engine.fakes import T0, StubFeed, engine_config, face_result, frame
from ag_workstation.engine_api import BurstKind, StateBucket, StateEpisode
from ag_workstation.evidence import EvidenceStore
from ag_workstation.landmarks import FakeLandmarkProvider, LandmarkError
from ag_workstation.landmarks.types import LandmarkResult
from ag_workstation.queue import EventQueue, InMemoryKeyProvider
from ag_workstation.record import RECORD_FILE, PersonalRecordStore
from ag_workstation.service import PipeTransport, ProcessLauncher
from ag_workstation.service import engine_factory as ef
from ag_workstation.service.child import resolve_factory
from ag_workstation.service.station import (
    MAX_SESSION_TTL_S,
    EngineEventRecorder,
    FanOut,
    OutcomeRegistry,
    PushedSession,
    PushedSessions,
    StationChild,
)

A = AttentionState
KEY = bytes(range(32))
SESSION = UUID("0193a0b0-0000-7000-8000-0000000c0001")
USER = UUID("0193a0b0-0000-7000-8000-0000000c0002")
EVENT = UUID("0193a0b0-0000-7000-8000-0000000c0003")


def fake_jpeg(image: Image, quality: int) -> bytes:
    return b"\xff\xd8\xff\xe0" + bytes([quality]) + b"\xff\xd9"


def _wait(cond: Callable[[], bool], limit: float = 3.0) -> None:
    end = time.monotonic() + limit
    while not cond():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.01)


class World:
    def __init__(self) -> None:
        self.ear = {1: 0.30, 2: 0.30}
        self.no_model: set[int] = set()

    def provider(self, cam: CameraConfig) -> FakeLandmarkProvider:
        if cam.slot in self.no_model:
            raise LandmarkError("model file not found")

        def script(f: Frame, with_pose: bool) -> LandmarkResult:
            return face_result(f, ear=self.ear[f.camera_slot])

        return FakeLandmarkProvider(script)


class Station:
    def __init__(self, tmp: Path, *, config: bool = True, data: bool = True) -> None:
        self.world = World()
        self.clock = FakeClock(start_utc=T0)
        self.tmp = tmp
        env: dict[str, str] = {}
        if config:
            path = tmp / "engine.json"
            path.write_text(engine_config(config_version=5).model_dump_json())
            env[ef.ENGINE_CONFIG_ENV] = str(path)
        if data:
            env[ef.DATA_DIR_ENV] = str(tmp / "data")
        self.child = ef.build_station(
            None,
            env=env,
            clock=self.clock,
            feed_factory=lambda cams: StubFeed(cams),
            provider_factory=self.world.provider,
            encoder=fake_jpeg,
            keys=InMemoryKeyProvider(KEY) if data else None,
            tick_interval_s=3600.0,
            pump_interval_s=3600.0,
        )

    def run(self, seconds: float, slots: Sequence[int] = (1, 2)) -> None:
        engine = self.child.adapter.engine
        assert engine is not None
        for _ in range(round(seconds * 10)):
            ts = self.clock.monotonic()
            seq = round(ts * 10) + 1
            for slot in slots:
                engine.process_frame(frame(slot, seq, ts))
            engine.tick()
            self.child.adapter.pump()
            self.clock.advance(0.1)

    def login(self, *, consent: bool = True, started: timedelta = timedelta(minutes=5)) -> None:
        self.child.set_session(PushedSession(SESSION, USER, T0 - started, consent), 3600.0)

    def records(self) -> list[tuple[str, dict[str, object]]]:
        queue = EventQueue.open(
            self.tmp / "data" / "queue.db", InMemoryKeyProvider(KEY), self.clock
        )
        try:
            return [(r.type, dict(r.payload)) for r in queue.claim_batch()]
        finally:
            queue.close()

    def images(self, event_id: UUID) -> int:
        store = EvidenceStore.open(
            self.tmp / "data" / "evidence", InMemoryKeyProvider(KEY), self.clock
        )
        try:
            return len(store.event_images(event_id))
        finally:
            store.close()


@pytest.fixture
def station(tmp_path: Path) -> Iterator[Station]:
    s = Station(tmp_path)
    yield s
    s.child.close()


def _outcome(child: StationChild, event_id: UUID) -> CaptureOutcome:
    _wait(lambda: child.burst_outcome(event_id).status == "done")
    outcome = child.burst_outcome(event_id).outcome
    assert outcome is not None
    return outcome


def test_warned_scan_burst_is_gated_and_stored_with_consent(station: Station) -> None:
    station.login()
    station.run(6.0)
    station.world.ear = {1: 0.05, 2: 0.05}
    station.run(6.0)
    state = station.child.scan_state()
    assert state.state is A.SLEEPING
    station.child.request_burst(EVENT, BurstKind.SCAN_WARNING, state.ts)
    assert station.child.burst_outcome(EVENT).status == "pending"
    station.run(2.0)
    outcome = _outcome(station.child, EVENT)
    assert outcome.gate_reason is GateReason.OPEN
    assert outcome.images_withheld is ImagesWithheld.NONE
    assert len(outcome.image_ids) == outcome.frames == 6
    station.child.close()
    assert station.images(EVENT) == 6


def test_no_session_means_no_images(station: Station) -> None:
    station.run(6.0)
    station.child.request_burst(EVENT, BurstKind.SCAN_WARNING, station.clock.monotonic() - 1.0)
    station.run(2.0)
    outcome = _outcome(station.child, EVENT)
    assert outcome.gate_reason is GateReason.NO_SESSION
    assert outcome.images_withheld is ImagesWithheld.NO_WORKER
    assert outcome.image_ids == ()
    station.child.close()
    assert station.images(EVENT) == 0
    assert not any((station.tmp / "data" / "evidence").rglob("*.agimg"))


def test_no_consent_and_session_after_the_frames_withhold(station: Station) -> None:
    station.login(consent=False)
    station.run(6.0)
    station.child.request_burst(EVENT, BurstKind.EPISODE_KEYFRAME, station.clock.monotonic() - 0.5)
    assert _outcome(station.child, EVENT).images_withheld is ImagesWithheld.NO_CONSENT
    other = UUID(int=EVENT.int + 1)
    station.login(started=-timedelta(hours=1))  # the session began after these frames
    station.child.request_burst(other, BurstKind.EPISODE_KEYFRAME, station.clock.monotonic() - 0.5)
    assert _outcome(station.child, other).gate_reason is GateReason.SESSION_CHANGED


def test_expired_session_push_closes_the_gate(station: Station) -> None:
    station.child.set_session(PushedSession(SESSION, USER, T0 - timedelta(minutes=5), True), 5.0)
    station.run(6.0)  # 6 s of engine time: the 5 s push has expired
    assert station.child.sessions.current() is None
    station.child.request_burst(EVENT, BurstKind.EPISODE_KEYFRAME, station.clock.monotonic() - 0.5)
    assert _outcome(station.child, EVENT).gate_reason is GateReason.NO_SESSION


def test_episode_and_buckets_are_queued_with_the_session(station: Station) -> None:
    station.login()
    station.run(6.0)
    station.world.ear = {1: 0.05, 2: 0.05}
    station.run(6.0)
    engine = station.child.adapter.engine
    assert engine is not None
    sleeping = engine.open_episode()
    assert sleeping is not None
    # The key frame is stored on the sink's thread; the fake clock must not
    # outrun it past the recorder's wait.
    _wait(lambda: station.child.burst_outcome(sleeping.id).status == "done")
    station.world.ear = {1: 0.30, 2: 0.30}
    station.run(4.0)
    station.clock.advance(60.0)
    station.login()  # the UI re-pushes every second; a push lives at most 60 s
    station.run(0.5)
    station.child.close()
    records = station.records()
    episodes = [p for t, p in records if t == EventType.ATTENTION_EVENT]
    assert episodes[0]["state"] == A.SLEEPING
    # Recovery may pass through a non-key-frame state: no images expected there.
    for later in episodes[1:]:
        assert later["image_ids"] == [] and later["images_withheld"] == ImagesWithheld.NONE
    episode = episodes[0]
    assert episode["kind"] == AttentionEventKind.STATE_EPISODE
    assert episode["state"] == A.SLEEPING
    assert episode["session_id"] == str(SESSION) and episode["user_id"] == str(USER)
    assert episode["ended_at"] is not None
    assert episode["images_withheld"] == ImagesWithheld.NONE
    image_ids = episode["image_ids"]
    assert isinstance(image_ids, list) and len(image_ids) == 1  # the Sleeping key frame
    buckets = [p for t, p in records if t == EventType.BUCKET]
    assert buckets and all(b["user_id"] == str(USER) for b in buckets)
    # The episode id is the attention event id; no names or employee ids anywhere.
    assert "DEV001" not in json.dumps(records)


def test_episodes_also_reach_the_workers_local_record(station: Station) -> None:
    """2.10: the child mirrors its episodes into `my_record.db` (the UI adds the
    session row; here it is added by hand)."""
    station.login()
    station.run(6.0)
    station.world.ear = {1: 0.05, 2: 0.05}
    station.run(6.0)
    engine = station.child.adapter.engine
    assert engine is not None
    sleeping = engine.open_episode()
    assert sleeping is not None
    _wait(lambda: station.child.burst_outcome(sleeping.id).status == "done")
    station.world.ear = {1: 0.30, 2: 0.30}
    station.run(4.0)
    station.child.close()
    record = PersonalRecordStore.open(
        station.tmp / "data" / RECORD_FILE, InMemoryKeyProvider(KEY), station.clock
    )
    try:
        record.note_session_start(SESSION, USER, T0)
        events = record.snapshot(USER).events
        assert sleeping.id in {e.id for e in events}
        shown = next(e for e in events if e.id == sleeping.id)
        assert shown.state is A.SLEEPING and shown.images_saved == 1
        assert record.snapshot(UUID(int=7)).events == ()
    finally:
        record.close()


def test_episodes_without_a_session_are_not_queued(station: Station) -> None:
    station.run(6.0)
    station.world.ear = {1: 0.05, 2: 0.05}
    station.run(6.0)
    station.world.ear = {1: 0.30, 2: 0.30}
    station.run(4.0)
    station.child.close()
    types = [t for t, _ in station.records()]
    assert EventType.ATTENTION_EVENT not in types
    assert EventType.BUCKET in types  # unattributed buckets still count station time


def test_new_session_resets_baselines(station: Station, monkeypatch: pytest.MonkeyPatch) -> None:
    resets: list[int] = []
    monkeypatch.setattr(station.child.adapter, "reset_session", lambda: resets.append(1))
    station.login()
    station.login()  # same session re-pushed: no reset
    station.child.set_session(None, 5.0)
    assert resets == [1]


def test_missing_storage_is_metadata_only(tmp_path: Path) -> None:
    s = Station(tmp_path, data=False)
    try:
        s.login()
        s.run(6.0)
        assert s.child.scan_state().state is A.ATTENTIVE  # detection still works
        s.child.request_burst(EVENT, BurstKind.EPISODE_KEYFRAME, s.clock.monotonic() - 0.5)
        outcome = _outcome(s.child, EVENT)
        # The gate is open but there is nowhere to write: images withheld.
        assert outcome.gate_reason is GateReason.OPEN
        assert outcome.images_withheld is ImagesWithheld.DISK_FULL and outcome.image_ids == ()
    finally:
        s.child.close()
    assert not (tmp_path / "data").exists()


def test_model_absence_gives_unknown_and_a_health_flag(tmp_path: Path) -> None:
    world = World()
    world.no_model = {1, 2}
    path = tmp_path / "engine.json"
    path.write_text(engine_config().model_dump_json())
    child = ef.build_station(
        None,
        env={ef.ENGINE_CONFIG_ENV: str(path)},
        clock=FakeClock(start_utc=T0),
        feed_factory=lambda cams: StubFeed(cams),
        provider_factory=world.provider,
        encoder=fake_jpeg,
        tick_interval_s=3600.0,
        pump_interval_s=3600.0,
    )
    try:
        assert child.scan_state().state is A.UNKNOWN
        health = child.health()
        assert health.last_error == FLAG_MODEL_UNAVAILABLE
        assert not health.monitoring_available
    finally:
        child.close()


def test_real_mediapipe_factory_without_model_dir_flags_and_continues(tmp_path: Path) -> None:
    make = ef.mediapipe_provider_factory({})
    with pytest.raises(LandmarkError):
        make(engine_config().cameras[0])
    make = ef.mediapipe_provider_factory({"AG_MODEL_DIR": str(tmp_path)})
    with pytest.raises(LandmarkError, match="face model"):
        make(engine_config().cameras[0])
    (tmp_path / "face_landmarker.task").write_bytes(b"model")
    provider = make(engine_config().cameras[0])  # loads lazily: no MediaPipe needed here
    provider.close()


def test_default_factory_is_the_real_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_factory(ef.DEFAULT_ENGINE_FACTORY) is ef.real_engine_factory
    launcher = ProcessLauncher.real(PipeTransport("unused", b"k" * 32))
    assert launcher.factory_path == ef.DEFAULT_ENGINE_FACTORY
    for name in (ef.ENGINE_CONFIG_ENV, ef.DATA_DIR_ENV, ef.DATA_KEY_ENV, "AG_MODEL_DIR"):
        monkeypatch.delenv(name, raising=False)
    child = ef.real_engine_factory(FanOut())
    try:
        assert isinstance(child, StationChild)
        assert child.scan_state().state is A.UNKNOWN
        health = child.health()
        assert (health.status, health.last_error) == (EngineStatus.DEGRADED, FLAG_NOT_CONFIGURED)
    finally:
        child.close()


def test_service_host_defaults_to_the_real_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from ag_workstation.service import host as host_module
    from ag_workstation.service import windows_service

    with pytest.raises(ValueError, match="AG_IPC_AUTHKEY"):
        host_module.default_service_host({})
    host = host_module.default_service_host({host_module.AUTHKEY_ENV: "ab" * 32})
    launcher = host.supervisor._launcher  # checking the wiring
    assert isinstance(launcher, ProcessLauncher)
    assert launcher.factory_path == ef.DEFAULT_ENGINE_FACTORY
    assert windows_service.main.__defaults__ == (host_module.default_service_host, None)


def test_engine_config_loading_never_raises(tmp_path: Path) -> None:
    assert ef.load_engine_config({}) is None
    assert ef.load_engine_config({ef.ENGINE_CONFIG_ENV: str(tmp_path / "missing.json")}) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ef.load_engine_config({ef.ENGINE_CONFIG_ENV: str(bad)}) is None
    good = tmp_path / "good.json"
    good.write_text(engine_config(config_version=9).model_dump_json())
    loaded = ef.load_engine_config({ef.ENGINE_CONFIG_ENV: str(good)})
    assert loaded is not None and loaded.config_version == 9


def test_key_provider_from_env(tmp_path: Path) -> None:
    assert ef.key_provider_from_env({ef.DATA_KEY_ENV: "zz"}, None) is None
    keys = ef.key_provider_from_env({ef.DATA_KEY_ENV: KEY.hex()}, None)
    assert keys is not None and keys.data_key() == KEY


def test_fail_open_provider_factory_tracks_slots() -> None:
    world = World()
    factory = ef.FailOpenProviderFactory(world.provider)
    world.no_model = {2}
    cams = engine_config().cameras
    factory(cams[0])
    with pytest.raises(LandmarkError):
        factory(cams[1])
    assert factory.failed_slots() == {2}
    world.no_model = set()
    factory(cams[1])
    assert factory.failed_slots() == frozenset()


# -- small parts --------------------------------------------------------------------------------


def test_pushed_sessions_expire_and_cap_the_ttl() -> None:
    clock = FakeClock()
    sessions = PushedSessions(clock)
    s = PushedSession(SESSION, USER, T0, True)
    assert sessions.set(s, 5.0) is True
    assert sessions.set(s, 5.0) is False
    clock.advance(5.1)
    assert sessions.current() is None
    sessions.set(s, 10_000.0)
    clock.advance(MAX_SESSION_TTL_S + 0.1)
    assert sessions.current() is None
    assert sessions.set(None, 5.0) is False  # already expired: no change


def test_outcome_registry_is_bounded() -> None:
    clock = FakeClock()
    registry = OutcomeRegistry(clock, capacity=2)
    ids = [UUID(int=i + 1) for i in range(3)]
    for event_id in ids:
        registry.requested(event_id)
    assert registry.status(ids[0]).status == "unknown"  # pushed out
    assert registry.status(ids[2]).status == "pending"
    registry.record(CaptureOutcome(ids[2], (), ImagesWithheld.NO_WORKER, GateReason.NO_SESSION, 0))
    assert registry.status(ids[2]).status == "done"
    clock.advance(10_000.0)
    registry.requested(UUID(int=99))
    assert registry.status(ids[2]).status == "unknown"  # aged out


def test_recorder_without_queue_or_with_a_failing_queue_counts_losses() -> None:
    clock = FakeClock(start_utc=T0)
    sessions = PushedSessions(clock)
    sessions.set(PushedSession(SESSION, USER, T0, True), 60.0)
    recorder = EngineEventRecorder(None, sessions, OutcomeRegistry(clock), clock)
    bucket = StateBucket(T0.replace(minute=0), {A.ATTENTIVE: 60.0})
    recorder.bucket_closed(bucket)
    assert recorder.lost == 1

    class Failing:
        def enqueue(self, *args: object, **kwargs: object) -> object:
            raise OSError("disk")

    recorder = EngineEventRecorder(Failing(), sessions, OutcomeRegistry(clock), clock)
    episode = StateEpisode(EVENT, A.ABSENT, T0, 0.9, False, "v", 1)
    recorder.episode_opened(episode)
    recorder.episode_closed(episode)  # Absent with no key frame outcome yet: waits
    assert recorder.lost == 0
    recorder.flush()  # shutdown: queued without images, and the queue fails
    assert recorder.lost == 1


def test_recorder_waits_for_a_late_key_frame_then_gives_up() -> None:
    clock = FakeClock(start_utc=T0)
    sessions = PushedSessions(clock)
    sessions.set(PushedSession(SESSION, USER, T0, True), 60.0)
    queued: list[tuple[str, dict[str, object]]] = []

    class Queue:
        def enqueue(
            self, item_type: str, payload: object, occurred_at: object, **kw: object
        ) -> object:
            queued.append((item_type, payload.model_dump(mode="json")))  # type: ignore[attr-defined]  # a pydantic payload
            return None

    recorder = EngineEventRecorder(Queue(), sessions, OutcomeRegistry(clock), clock)
    late = StateEpisode(EVENT, A.SLEEPING, T0, 0.9, False, "v", 1, ended_at=T0)
    recorder.episode_opened(late)
    recorder.episode_closed(late)
    assert queued == []
    image = UUID(int=5)
    recorder.outcome_arrived(
        CaptureOutcome(EVENT, (image,), ImagesWithheld.NONE, GateReason.OPEN, 1)
    )
    assert queued[0][1]["image_ids"] == [str(image)]

    never = StateEpisode(UUID(int=EVENT.int + 1), A.ABSENT, T0, 0.9, False, "v", 1, ended_at=T0)
    recorder.episode_opened(never)
    recorder.episode_closed(never)
    clock.advance(10.0)
    recorder.bucket_closed(StateBucket(T0.replace(minute=0), {A.ABSENT: 60.0}))
    assert [p["images_withheld"] for t, p in queued if t == EventType.ATTENTION_EVENT] == [
        ImagesWithheld.NONE,
        ImagesWithheld.DISK_FULL,
    ]


def test_fan_out_isolates_listeners() -> None:
    seen: list[str] = []

    class Bad:
        def episode_opened(self, episode: StateEpisode) -> None:
            raise RuntimeError("bug")

        def episode_closed(self, episode: StateEpisode) -> None:
            raise RuntimeError("bug")

        def bucket_closed(self, bucket: StateBucket) -> None:
            raise RuntimeError("bug")

    class Good:
        def episode_opened(self, episode: StateEpisode) -> None:
            seen.append("opened")

        def episode_closed(self, episode: StateEpisode) -> None:
            seen.append("closed")

        def bucket_closed(self, bucket: StateBucket) -> None:
            seen.append("bucket")

    fan = FanOut(Bad(), Good())
    episode = StateEpisode(EVENT, A.ABSENT, T0, 0.9, False, "v", 1)
    fan.episode_opened(episode)
    fan.episode_closed(episode)
    fan.bucket_closed(StateBucket(T0.replace(minute=0), {}))
    assert seen == ["opened", "closed", "bucket"]
