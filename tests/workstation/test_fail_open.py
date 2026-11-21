"""Fail-open scenario suite (PLAN 2.11): every row of scope 4.4 on the assembled workstation.

The workstation is wired as in production (PROGRESS 2.9-pre, process
placement), all in one test process:

- UI process: `ui.app.assemble` (real `ScanFlow`, scan journal, encrypted
  queue opened enqueue-only, `SessionManager` with PIN login, record mirror,
  `SessionPusher`, `RemoteEvidence`, scan presenter) talking to the engine
  child over two real `IpcClient`s;
- engine child: `service.engine_factory.build_station` (real `Engine` behind
  `EngineAdapter`, rings, `BurstAssembler`, `CaptureGate`,
  `GatedEvidenceSink`, `EvidenceStore`, episode recorder) served by a real
  `IpcServer` over a real socket / named pipe;
- service parent: real `EngineSupervisor` and the housekeeping and sync
  client built by `sync.wiring.service_components` (health reporter,
  monitoring alerts, health sample, the real `SyncClient` over httpx against
  the fault-injecting `FakeDeviceServer`).

Only hardware and the network are replaced: a scripted frame feed and fake
landmarks instead of cameras and MediaPipe, an injected free-space probe
instead of a full disk, `httpx.MockTransport` instead of the network, and a
`FakeClock` shared by every component. The engine is ticked by the test
(its own tick thread waits an hour), so each scenario is deterministic. One
scenario spawns a real engine child process and kills it (POSIX only; on
Windows that is the owner's manual check, PROGRESS 2.3).

Every scenario asserts the scope's three things: each scan is accepted
(`on_barcode` returns within `SCAN_QUERY_BUDGET_S`, the scan is recorded in
the queue or on the server), the state becomes Unknown where the scope says
so, and the alert or health flag the scope requires is raised.

Scope 4.4 row -> scenarios (`SCENARIOS`, checked by
`test_every_scope_failure_row_has_a_scenario`):

| Scope 4.4 failure      | Scenarios                                                   |
|------------------------|-------------------------------------------------------------|
| Camera offline         | test_camera_offline_one_of_two_cameras,                     |
|                        | test_camera_offline_on_a_single_camera_station              |
| Both cameras offline   | test_both_cameras_offline                                   |
| Server unreachable     | test_server_unreachable                                     |
| Detection engine crash | test_engine_child_process_killed (POSIX),                   |
|                        | test_engine_tick_thread_dead, test_engine_ipc_unavailable,  |
|                        | test_engine_answer_hangs                                    |
| Disk full              | test_disk_full_purges_oldest_images_and_alerts,             |
|                        | test_disk_full_for_metadata_keeps_scans_in_the_journal      |
| Clock drift            | test_clock_drift                                            |

Interpretation of "Camera offline: status becomes Unknown": that camera's
status and candidate are offline/Unknown; the station continues on the
remaining camera (detection-engine section 6, rules 2 and 7), so on a
two-camera station the station state stays known, and on a one-camera
station it is Unknown. The admin/supervisor alert is a `camera_offline`
`local_alert` after 30 s (rule 7's degraded limit), and "Monitoring
unavailable" is both the scan-screen banner and a `monitoring_unavailable`
`local_alert`.

Two strict xfails at the end record gaps outside builder territory (see their
reasons and PROGRESS 2.11).
"""

import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import numpy as np
import pytest
from ag_shared.api import ConfigBundle, EventType, ImagesWithheld, LocalAlertKind
from ag_shared.config import CameraConfig
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.auth import StaticRosterSource
from ag_workstation.capture.types import CameraHealth, Frame, Image
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.engine.fakes import StubFeed, face_result
from ag_workstation.engine.runtime import Engine
from ag_workstation.engine_api import SCAN_QUERY_BUDGET_S, StationState
from ag_workstation.evidence.store import EvidenceStore, RemovalReason
from ag_workstation.fakes.device_server import FakeDeviceServer, Fault
from ag_workstation.landmarks import FakeLandmarkProvider
from ag_workstation.landmarks.types import LandmarkResult
from ag_workstation.queue import EventQueue, InMemoryKeyProvider
from ag_workstation.queue.store import ClaimedRecord
from ag_workstation.scan import ScanResult
from ag_workstation.service import (
    RESTART_DEADLINE_S,
    CallStatus,
    EngineSupervisor,
    IpcClient,
    IpcServer,
    PipeTransport,
    ProcessLauncher,
    ServiceHost,
    SupervisorConfig,
)
from ag_workstation.service.engine_factory import DATA_DIR_ENV, build_station
from ag_workstation.service.host import QueueLocalAlertSink
from ag_workstation.service.monitoring_alerts import ALERT_AFTER_S
from ag_workstation.sync import (
    DeviceCredentials,
    HttpxTransport,
    InMemoryCredentialStore,
    SyncFlag,
)
from ag_workstation.sync.wiring import service_components
from ag_workstation.ui.app import Assembly, assemble
from ag_workstation.ui.app_presenter import AppViewModel
from argon2 import PasswordHasher
from pydantic import SecretStr

