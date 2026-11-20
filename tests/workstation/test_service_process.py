"""The real engine child: killing the process restores monitoring within 5 s."""

import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from ag_shared.health import EngineStatus
from ag_workstation.clock import FakeClock, SystemClock
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
from ag_workstation.service.child import resolve_factory

AUTHKEY = b"p" * 32
FACTORY = "ag_workstation.fakes.service:fake_engine_factory"


@pytest.fixture
def transport() -> Iterator[PipeTransport]:
    path = Path(tempfile.mkdtemp(prefix="agproc"))
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

        os.kill(pid, signal.SIGKILL)
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
        os.kill(pid, signal.SIGSTOP)
        restored = _wait(
            lambda: supervisor.health().restarts == 1 and client.ping() is CallStatus.OK,
            limit=20.0,
        )
        assert restored < RESTART_DEADLINE_S
        assert supervisor.health().last_error == "hung"
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
        engine_child_main(counter, FACTORY, transport, 0.0, sleep=sleep, max_beats=2)
    finally:
        client.close()
    assert counter.value == 2
    assert seen == [CallStatus.OK, CallStatus.OK]


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
    assert ProcessLauncher(FACTORY, transport, start_method="forkserver") is not None
    if sys.platform != "win32":
        assert ProcessLauncher(FACTORY, transport, start_method="fork") is not None
