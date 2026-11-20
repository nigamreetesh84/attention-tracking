"""Health snapshot, housekeeping schedule, local-alert sink and the service host."""

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ag_shared.api import EventType, LocalAlertKind, LocalAlertPayload
from ag_shared.health import CameraStatus, EngineStatus
from ag_workstation.clock import FakeClock
from ag_workstation.engine_api import CameraHealthSummary, EngineHealth
from ag_workstation.queue import EventQueue, InMemoryKeyProvider, QueueStats
from ag_workstation.service import (
    EngineSupervisor,
    HealthReporter,
    Housekeeper,
    QueueLocalAlertSink,
    ServiceHost,
    SupervisorConfig,
    SupervisorHealth,
    disk_free_mb_hook,
    standard_housekeeping,
)


class FixedSupervisor:
    def __init__(self, status: EngineStatus, restarts: int = 2) -> None:
        self.status = status
        self.restarts = restarts

    def health(self) -> SupervisorHealth:
        return SupervisorHealth(self.status, self.restarts, "exited", 0, 42)


CAMS = (
    CameraHealthSummary(1, CameraStatus.ONLINE, 12.0, 0.8),
    CameraHealthSummary(2, CameraStatus.OFFLINE),
)
STATS = QueueStats(depth=5, sending=1, oldest_unsent_at=None, dead_letter_count=3)


def _boom() -> QueueStats:
    raise RuntimeError("db locked")


def test_snapshot_merges_supervisor_engine_queue_and_disk() -> None:
    clock = FakeClock()
    reporter = HealthReporter(
        clock,
        FixedSupervisor(EngineStatus.RUNNING),
        engine_health=lambda: EngineHealth(EngineStatus.DEGRADED, CAMS),
        queue_stats=lambda: STATS,
        disk_free_mb=lambda: 1234,
    )
    snap = reporter.snapshot()
    assert snap.taken_at == clock.now()
    assert snap.engine_status is EngineStatus.DEGRADED
    assert (snap.engine_restarts, snap.engine_last_error) == (2, "exited")
    assert snap.cameras == CAMS
    assert snap.queue == STATS
    assert snap.disk_free_mb == 1234
    assert snap.monitoring_available
    payload = snap.to_payload(app_version="1.0.0", clock_offset_ms=-20)
    assert payload.engine_status is EngineStatus.DEGRADED
    assert (payload.queue_depth, payload.dead_letter_count, payload.engine_restarts) == (5, 3, 2)
    assert [c.slot for c in payload.cameras] == [1, 2]
    assert payload.disk_free_mb == 1234


@pytest.mark.parametrize("status", [EngineStatus.RESTARTING, EngineStatus.DOWN])
def test_supervisor_status_wins_when_engine_not_running(status: EngineStatus) -> None:
    calls: list[int] = []

    def engine() -> EngineHealth:
        calls.append(1)
        return EngineHealth(EngineStatus.RUNNING, CAMS)

    snap = HealthReporter(FakeClock(), FixedSupervisor(status), engine_health=engine).snapshot()
    assert snap.engine_status is status
    assert snap.cameras == ()
    assert calls == []  # no IPC call while the engine is known down
    assert not snap.monitoring_available


@pytest.mark.parametrize("answer", [EngineStatus.DOWN, EngineStatus.UNKNOWN, None])
def test_running_process_whose_engine_does_not_answer_is_unknown(
    answer: EngineStatus | None,
) -> None:
    def engine() -> EngineHealth:
        if answer is None:
            raise RuntimeError("ipc broke")
        return EngineHealth(answer)

    snap = HealthReporter(
        FakeClock(), FixedSupervisor(EngineStatus.RUNNING), engine_health=engine
    ).snapshot()
    assert snap.engine_status is EngineStatus.UNKNOWN


def test_missing_and_failing_hooks_blank_their_fields() -> None:
    snap = HealthReporter(
        FakeClock(), FixedSupervisor(EngineStatus.RUNNING), queue_stats=_boom
    ).snapshot()
    assert snap.engine_status is EngineStatus.RUNNING
    assert (snap.queue, snap.disk_free_mb, snap.cameras) == (None, None, ())
    payload = snap.to_payload()
    assert (payload.queue_depth, payload.dead_letter_count) == (0, 0)


def test_disk_free_hook(tmp_path: Path) -> None:
    free = disk_free_mb_hook(tmp_path)()
    assert free is not None and free >= 0