Json = dict[str, Any]  # Any: free-form JSON for bundles and decoded payloads

KEY = bytes(range(32))
AUTHKEY = b"f" * 32
T0 = datetime(2026, 11, 21, 4, 30, tzinfo=UTC)
STATION = UUID("0193a0b0-0000-7000-8000-00000000f001")
WORKER = UUID("0193a0b0-0000-7000-8000-00000000f002")
EMPLOYEE = "W100"
PIN = "2468"
SECRET = "s"  # noqa: S105 - fake device secret
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
PIN_HASH = FAST_HASHER.hash(PIN)
AWAKE, ASLEEP = 0.30, 0.05
"""Eye aspect ratios the fake landmarks report."""

SCENARIOS: dict[str, tuple[str, ...]] = {
    "Camera offline": (
        "test_camera_offline_one_of_two_cameras",
        "test_camera_offline_on_a_single_camera_station",
    ),
    "Both cameras offline": ("test_both_cameras_offline",),
    "Server unreachable": ("test_server_unreachable",),
    "Detection engine crash": (
        "test_engine_child_process_killed",
        "test_engine_tick_thread_dead",
        "test_engine_ipc_unavailable",
        "test_engine_answer_hangs",
    ),
    "Disk full": (
        "test_disk_full_purges_oldest_images_and_alerts",
        "test_disk_full_for_metadata_keeps_scans_in_the_journal",
    ),
    "Clock drift": ("test_clock_drift",),
}
"""Scope 4.4 failure row -> the scenarios that cover it."""


# -- helpers -----------------------------------------------------------------


def _wait(cond: Callable[[], bool], limit: float = 5.0, what: str = "condition") -> None:
    end = time.monotonic() + limit
    while not cond():
        assert time.monotonic() < end, f"{what} not reached within {limit} s"
        time.sleep(0.01)


def _fake_jpeg(image: Image, quality: int) -> bytes:
    return b"\xff\xd8" + bytes([quality]) + b"\xff\xd9"


def _image() -> Image:
    return np.zeros((6, 8, 3), dtype=np.uint8)


def _bundle(cameras: int) -> ConfigBundle:
    profile = builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json")
    body: Json = {
        "version": 3,
        "generated_at": T0.isoformat(),
        "site": {"timezone": "UTC", "settings": {}},
        "workstation": {
            "id": str(STATION),
            "name": "Fail-open station",
            "line_id": str(STATION),
            "camera_count": cameras,
        },
        "cameras": [
            {
                "slot": slot,
                "role": "primary" if slot == 1 else "secondary",
                "kind": "usb",
                "uri": str(slot - 1),
            }
            for slot in range(1, cameras + 1)
        ],
        "ppe_profile": profile,
        "thresholds": {},
        "roster": [
            {
                "user_id": str(WORKER),
                "employee_id": EMPLOYEE,
                "display_name": "Worker",
                "pin_hash": PIN_HASH,
                "has_consent": True,
                "ppe_profile_override": None,
                "preferred_language": None,
                "is_active": True,
            }
        ],
    }
    return ConfigBundle.model_validate(body)


def _transport(root: Path) -> PipeTransport:
    if sys.platform == "win32":
        return PipeTransport(rf"\\.\pipe\agfo-{uuid.uuid4().hex}", AUTHKEY)
    return PipeTransport(str(root / "e.sock"), AUTHKEY)


class Cameras:
    """The hardware: which cameras deliver frames and what the faces look like."""

    def __init__(self, count: int) -> None:
        self.slots = tuple(range(1, count + 1))
        self.lost: set[int] = set()
        self.ear = AWAKE

    def provider(self, camera: CameraConfig) -> FakeLandmarkProvider:
        def script(frame: Frame, with_pose: bool) -> LandmarkResult:
            return face_result(frame, ear=self.ear)

        return FakeLandmarkProvider(script)

    def feed(self, cameras: Sequence[CameraConfig]) -> StubFeed:
        return _Feed(cameras, self)


