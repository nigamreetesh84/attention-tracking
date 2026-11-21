"""Sync wiring (PLAN 2.9): the scan path never waits for sync, the service
host runs sync on its own thread, the engine child and the UI load the
stored bundle, and the production assembly builds without a network."""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from ag_shared.api import EventType
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.engine_api import CameraHealthSummary, EngineHealth, StationState
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.fakes.device_server import FakeDeviceServer, Fault
from ag_workstation.queue import EventQueue, InMemoryKeyProvider, PayloadCipher
from ag_workstation.scan import ScanFlow, ScanJournal
from ag_workstation.service import EngineSupervisor, ServiceHost
from ag_workstation.service import engine_factory as ef
from ag_workstation.service.health import HealthReporter
from ag_workstation.service.service_key import SERVICE_KEY_ENV
from ag_workstation.service.supervisor import SupervisorHealth
from ag_workstation.sync import (
    ConfigStore,
    DeviceCredentials,
    HttpxTransport,
    InMemoryCredentialStore,
    SyncFlag,
    bundle_path,
)
from ag_workstation.sync.wiring import (
    SERVER_URL_ENV,
    HealthSampler,
    ServiceStatusSource,
    app_version,
    build_sync_service,
    service_components,
    sync_service_from_env,
)
from pydantic import SecretStr

Json = dict[str, Any]  # Any: free-form JSON fixtures

KEY = bytes(range(32))
SERVICE_KEY_HEX = bytes(range(100, 132)).hex()
"""The service-only key (3.0), given by `AG_SERVICE_KEY` off Windows."""
T0 = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
STATION = UUID("0193a0b0-0000-7000-8000-00000000d001")


def bundle_json(version: int = 7) -> Json:
    return {
        "version": version,
        "generated_at": "2026-11-21T07:00:00Z",
        "site": {"timezone": "Asia/Kolkata"},
        "workstation": {
            "id": str(STATION),
            "name": "WS",
            "line_id": str(STATION),
            "camera_count": 1,
        },
        "cameras": [{"slot": 1, "role": "primary", "kind": "usb", "uri": "0"}],
        "ppe_profile": builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json"),
        "thresholds": {},
    }


class NoSessions:
    def current(self) -> None:
        return None


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
    def launch(self) -> Child:
        return Child()


@dataclass
class Hung:
    server: FakeDeviceServer
    sync_queue: EventQueue
    ui_queue: EventQueue
    service: Any  # Any: SyncService, kept loose for the fixture


@pytest.fixture
def hung(tmp_path: Path) -> Iterator[Hung]:
    """A real sync thread stuck inside a request that never returns."""
    clock = SystemClock()
    keys = InMemoryKeyProvider(KEY)
    server = FakeDeviceServer(clock=clock, workstation_id=STATION, secret="s")  # noqa: S106 - test fixture
    server.inject("/", Fault.HANG, times=1000)
    sync_queue = EventQueue.open(tmp_path / "queue.db", keys, clock)
    ui_queue = EventQueue.open(tmp_path / "queue.db", keys, clock, recover_sending=False)
    service = build_sync_service(
        data_dir=tmp_path,
        keys=keys,
        queue=sync_queue,
        clock=clock,
        transport=HttpxTransport("https://ag.test", transport=httpx.MockTransport(server)),
        credentials=InMemoryCredentialStore(DeviceCredentials(STATION, SecretStr("s"))),
    )
    service.start()
    assert server.wait_hanging(5.0), "sync is inside a request"
    yield Hung(server, sync_queue, ui_queue, service)
    server.release_hang()
    service.stop(timeout=2.0)
    ui_queue.close()
    sync_queue.close()


