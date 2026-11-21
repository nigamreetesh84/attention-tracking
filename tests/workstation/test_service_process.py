"""The real engine child: killing the process restores monitoring within 5 s."""

import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from ag_shared.api import LocalAlertKind, LocalAlertPayload
from ag_shared.health import EngineStatus
from ag_shared.states import AttentionState
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.fakes import service as fake_service
from ag_workstation.service import (
    RESTART_DEADLINE_S,
    CallStatus,
    EngineSupervisor,
    IpcClient,
    PipeTransport,
    ProcessLauncher,
    ServiceHost,
    SupervisorConfig,
    engine_child_main,
)
from ag_workstation.service.child import beat_allowed, resolve_factory

AUTHKEY = b"p" * 32
FACTORY = "ag_workstation.fakes.service:fake_engine_factory"


@pytest.fixture
def transport() -> Iterator[PipeTransport]:
    path = Path(tempfile.mkdtemp(prefix="agproc"))
    if sys.platform == "win32":
        # multiprocessing on Windows has named pipes, not AF_UNIX sockets.
        yield PipeTransport(rf"\\.\pipe\agproc-{uuid.uuid4().hex}", AUTHKEY)
    else:
        yield PipeTransport(str(path / "e.sock"), AUTHKEY)
    shutil.rmtree(path, ignore_errors=True)


def _wait(cond: Callable[[], bool], limit: float) -> float:
    start = time.monotonic()
    while not cond():
        assert time.monotonic() - start < limit, "condition not reached"
        time.sleep(0.02)
    return time.monotonic() - start


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill; Windows covered by manual check")
def test_killing_the_engine_process_restores_monitoring(transport: PipeTransport) -> None:
    launcher = ProcessLauncher(FACTORY, transport, heartbeat_interval_s=0.05)
    config = SupervisorConfig(poll_interval_s=0.05, heartbeat_timeout_s=1.0)
    alerts: list[object] = []
    supervisor = EngineSupervisor(launcher, SystemClock(), on_alert=alerts.append, config=config)
    host = ServiceHost(supervisor, tick_s=0.05)
    client = IpcClient(transport, FakeClock())
    host.start()
    try:
        _wait(
            lambda: (
                client.ping() is CallStatus.OK
                and supervisor.health().status is EngineStatus.RUNNING
            ),
            limit=20.0,
        )
        pid = supervisor.health().pid
        assert pid is not None

        os.kill(pid, getattr(signal, "SIGKILL"))  # noqa: B009 - attribute missing on Windows
        restored = _wait(
            lambda: supervisor.health().restarts == 1 and client.ping() is CallStatus.OK,
            limit=20.0,
        )
        assert restored < RESTART_DEADLINE_S
        assert supervisor.health().pid != pid
        assert client.query_scan_state().status is CallStatus.OK
        assert len(alerts) == 1
    finally:
        host.stop()
        client.close()
    assert supervisor.health().status is EngineStatus.DOWN


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP is POSIX")
def test_frozen_engine_process_is_replaced(transport: PipeTransport) -> None:
    launcher = ProcessLauncher(FACTORY, transport, heartbeat_interval_s=0.05)
    config = SupervisorConfig(poll_interval_s=0.05, heartbeat_timeout_s=0.5)
    supervisor = EngineSupervisor(launcher, SystemClock(), config=config)
    host = ServiceHost(supervisor, tick_s=0.05)
    client = IpcClient(transport, FakeClock())
    host.start()
    try:
        _wait(
            lambda: (
                client.ping() is CallStatus.OK
                and supervisor.health().status is EngineStatus.RUNNING
            ),
            limit=20.0,
        )
        pid = supervisor.health().pid
        assert pid is not None
        os.kill(pid, getattr(signal, "SIGSTOP"))  # noqa: B009 - attribute missing on Windows
        restored = _wait(
            lambda: supervisor.health().restarts == 1 and client.ping() is CallStatus.OK,
            limit=20.0,
        )
        assert restored < RESTART_DEADLINE_S
        assert supervisor.health().last_error == "hung"
    finally:
        host.stop()
        client.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket path; Windows is a manual check")