class _Feed(StubFeed):
    """A stub feed whose lost cameras report offline, as the capture worker does."""

    def __init__(self, cameras: Sequence[CameraConfig], world: Cameras) -> None:
        super().__init__(cameras)
        self._world = world

    def health(self) -> dict[int, CameraHealth]:
        return {
            slot: replace(h, status=CameraStatus.OFFLINE) if slot in self._world.lost else h
            for slot, h in super().health().items()
        }


class _AliveChild:
    """The supervisor's process handle while the engine runs in this process."""

    pid = 4242
    exitcode = None

    def __init__(self) -> None:
        self.beats = 0

    def is_alive(self) -> bool:
        return True

    def heartbeat(self) -> int:
        self.beats += 1
        return self.beats

    def kill(self) -> None:
        pass


class _InProcessLauncher:
    def launch(self) -> _AliveChild:
        return _AliveChild()


class Station:
    """One assembled workstation (UI process + engine child + service parent)."""

    def __init__(self, root: Path, *, cameras: int = 2) -> None:
        self.root = root
        self.data = root / "data"
        self.data.mkdir(parents=True)
        self.clock = FakeClock(start_utc=T0)
        self.keys = InMemoryKeyProvider(KEY)
        self.cams = Cameras(cameras)
        self.free_bytes = 10**12
        self.seq = 0
        self.records: list[ClaimedRecord] = []
        self.scans: list[ScanResult] = []
        self.transport = _transport(root)

        # Service parent (opened first, as in production: it sweeps the evidence store).
        self.sender = EventQueue.open(self.data / "queue.db", self.keys, self.clock)
        self.supervisor = EngineSupervisor(
            _InProcessLauncher(),
            self.clock,
            on_alert=QueueLocalAlertSink(self.sender, self.clock),
        )
        self.supervisor.start()
        self.service_ipc = IpcClient(self.transport, self.clock, timeout_s=0.5)
        self.device = FakeDeviceServer(clock=self.clock, workstation_id=STATION, secret=SECRET)
        self.parts = service_components(
            {},
            data_dir=self.data,
            keys=self.keys,
            queue=self.sender,
            clock=self.clock,
            supervisor=self.supervisor,
            state=self.service_ipc,
            engine_health=self.service_ipc.health,
            transport=HttpxTransport("https://ag.test", transport=httpx.MockTransport(self.device)),
            credentials=InMemoryCredentialStore(DeviceCredentials(STATION, SecretStr(SECRET))),
        )
        assert self.parts.sync is not None
        self.sync = self.parts.sync
        self.host = ServiceHost(self.supervisor, self.parts.housekeeper)

        # Engine child.
        self.child = build_station(
            None,
            env={DATA_DIR_ENV: str(self.data)},
            clock=self.clock,
            feed_factory=self.cams.feed,
            provider_factory=self.cams.provider,
            encoder=_fake_jpeg,
            keys=self.keys,
            tick_interval_s=3600.0,  # the test ticks
            pump_interval_s=3600.0,  # the test pumps
            evidence_disk_free=lambda _path: self.free_bytes,
        )
        self.ipc_server = IpcServer(self.transport, self.child, control=self.child)
        self.ipc_server.start()

        # UI process.
        self.ui: Assembly = assemble(
            data_dir=self.data,
            dev=False,
            clock=self.clock,
            ipc=IpcClient(self.transport, self.clock),
            control=IpcClient(self.transport, self.clock, timeout_s=1.0),
            roster=StaticRosterSource(_bundle(cameras)),
            keys=self.keys,
            hasher=FAST_HASHER,
        )
        # The UI pushes the station config from the bundle; the first one starts the engine.
        _wait(lambda: self.child.adapter.engine is not None, what="engine started by the UI")

    # -- driving ----------------------------------------------------------

    @property
    def engine(self) -> Engine:
        engine = self.child.adapter.engine
        assert engine is not None
        return engine

    def run(self, seconds: float, *, tick_engine: bool = True, view: bool = False) -> None:
        """Advance time in 0.1 s steps: frames from the cameras still delivering,
        an engine tick and pump, a service tick every second and (with `view`)
        a scan-screen refresh every half second."""
        for step in range(round(seconds * 10)):
            ts = self.clock.monotonic()
            self.seq += 1
            engine = self.child.adapter.engine
            if engine is not None:
                for slot in self.cams.slots:
                    if slot not in self.cams.lost:
                        engine.process_frame(Frame(slot, self.seq, ts, self.clock.now(), _image()))
                if tick_engine:
                    engine.tick()
            self.child.adapter.pump()
            if step % 10 == 0:
                self.host.tick()
                # The UI's 1 s session push, on the test's timeline (its own
                # thread runs on real time, which barely moves here).
                if self.ui.pusher is not None:
                    self.ui.pusher.push()
            if view and step % 5 == 0:
                self.view()
            self.clock.advance(0.1)

    def view(self) -> AppViewModel:
        """Refresh the scan screen and let its status poll finish."""
        presenter = self.ui.presenter
        presenter.tick()
        poller = presenter.scan._poller  # the poll runs on a runner thread
        _wait(lambda: not poller.in_flight, what="status poll")
        return presenter.tick()

    def login(self) -> None:
        presenter = self.ui.presenter
        presenter.show_login()
        presenter.login.key(f"{EMPLOYEE}\t{PIN}\r")
        assert self.ui.manager is not None
        manager = self.ui.manager
        _wait(lambda: manager.current() is not None, what="login")
        _wait(lambda: self.child.sessions.current() is not None, what="session pushed")
        presenter.tick()

    def scan(self, code: str) -> ScanResult:
        """One barcode through the real scan flow; accepted within the budget."""
        assert self.ui.flow is not None
        start = time.monotonic()
        result = self.ui.flow.on_barcode(code)
        elapsed = time.monotonic() - start
        assert elapsed < SCAN_QUERY_BUDGET_S, f"on_barcode took {elapsed * 1000:.0f} ms"
        assert result.persisted
        assert result.barcode == code
        self.scans.append(result)
        return result

    # -- reading what was recorded ------------------------------------------

    def drain(self) -> list[ClaimedRecord]:
        """Everything queued so far (as the sync client would claim it)."""
        while True:
            claimed = self.sender.claim_batch()
            if not claimed:
                return self.records
            self.records.extend(claimed)
            self.sender.mark_done(r.id for r in claimed)

    def queued(self, item_type: str) -> list[ClaimedRecord]:
        return [r for r in self.drain() if r.type == item_type]

    def queued_scans(self) -> dict[UUID, Json]:
        return {r.id: r.payload for r in self.queued(EventType.SCAN)}

    def alerts(self, kind: LocalAlertKind) -> list[Json]:
        return [r.payload for r in self.queued(EventType.LOCAL_ALERT) if r.payload["kind"] == kind]

    def assert_scans_recorded(self) -> None:
        """Every scan of the scenario is in the queue exactly once, as scanned."""
        scans = self.queued_scans()
        for result in self.scans:
            assert result.scan_id in scans, f"scan {result.barcode} not queued"
            payload = scans[result.scan_id]
            assert payload["barcode"] == result.barcode
            assert payload["state_at_scan"] == result.state.value
            assert payload["outcome"] == result.outcome.value
        assert len([r for r in self.records if r.type == EventType.SCAN]) == len(scans)

    def close(self) -> None:
        self.ui.close()
        self.ipc_server.stop()
        self.child.close()
        self.sync.stop(0.5)
        self.service_ipc.close()
        for close in reversed(self.parts.closers):
            close()
        self.supervisor.stop()
        self.sender.close()