def test_a_scan_is_accepted_while_sync_is_hung(hung: Hung, tmp_path: Path) -> None:
    clock = SystemClock()
    engine = FakeStationEngine(clock)
    engine.set_state(AttentionState.ATTENTIVE)
    journal = ScanJournal.open(tmp_path / "scan.jnl", PayloadCipher(InMemoryKeyProvider(KEY)))
    flow = ScanFlow(
        queue=hung.ui_queue,
        journal=journal,
        engine=engine,
        evidence=engine,
        sessions=NoSessions(),
        clock=clock,
    )
    try:
        for n in range(5):
            started = time.monotonic()
            result = flow.on_barcode(f"SCAN{n}")
            assert time.monotonic() - started < 0.2
            assert result.outcome is not None
        assert hung.ui_queue.stats().depth >= 5  # durably queued behind the hung sender
        assert hung.service.runner.running
    finally:
        flow.close()
        journal.close()


def test_service_host_supervises_while_sync_is_hung(hung: Hung) -> None:
    clock = FakeClock()
    supervisor = EngineSupervisor(Launcher(), clock)
    host = ServiceHost(supervisor, tick_s=0.01, sync=hung.service)
    supervisor.start()
    started = time.monotonic()
    for _ in range(20):
        host.tick()
        clock.advance(0.1)
    assert time.monotonic() - started < 1.0
    assert supervisor.health().status is EngineStatus.RUNNING
    supervisor.stop()


def test_service_host_starts_and_stops_sync() -> None:
    events: list[str] = []

    class Sync:
        def start(self) -> None:
            events.append("start")

        def stop(self, timeout: float = 2.0) -> None:
            events.append("stop")

    closed: list[bool] = []
    host = ServiceHost(
        EngineSupervisor(Launcher(), FakeClock()),
        tick_s=0.01,
        sync=Sync(),
        closers=(lambda: closed.append(True),),
    )
    host.start()
    host.stop()
    assert events == ["start", "stop"] and closed == [True]


def test_service_host_survives_a_broken_sync() -> None:
    class Broken:
        def start(self) -> None:
            raise RuntimeError("no")

        def stop(self, timeout: float = 2.0) -> None:
            raise RuntimeError("no")

    def bad_close() -> None:
        raise OSError("gone")

    host = ServiceHost(
        EngineSupervisor(Launcher(), FakeClock()),
        tick_s=0.01,
        sync=Broken(),
        closers=(bad_close,),
    )
    host.start()
    assert host.running
    host.stop()
    assert not host.running


# -- assembly ---------------------------------------------------------------------------


class Supervisor:
    def health(self) -> SupervisorHealth:
        return SupervisorHealth(
            status=EngineStatus.RUNNING,
            pid=7,
            restarts=0,
            last_error=None,
            consecutive_failures=0,
        )


class State:
    def current_state(self) -> StationState:
        return StationState(
            state=AttentionState.DROWSY,
            confidence=0.6,
            low_confidence=True,
            at=T0,
            ts=0.0,
            engine_version="x",
            config_version=7,
        )


def _engine_health() -> EngineHealth:
    return EngineHealth(
        EngineStatus.RUNNING, (CameraHealthSummary(slot=1, status=CameraStatus.ONLINE, fps=9),)
    )


def test_status_source_merges_health_and_state() -> None:
    reporter = HealthReporter(
        FakeClock(), Supervisor(), engine_health=_engine_health, disk_free_mb=lambda: 512
    )
    status = ServiceStatusSource(reporter, State()).station_status()
    assert status.state is AttentionState.DROWSY and status.low_confidence
    assert status.cameras[0].status is CameraStatus.ONLINE and status.disk_free_mb == 512
    bare = ServiceStatusSource(reporter, None).station_status()
    assert bare.state is AttentionState.UNKNOWN and bare.engine_status is EngineStatus.RUNNING


def test_health_sampler_queues_a_health_item(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=T0)
    queue = EventQueue.open(tmp_path / "q.db", InMemoryKeyProvider(KEY), clock)
    reporter = HealthReporter(
        clock, Supervisor(), engine_health=_engine_health, queue_stats=queue.stats
    )
    HealthSampler(reporter, queue, clock, offset=lambda: 42)()
    [record] = queue.claim_batch()
    assert record.type == EventType.HEALTH
    assert record.payload["clock_offset_ms"] == 42
    assert record.payload["app_version"] == app_version()
    queue.close()