def test_real_engine_child_process_is_fail_open_without_hardware(
    transport: PipeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2.9-pre: the default launcher spawns the real engine. Here there is no
    # camera config, camera, model or data dir: the child must still start,
    # answer Unknown and report the reason in health, never crash.
    for name in ("AG_ENGINE_CONFIG", "AG_DATA_DIR", "AG_DATA_KEY", "AG_MODEL_DIR"):
        monkeypatch.delenv(name, raising=False)
    launcher = ProcessLauncher.real(transport, heartbeat_interval_s=0.05)
    supervisor = EngineSupervisor(
        launcher, SystemClock(), config=SupervisorConfig(poll_interval_s=0.05)
    )
    host = ServiceHost(supervisor, tick_s=0.05)
    client = IpcClient(transport, FakeClock(), timeout_s=1.0)
    host.start()
    try:
        _wait(lambda: client.ping() is CallStatus.OK, limit=30.0)
        state = client.query_scan_state()
        assert state.status is CallStatus.OK
        assert state.state.state is AttentionState.UNKNOWN
        health = client.query_health().health
        assert health.last_error == "not_configured"
        assert not health.monitoring_available
        assert supervisor.health().restarts == 0
    finally:
        host.stop()
        client.close()


class _Counter:
    value = 0


def test_child_main_serves_and_beats_in_process(transport: PipeTransport) -> None:
    counter = _Counter()
    seen: list[CallStatus] = []
    client = IpcClient(transport, FakeClock(), timeout_s=1.0)

    def sleep(_: float) -> None:
        seen.append(client.ping())

    try:
        engine_child_main(counter, FACTORY, transport, 0.0, sleep=sleep, max_loops=2)
    finally:
        client.close()
    assert counter.value == 2
    assert seen == [CallStatus.OK, CallStatus.OK]


STALLING = "ag_workstation.fakes.service:stalling_engine_factory"


def test_child_main_stops_beating_while_the_engine_is_not_alive(
    transport: PipeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live child whose engine stalled (2.11): it keeps serving (scans get
    Unknown) but its heartbeat stops, so the supervisor restarts it."""
    monkeypatch.setattr(fake_service, "STALL_AFTER_S", 0.0)
    counter = _Counter()
    counter.value = 0
    client = IpcClient(transport, FakeClock(), timeout_s=1.0)
    seen: list[CallStatus] = []

    def sleep(_: float) -> None:
        seen.append(client.ping())

    try:
        engine_child_main(counter, STALLING, transport, 0.0, sleep=sleep, max_loops=3)
    finally:
        client.close()
    assert counter.value == 0
    assert seen == [CallStatus.OK] * 3  # still serving


def test_beat_allowed_reads_liveness_and_fails_open() -> None:
    class Alive:
        def alive(self) -> bool:
            return True

    class Dead:
        def alive(self) -> bool:
            return False

    class Broken:
        def alive(self) -> bool:
            raise RuntimeError("probe bug")

    assert beat_allowed(object())  # engines without a liveness probe always beat
    assert beat_allowed(Alive())
    assert not beat_allowed(Dead())
    assert beat_allowed(Broken())  # a broken probe never restarts a working child


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket path; Windows is a manual check")
def test_a_stalled_engine_in_a_live_child_is_restarted(transport: PipeTransport) -> None:
    """Scope 4.4 for a dead tick thread: the process is alive and serving,
    the supervisor still restarts it within seconds and raises `engine_restart`."""
    launcher = ProcessLauncher(STALLING, transport, heartbeat_interval_s=0.05)
    config = SupervisorConfig(poll_interval_s=0.05, heartbeat_timeout_s=0.5)
    alerts: list[LocalAlertPayload] = []
    supervisor = EngineSupervisor(launcher, SystemClock(), on_alert=alerts.append, config=config)
    host = ServiceHost(supervisor, tick_s=0.05)
    client = IpcClient(transport, FakeClock())
    host.start()
    try:
        _wait(lambda: client.ping() is CallStatus.OK, limit=20.0)
        first = supervisor.health().pid
        _wait(lambda: supervisor.health().restarts >= 1, limit=RESTART_DEADLINE_S + 15.0)
        assert supervisor.health().last_error == "hung"
        assert supervisor.health().pid != first
        assert alerts and alerts[0].kind is LocalAlertKind.ENGINE_RESTART
        assert alerts[0].detail["reason"] == "hung"
        _wait(lambda: client.ping() is CallStatus.OK, limit=20.0)
    finally:
        host.stop()
        client.close()


@pytest.mark.parametrize("path", ["nocolon", ":f", "m:", "ag_workstation.clock:nothing_here"])
def test_resolve_factory_rejects_bad_paths(path: str) -> None:
    with pytest.raises((ValueError, AttributeError)):
        resolve_factory(path)


def test_resolve_factory_rejects_non_callables() -> None:
    with pytest.raises(TypeError):
        resolve_factory("ag_workstation.engine_api:SCAN_QUERY_TIMEOUT_S")


def test_launcher_validates_factory_and_start_method(transport: PipeTransport) -> None:
    with pytest.raises(ValueError):
        ProcessLauncher("bad", transport)
    if sys.platform != "win32":
        # Windows has only the spawn start method.
        assert ProcessLauncher(FACTORY, transport, start_method="forkserver") is not None
        assert ProcessLauncher(FACTORY, transport, start_method="fork") is not None