@pytest.fixture
def make_station() -> Iterator[Callable[..., Station]]:
    stations: list[Station] = []
    roots: list[Path] = []

    def make(cameras: int = 2) -> Station:
        # A short path: AF_UNIX socket paths are limited to about 100 bytes.
        root = Path(tempfile.mkdtemp(prefix="agfo"))
        roots.append(root)
        station = Station(root, cameras=cameras)
        stations.append(station)
        return station

    yield make
    for station in stations:
        station.close()
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)


def _server_payload(station: Station, item_id: UUID) -> Json:
    """The payload of an item the fake server accepted."""
    item: Any = station.device.items[item_id]  # Any: decoded JSON object
    payload: Json = item["payload"]
    return payload


def _awake_and_logged_in(station: Station) -> None:
    station.run(4.0)
    station.login()
    station.run(2.0)
    assert station.child.scan_state().state is AttentionState.ATTENTIVE


# -- the mapping -----------------------------------------------------------------


def _scope_failure_rows() -> list[str]:
    scope = Path(__file__).resolve().parents[2] / "docs" / "scope-v1.md"
    text = scope.read_text(encoding="utf-8")
    section = text.split("### 4.4", 1)[1].split("\n---", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) < 2:
            continue
        if cells[0] in ("Failure", "") or re.fullmatch(r"-+", cells[0]):
            continue
        rows.append(cells[0])
    return rows


def test_every_scope_failure_row_has_a_scenario() -> None:
    rows = _scope_failure_rows()
    assert len(rows) >= 6, rows
    assert set(rows) == set(SCENARIOS), "scope 4.4 and SCENARIOS differ"
    module = globals()
    for row, names in SCENARIOS.items():
        assert names, row
        for name in names:
            test = module.get(name)
            assert callable(test), f"{row}: {name} is missing"
            marks = [m.name for m in getattr(test, "pytestmark", [])]
            assert "xfail" not in marks, f"{row}: {name} must pass, not xfail"