def test_no_server_url_means_offline_only(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=T0)
    queue = EventQueue.open(tmp_path / "q.db", InMemoryKeyProvider(KEY), clock)
    keys = InMemoryKeyProvider(KEY)
    assert sync_service_from_env({}, data_dir=tmp_path, keys=keys, queue=queue, clock=clock) is None
    assert (
        sync_service_from_env(
            {SERVER_URL_ENV: "http://plain.example"},
            data_dir=tmp_path,
            keys=keys,
            queue=queue,
            clock=clock,
        )
        is None
    )
    queue.close()


def test_service_components_from_env(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=T0)
    keys = InMemoryKeyProvider(KEY)
    queue = EventQueue.open(tmp_path / "q.db", keys, clock)
    parts = service_components(
        {SERVER_URL_ENV: "https://ag.example", SERVICE_KEY_ENV: SERVICE_KEY_HEX},
        data_dir=tmp_path,
        keys=keys,
        queue=queue,
        clock=clock,
        supervisor=Supervisor(),
        state=State(),
        engine_health=_engine_health,
    )
    assert parts.sync is not None
    assert set(parts.housekeeper.jobs) >= {
        "purge_dead_letters",
        "evidence_housekeeping",
        "health_sample",
        "purge_commands",
    }
    assert parts.sync.client.tick() == 5.0  # not enrolled yet: no network at all
    assert SyncFlag.NOT_ENROLLED in parts.sync.client.health().flags
    clock.advance(61)
    assert "health_sample" in parts.housekeeper.tick()
    parts.sync.stop()
    for close in parts.closers:
        close()
    offline = service_components(
        {SERVICE_KEY_ENV: SERVICE_KEY_HEX},
        data_dir=tmp_path,
        keys=keys,
        queue=queue,
        clock=clock,
        supervisor=Supervisor(),
    )
    assert offline.sync is None and "health_sample" in offline.housekeeper.jobs
    for close in offline.closers:
        close()
    queue.close()


def test_default_service_host_with_data_dir_wires_sync(tmp_path: Path) -> None:
    from ag_workstation.service import host as host_module

    host = host_module.default_service_host(
        {
            host_module.AUTHKEY_ENV: "ab" * 32,
            ef.DATA_DIR_ENV: str(tmp_path),
            ef.DATA_KEY_ENV: KEY.hex(),
            SERVER_URL_ENV: "https://ag.example",
        }
    )
    assert host.sync is not None and host.housekeeper is not None
    host.stop()  # never started: closes files only


# -- bundle consumers -------------------------------------------------------------------


def test_engine_child_starts_from_the_stored_bundle(tmp_path: Path) -> None:
    keys = InMemoryKeyProvider(KEY)
    data = tmp_path / "data"
    ConfigStore(bundle_path(data), keys).apply(json.dumps(bundle_json(7)).encode())
    config = ef.station_config_from_bundle(data, keys)
    assert config is not None and config.config_version == 7
    child = ef.build_station(
        None,
        env={ef.DATA_DIR_ENV: str(data)},
        keys=keys,
        clock=FakeClock(start_utc=T0),
        start=False,
    )
    try:
        assert child.adapter.config is not None
        assert child.adapter.config.config_version == 7
    finally:
        child.close()
    assert ef.station_config_from_bundle(tmp_path / "empty", keys) is None


def test_ui_takes_its_roster_from_the_stored_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ag_workstation.ui.app import assemble
    from argon2 import PasswordHasher

    keys = InMemoryKeyProvider(KEY)
    ConfigStore(bundle_path(tmp_path), keys).apply(json.dumps(bundle_json(7)).encode())
    built = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(start_utc=T0),
        keys=keys,
        hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1),
    )
    try:
        assert built.engine_mode == "unreachable"
        assert (tmp_path / "commands.db").exists()  # the consumer's inbox
    finally:
        built.close()