def test_housekeeper_runs_jobs_on_their_interval() -> None:
    clock = FakeClock()
    keeper = Housekeeper(clock)
    runs: list[str] = []
    keeper.add("a", 10.0, lambda: runs.append("a"))
    keeper.add("b", 5.0, lambda: runs.append("b"), run_first=False)
    assert keeper.jobs == ("a", "b")
    assert keeper.tick() == ["a"]
    assert keeper.tick() == []
    clock.advance(5.0)
    assert keeper.tick() == ["b"]
    clock.advance(5.0)
    assert keeper.tick() == ["a", "b"]
    assert runs == ["a", "b", "a", "b"]
    with pytest.raises(ValueError):
        keeper.add("c", 0, lambda: None)


def test_failing_job_is_counted_and_retried_next_interval() -> None:
    clock = FakeClock()
    keeper = Housekeeper(clock)
    attempts: list[int] = []

    def flaky() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("disk")

    keeper.add("flaky", 60.0, flaky)
    assert keeper.tick() == []
    assert keeper.failures == {"flaky": 1}
    clock.advance(60.0)
    assert keeper.tick() == ["flaky"]


class Purger:
    def __init__(self) -> None:
        self.calls: list[timedelta] = []

    def purge_dead_letters(self, retention: timedelta = timedelta(days=30)) -> int:
        self.calls.append(retention)
        return 0


class Evidence:
    def __init__(self) -> None:
        self.calls = 0

    def housekeeping(self) -> object:
        self.calls += 1
        return None


def test_standard_housekeeping_purges_dead_letters_and_old_evidence() -> None:
    clock = FakeClock()
    purger, evidence = Purger(), Evidence()
    keeper = standard_housekeeping(clock, queue=purger, evidence=evidence, interval_s=3600)
    assert keeper.jobs == ("purge_dead_letters", "evidence_housekeeping")
    keeper.tick()
    clock.advance(1800)
    keeper.tick()
    clock.advance(1800)
    keeper.tick()
    assert (len(purger.calls), evidence.calls) == (2, 2)
    assert standard_housekeeping(clock).jobs == ()


def test_standard_housekeeping_with_real_queue(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=datetime(2026, 1, 1, tzinfo=UTC))
    with EventQueue.open(tmp_path / "q.db", InMemoryKeyProvider(bytes(32)), clock) as queue:
        keeper = standard_housekeeping(clock, queue=queue)
        assert keeper.tick() == ["purge_dead_letters"]


def test_local_alert_sink_enqueues(tmp_path: Path) -> None:
    clock = FakeClock()
    with EventQueue.open(tmp_path / "q.db", InMemoryKeyProvider(bytes(32)), clock) as queue:
        sink = QueueLocalAlertSink(queue, clock)
        sink(LocalAlertPayload(kind=LocalAlertKind.ENGINE_RESTART, detail={"reason": "hung"}))
        assert queue.stats().depth == 1
        [claimed] = queue.claim_batch(10)
        assert claimed.type == EventType.LOCAL_ALERT
        assert claimed.payload["kind"] == "engine_restart"


class Child:
    pid = 7
    exitcode = None

    def __init__(self) -> None:
        self.beats = 0
        self.killed = False

    def is_alive(self) -> bool:
        return not self.killed

    def heartbeat(self) -> int:
        self.beats += 1
        return self.beats

    def kill(self) -> None:
        self.killed = True


class Launcher:
    def __init__(self) -> None:
        self.children: list[Child] = []

    def launch(self) -> Child:
        self.children.append(Child())
        return self.children[-1]


def test_service_host_ticks_supervisor_and_housekeeping() -> None:
    clock = FakeClock()
    launcher = Launcher()
    supervisor = EngineSupervisor(launcher, clock, config=SupervisorConfig())
    keeper = Housekeeper(clock)
    ran = threading.Event()
    keeper.add("job", 1.0, ran.set)
    host = ServiceHost(supervisor, keeper, tick_s=0.01)
    host.start()
    host.start()  # idempotent
    try:
        assert host.running
        assert ran.wait(1.0)
        assert not host.wait(0.0)
    finally:
        host.stop()
    assert not host.running
    assert host.wait(0.0)
    assert launcher.children[0].killed
    assert supervisor.health().status is EngineStatus.DOWN


def test_service_host_tick_without_housekeeper() -> None:
    clock = FakeClock()
    supervisor = EngineSupervisor(Launcher(), clock)
    host = ServiceHost(supervisor)
    supervisor.start()
    host.tick()
    assert supervisor.health().status is EngineStatus.RUNNING
    host.stop()