# -- Camera offline ----------------------------------------------------------------


def test_camera_offline_one_of_two_cameras(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    station.scan("C-BEFORE")

    station.cams.lost.add(2)
    station.run(3.0, view=True)  # past unknown_seconds without frames from camera 2
    # That camera is offline and contributes Unknown; the station goes on with camera 1.
    health = station.child.health()
    assert {c.slot: c.status for c in health.cameras} == {
        1: CameraStatus.ONLINE,
        2: CameraStatus.OFFLINE,
    }
    assert health.status is EngineStatus.DEGRADED
    answer = station.child.scan_state()
    by_slot = {c.slot: c for c in answer.camera_states}
    assert by_slot[2].state is AttentionState.UNKNOWN and not by_slot[2].usable
    assert answer.state is AttentionState.ATTENTIVE
    during = station.scan("C-DURING")
    assert during.outcome is ScanOutcome.ATTENTIVE

    # No alert before the degraded limit, one camera_offline alert after it.
    assert station.alerts(LocalAlertKind.CAMERA_OFFLINE) == []
    station.run(ALERT_AFTER_S + 6.0, view=True)
    station.scan("C-LATER")
    (alert,) = station.alerts(LocalAlertKind.CAMERA_OFFLINE)
    assert alert["detail"]["slot"] == 2
    # Monitoring itself is available: no banner, no monitoring_unavailable alert.
    assert station.view().scan.unavailable_text == ""
    assert station.alerts(LocalAlertKind.MONITORING_UNAVAILABLE) == []
    station.run(30.0)
    assert len(station.alerts(LocalAlertKind.CAMERA_OFFLINE)) == 1  # once per episode
    station.assert_scans_recorded()


def test_camera_offline_on_a_single_camera_station(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=1)
    _awake_and_logged_in(station)

    station.cams.lost.add(1)
    station.run(1.0)
    result = station.scan("S-1")
    assert result.state is AttentionState.UNKNOWN
    assert result.outcome is ScanOutcome.UNMONITORED and not result.warn
    station.run(ALERT_AFTER_S + 6.0)
    assert [a["detail"]["slot"] for a in station.alerts(LocalAlertKind.CAMERA_OFFLINE)] == [1]
    assert len(station.alerts(LocalAlertKind.MONITORING_UNAVAILABLE)) == 1
    station.scan("S-2")
    station.assert_scans_recorded()


# -- Both cameras offline -------------------------------------------------------


def test_both_cameras_offline(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    station.run(1.0, view=True)
    assert station.view().scan.unavailable_text == ""

    station.cams.lost.update({1, 2})
    station.run(1.0, view=True)
    result = station.scan("B-1")
    assert result.state is AttentionState.UNKNOWN
    assert result.outcome is ScanOutcome.UNMONITORED
    assert not station.child.health().monitoring_available

    # "Monitoring unavailable" banner after the screen's 5 s grace.
    station.run(6.0, view=True)
    screen = station.view().scan
    assert screen.unavailable_text != ""
    assert screen.status_level.value == "unavailable"
    station.scan("B-2")

    # Alerts: each camera, and monitoring unavailable.
    station.run(ALERT_AFTER_S, view=True)
    slots = sorted(a["detail"]["slot"] for a in station.alerts(LocalAlertKind.CAMERA_OFFLINE))
    assert slots == [1, 2]
    (unavailable,) = station.alerts(LocalAlertKind.MONITORING_UNAVAILABLE)
    assert unavailable["detail"]["cameras_offline"] == [1, 2]

    # The cameras come back: monitoring and the banner recover, scans are attentive again.
    station.cams.lost.clear()
    station.run(4.0, view=True)
    assert station.view().scan.unavailable_text == ""
    assert station.scan("B-3").outcome is ScanOutcome.ATTENTIVE
    station.assert_scans_recorded()


# -- Detection engine crash -------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill; Windows is a manual check (2.3)")
def test_engine_child_process_killed(tmp_path: Path) -> None:
    """A real spawned engine child is killed: scans go on (Unknown, fast), the
    supervisor restarts it within 5 s and queues an `engine_restart` alert."""
    root = Path(tempfile.mkdtemp(prefix="agfk"))
    transport = _transport(root)
    clock = SystemClock()
    keys = InMemoryKeyProvider(KEY)
    data = tmp_path / "data"
    data.mkdir()
    sender = EventQueue.open(data / "queue.db", keys, clock)
    launcher = ProcessLauncher(
        "ag_workstation.fakes.service:fake_engine_factory", transport, heartbeat_interval_s=0.05
    )
    supervisor = EngineSupervisor(
        launcher,
        clock,
        on_alert=QueueLocalAlertSink(sender, clock),
        config=SupervisorConfig(poll_interval_s=0.05, heartbeat_timeout_s=1.0),
    )
    ping = IpcClient(transport, FakeClock())
    ui = assemble(
        data_dir=data,
        dev=False,
        clock=FakeClock(start_utc=T0),
        ipc=IpcClient(transport, FakeClock()),
        control=IpcClient(transport, FakeClock(), timeout_s=1.0),
        roster=StaticRosterSource(_bundle(1)),
        keys=keys,
        hasher=FAST_HASHER,
    )
    assert ui.flow is not None
    flow = ui.flow
    scans: list[ScanResult] = []

    def scan(code: str) -> ScanResult:
        start = time.monotonic()
        result = flow.on_barcode(code)
        assert time.monotonic() - start < SCAN_QUERY_BUDGET_S
        assert result.persisted
        scans.append(result)
        return result

    def polled_up(restarts: int = 0) -> bool:
        supervisor.poll()  # what the service's tick does
        return supervisor.health().restarts >= restarts and ping.ping() is CallStatus.OK

    supervisor.start()
    try:
        _wait(
            polled_up,
            limit=20.0,
            what="engine child up",
        )
        scan("K-UP")
        pid = supervisor.health().pid
        assert pid is not None
        killed_at = time.monotonic()
        os.kill(pid, getattr(signal, "SIGKILL"))  # noqa: B009 - missing on Windows
        _wait(lambda: ping.ping() is not CallStatus.OK, what="child gone")
        # The engine is down: every scan is still accepted at once, as Unknown.
        for n in range(3):
            down = scan(f"K-DOWN-{n}")
            assert down.state is AttentionState.UNKNOWN
            assert down.outcome is ScanOutcome.UNMONITORED
        # The supervisor (the service's tick) restarts it within the deadline.
        _wait(
            lambda: polled_up(restarts=1),
            limit=RESTART_DEADLINE_S,
            what="engine restarted",
        )
        assert time.monotonic() - killed_at < RESTART_DEADLINE_S
        assert supervisor.health().pid != pid
        scan("K-BACK")

        records: list[ClaimedRecord] = []
        while batch := sender.claim_batch():
            records.extend(batch)
            sender.mark_done(r.id for r in batch)
        queued = {r.id: r.payload for r in records if r.type == EventType.SCAN}
        assert {s.scan_id for s in scans} <= set(queued)
        alerts = [r.payload for r in records if r.type == EventType.LOCAL_ALERT]
        assert [a["kind"] for a in alerts] == [LocalAlertKind.ENGINE_RESTART.value]
    finally:
        ui.close()
        supervisor.stop()
        ping.close()
        sender.close()
        shutil.rmtree(root, ignore_errors=True)


def test_engine_tick_thread_dead(make_station: Callable[..., Station]) -> None:
    """The engine's tick stops (thread dead) while frames still arrive: the
    stale state is not passed off as current; scans are Unknown and accepted."""
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    assert station.scan("T-1").outcome is ScanOutcome.ATTENTIVE

    station.run(3.0, tick_engine=False)  # longer than unknown_seconds (2 s)
    answer = station.child.scan_state()
    assert answer.state is AttentionState.UNKNOWN
    result = station.scan("T-2")
    assert result.state is AttentionState.UNKNOWN
    assert result.outcome is ScanOutcome.UNMONITORED
    assert station.child.current_state().state is AttentionState.UNKNOWN

    station.run(1.0)  # the tick is back
    assert station.scan("T-3").outcome is ScanOutcome.ATTENTIVE
    station.assert_scans_recorded()


def test_engine_ipc_unavailable(make_station: Callable[..., Station]) -> None:
    """The engine child stops answering (IPC gone): scans are Unknown and fast,
    the screen shows "Monitoring unavailable", the service raises
    `monitoring_unavailable`, and everything recovers when it is back."""
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    station.run(1.0, view=True)

    station.ipc_server.stop()
    station.run(0.5)
    for n in range(3):
        result = station.scan(f"I-{n}")
        assert result.state is AttentionState.UNKNOWN
        assert result.outcome is ScanOutcome.UNMONITORED
    station.run(6.0, view=True)
    assert station.view().scan.unavailable_text != ""
    station.run(ALERT_AFTER_S)
    (alert,) = station.alerts(LocalAlertKind.MONITORING_UNAVAILABLE)
    assert alert["detail"]["engine_status"] == EngineStatus.UNKNOWN.value

    # The child answers again (a restarted child binds the same address).
    station.ipc_server = IpcServer(station.transport, station.child, control=station.child)
    station.ipc_server.start()
    _wait(lambda: station.service_ipc.ping() is CallStatus.OK, what="IPC back")
    station.run(2.0, view=True)
    assert station.scan("I-BACK").outcome is ScanOutcome.ATTENTIVE
    station.run(1.0, view=True)
    assert station.view().scan.unavailable_text == ""
    station.assert_scans_recorded()


def test_engine_answer_hangs(
    make_station: Callable[..., Station], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine hangs inside the state query: the 150 ms IPC deadline answers Unknown."""
    import threading

    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    release = threading.Event()
    real = station.child.scan_state

    def hang() -> StationState:
        release.wait(5.0)
        return real()

    monkeypatch.setattr(station.child, "scan_state", hang)
    try:
        for n in range(3):
            result = station.scan(f"H-{n}")
            assert result.state is AttentionState.UNKNOWN
            assert result.outcome is ScanOutcome.UNMONITORED
    finally:
        release.set()
    monkeypatch.undo()
    _wait(lambda: station.ui.flow is not None and station.ui.flow.health().scans == 3)
    station.run(0.5)
    _wait(
        lambda: station.child.scan_state().state is AttentionState.ATTENTIVE,
        what="engine answering",
    )
    # The client reconnects after the late answers; a later scan is monitored again.
    _wait(
        lambda: (
            station.ui.flow is not None
            and station.ui.flow.on_barcode("H-PROBE").outcome is ScanOutcome.ATTENTIVE
        ),
        what="monitored scan",
    )
    station.assert_scans_recorded()


# -- Server unreachable -----------------------------------------------------------


def test_server_unreachable(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    client = station.sync.client
    station.device.inject("/", Fault.DROP_REQUEST, times=1_000_000)

    for n in range(5):
        station.scan(f"N-{n}")
        client.tick()
        station.run(1.0)
    assert SyncFlag.OFFLINE in client.health().flags
    assert station.device.items == {}
    stats = station.sender.stats()
    assert stats.depth >= 5  # buffered locally, encrypted

    # The server comes back: the buffer drains, each scan arrives exactly once.
    station.device.rules.clear()
    ids = {s.scan_id for s in station.scans}
    for _ in range(200):
        client.tick()
        if ids <= set(station.device.unique_items(EventType.SCAN)):
            break
        station.clock.advance(10.0)
    assert ids <= set(station.device.unique_items(EventType.SCAN))
    assert all(station.device.item_arrivals[i] >= 1 for i in ids)
    assert SyncFlag.OFFLINE not in client.health().flags
    assert station.device.heartbeats  # live status resumed
    for result in station.scans:
        assert _server_payload(station, result.scan_id)["barcode"] == result.barcode


# -- Disk full ----------------------------------------------------------------------


def _warned_scan_with_burst(station: Station, code: str) -> ScanResult:
    result = station.scan(code)
    assert result.warn and result.event_id is not None
    event_id = result.event_id
    _wait(
        lambda: station.child.burst_outcome(event_id).status != "unknown",
        what="burst requested",
    )
    station.run(2.0)  # the post frames
    _wait(lambda: station.child.burst_outcome(event_id).status == "done", what="burst stored")
    _wait(
        lambda: any(
            r.payload.get("scan_id") == str(result.scan_id)
            for r in station.queued(EventType.ATTENTION_EVENT)
        ),
        what="warned-scan event queued",
    )
    return result


def _event(station: Station, result: ScanResult) -> Json:
    (payload,) = [
        r.payload
        for r in station.queued(EventType.ATTENTION_EVENT)
        if r.payload.get("scan_id") == str(result.scan_id)
    ]
    return payload


def test_disk_full_purges_oldest_images_and_alerts(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=2)
    station.cams.ear = ASLEEP
    station.run(8.0)  # asleep before login: the episode key frame is withheld (no worker)
    station.login()
    station.run(3.0)  # the burst's pre frames must be after the login (capture gate)
    assert station.child.scan_state().outcome is ScanOutcome.WARNED

    first = _warned_scan_with_burst(station, "D-1")
    first_event = _event(station, first)
    assert first_event["images_withheld"] == ImagesWithheld.NONE.value
    assert first_event["image_ids"]
    assert station.alerts(LocalAlertKind.DISK_FULL) == []

    # The volume is full (free space at the reserve): the oldest unsent images go first.
    station.free_bytes = 0
    second = _warned_scan_with_burst(station, "D-2")
    second_event = _event(station, second)
    assert second_event["image_ids"], "the new event's images were saved"
    (alert,) = station.alerts(LocalAlertKind.DISK_FULL)
    assert alert["kind"] == LocalAlertKind.DISK_FULL.value

    store = EvidenceStore.open(station.data / "evidence", station.keys, station.clock)
    try:
        assert first.event_id is not None and second.event_id is not None
        purged = store.event_images(first.event_id)
        assert len(purged) == len(first_event["image_ids"])
        assert all(not i.present and i.removed_reason is RemovalReason.DISK_FULL for i in purged)
        assert store.withheld_reason(first.event_id) is ImagesWithheld.DISK_FULL
        kept = store.event_images(second.event_id)
        assert kept and all(i.present for i in kept)
    finally:
        store.close()
    station.scan("D-3")
    station.assert_scans_recorded()


def test_disk_full_for_metadata_keeps_scans_in_the_journal(
    make_station: Callable[..., Station], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue database cannot be written (disk full): the scan is still
    accepted and durable in the scan journal, counted in the flow's health,
    and moved to the queue when space is back."""
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    flow = station.ui.flow
    assert flow is not None
    writer: Any = flow._queue  # Any: the RecordMirror or EventQueue the flow writes to

    def full(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(writer, "enqueue", full)
    result = station.scan("M-1")
    assert result.outcome is ScanOutcome.ATTENTIVE
    health = flow.health()
    assert health.queue_write_failures >= 1
    assert health.journal_backlog >= 1
    assert result.scan_id not in station.queued_scans()

    monkeypatch.undo()
    flow.recover_journal()
    station.scan("M-2")
    station.assert_scans_recorded()
    assert flow.health().journal_backlog == 0


# -- Clock drift ---------------------------------------------------------------------


def test_clock_drift(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    client = station.sync.client
    station.device.skew = timedelta(seconds=5)  # the PC clock is 5 s behind the server

    for n in range(3):
        client.tick()
        station.scan(f"K-{n}")
        station.run(2.5)
    flags = client.health().flags
    assert SyncFlag.CLOCK_DRIFT in flags
    assert abs(client.health().clock_offset_ms - 5000) < 100
    for _ in range(20):
        client.tick()
        station.clock.advance(2.5)
    alerts = [
        _server_payload(station, i)
        for i in station.device.unique_items(EventType.LOCAL_ALERT)
        if _server_payload(station, i)["kind"] == LocalAlertKind.CLOCK_DRIFT.value
    ]
    assert len(alerts) == 1  # one per episode
    assert abs(int(alerts[0]["detail"]["offset_ms"]) - 5000) < 100
    # The offset is reported with the live status.
    assert abs(station.device.heartbeats[-1].clock_offset_ms - 5000) < 100
    for result in station.scans:
        assert result.scan_id in station.device.unique_items(EventType.SCAN)


# -- gaps for the architect (strict xfails) ------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "architect (engine adapter/supervision): a live engine child whose engine has "
        "stopped ticking reports health RUNNING/monitoring available, so neither the "
        "supervisor (child heartbeat still beats) nor the monitoring alerts notice; "
        "scope 4.4 'engine crash: auto-restart within seconds' is not met for a dead "
        "tick thread. PROGRESS 2.11."
    ),
)
def test_dead_engine_tick_is_reported_and_restarted(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    station.run(3.0, tick_engine=False)
    assert station.child.scan_state().state is AttentionState.UNKNOWN  # this part holds
    health = station.child.health()
    assert not health.monitoring_available
    assert health.status not in (EngineStatus.RUNNING, EngineStatus.DEGRADED)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "contract 6 'stamps records with corrected UTC': only the sync client's own "
        "records use CorrectedClock; the UI (scans, sessions) and the engine child "
        "(episodes, evidence captured_at) stamp the raw PC clock. Fixing it moves the "
        "time base the capture gate compares (pushed session start vs burst start), so "
        "it is handed to the architect. PROGRESS 2.9 open item, 2.11."
    ),
)
def test_scan_records_are_stamped_with_corrected_utc(make_station: Callable[..., Station]) -> None:
    station = make_station(cameras=2)
    _awake_and_logged_in(station)
    client = station.sync.client
    station.device.skew = timedelta(seconds=5)
    for _ in range(3):
        client.tick()
        station.clock.advance(2.5)
    assert SyncFlag.CLOCK_DRIFT in client.health().flags
    result = station.scan("UTC-1")
    (record,) = [r for r in station.queued(EventType.SCAN) if r.id == result.scan_id]
    expected = station.clock.now() + timedelta(seconds=5)
    assert abs((record.occurred_at - expected).total_seconds()) < 1.0
