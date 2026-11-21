"""UI assembly against the service's engine child over IPC (2.9-pre wiring, no Qt)."""

import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import ImagesWithheld
from ag_shared.config import EngineConfig
from ag_shared.states import AttentionState
from ag_workstation.auth import PIN_METHOD_NAME, PinCredentials, StaticRosterSource
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.consent import CaptureOutcome, GateReason
from ag_workstation.engine_api import BurstKind, ConfigApplyResult
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.queue import EventQueue, InMemoryKeyProvider
from ag_workstation.queue.keys import KeyUnavailableError
from ag_workstation.service import IpcClient, IpcServer, PipeTransport
from ag_workstation.service.station import BurstStatus, PushedSession
from ag_workstation.ui.app import DEV_EMPLOYEE_ID, DEV_PIN, Assembly, assemble, dev_bundle
from argon2 import PasswordHasher

AUTHKEY = b"u" * 32
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def _wait(cond: Callable[[], bool], limit: float = 5.0) -> None:
    end = time.monotonic() + limit
    while not cond():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.01)


class Control:
    def __init__(self) -> None:
        self.sessions: list[PushedSession | None] = []
        self.configs: list[EngineConfig] = []
        self.bursts: list[tuple[UUID, BurstKind]] = []

    def set_session(self, session: PushedSession | None, ttl_s: float) -> None:
        self.sessions.append(session)

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        self.configs.append(config)
        return ConfigApplyResult(True, config.config_version)

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        self.bursts.append((event_id, kind))

    def burst_outcome(self, event_id: UUID) -> BurstStatus:
        outcome = CaptureOutcome(event_id, (), ImagesWithheld.NO_CONSENT, GateReason.NO_CONSENT, 6)
        return BurstStatus("done", outcome)


@pytest.fixture
def transport() -> Iterator[PipeTransport]:
    path = Path(tempfile.mkdtemp(prefix="agui"))
    if sys.platform == "win32":
        yield PipeTransport(rf"\\.\pipe\agui-{uuid.uuid4().hex}", AUTHKEY)
    else:
        yield PipeTransport(str(path / "u.sock"), AUTHKEY)
    shutil.rmtree(path, ignore_errors=True)


def test_ui_talks_to_the_engine_child_for_session_config_and_bursts(
    transport: PipeTransport, tmp_path: Path
) -> None:
    engine = FakeStationEngine(SystemClock())
    engine.set_state(AttentionState.DROWSY)
    control = Control()
    server = IpcServer(transport, engine, control=control)
    server.start()
    roster = StaticRosterSource(dev_bundle(FAST_HASHER.hash(DEV_PIN)))
    built = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        ipc=IpcClient(transport, FakeClock()),
        control=IpcClient(transport, FakeClock(), timeout_s=1.0),
        roster=roster,
        hasher=FAST_HASHER,
    )
    try:
        assert built.engine_mode == "ipc"
        # The station config from the bundle and the (empty) session are pushed.
        _wait(lambda: len(control.configs) >= 1 and control.sessions[:1] == [None])
        presenter = built.presenter
        presenter.key("SCAN1\r")
        _wait(lambda: presenter.tick().screen.value == "login")
        presenter.login.key(f"{DEV_EMPLOYEE_ID}\t{DEV_PIN}\r")
        _wait(lambda: any(s is not None for s in control.sessions))
        pushed = next(s for s in control.sessions if s is not None)
        assert pushed.has_consent is True
        # A warned scan asks the child for a burst (consent was granted).
        _wait(lambda: presenter.tick().screen.value == "scan")
        presenter.key("SCAN2\r")
        _wait(lambda: len(control.bursts) == 1)
        assert control.bursts[0][1] is BurstKind.SCAN_WARNING
    finally:
        built.close()
        server.stop()


def test_without_a_control_client_bursts_are_dropped_not_raised(tmp_path: Path) -> None:
    class StubIpc:
        def scan_state(self) -> object:
            raise RuntimeError("down")

        def current_state(self) -> object:
            raise RuntimeError("down")

        def health(self) -> object:
            raise RuntimeError("down")

        def close(self) -> None:
            pass

    built = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        ipc=StubIpc(),  # type: ignore[arg-type]  # duck-typed IpcClient
        hasher=FAST_HASHER,
    )
    assert built.engine_mode == "ipc"
    built.presenter.key("SCAN1\r")  # the scan is still taken (fail-open)
    _wait(lambda: not built.presenter.tick().scan.scans_pending)
    assert built.presenter.tick().scan.last_item_text.endswith("SCAN1")
    built.close()


# -- 2.R C1/H6: the scan screen starts and records whatever storage does -------------

KEY = bytes(range(32))


class BrokenKeys:
    """DPAPI cannot unwrap the key file (R6: disk moved, image cloned)."""

    def data_key(self) -> bytes:
        raise KeyUnavailableError("DPAPI could not unwrap the key")


def _scan(built: Assembly, code: str) -> None:
    built.presenter.key(f"{code}\r")
    _wait(lambda: not built.presenter.tick().scan.scans_pending)


def _queued(data_dir: Path) -> list[dict[str, object]]:
    queue = EventQueue.open(data_dir / "queue.db", InMemoryKeyProvider(KEY), FakeClock())
    try:
        return [{"type": r.type, **r.payload} for r in queue.claim_batch(max_items=200)]
    finally:
        queue.close()


def test_scans_are_recorded_without_a_data_key_and_sealed_at_the_next_start(
    tmp_path: Path,
) -> None:
    built = assemble(
        data_dir=tmp_path, dev=False, clock=FakeClock(), keys=BrokenKeys(), hasher=FAST_HASHER
    )
    try:
        _scan(built, "NOKEY-1")
        assert built.flow is not None
        health = built.flow.health()
        assert health.records_lost == 0
        assert health.journal_backlog == 1
        assert "data_key_unavailable" in built.degraded
    finally:
        built.close()
    clear = tmp_path / "scan-clear.jnl"
    assert b"NOKEY-1" in clear.read_bytes()  # the bounded plaintext case
    # Next start with the key: sealed into the queue, plaintext erased.
    again = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        keys=InMemoryKeyProvider(KEY),
        hasher=FAST_HASHER,
    )
    again.close()
    assert clear.stat().st_size == 0
    assert [i["barcode"] for i in _queued(tmp_path) if i["type"] == "scan"] == ["NOKEY-1"]


def test_scans_are_recorded_when_the_queue_cannot_open(tmp_path: Path) -> None:
    (tmp_path / "queue.db").write_bytes(b"this is not a database" * 100)
    built = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        keys=InMemoryKeyProvider(KEY),
        hasher=FAST_HASHER,
    )
    try:
        _scan(built, "NOQUEUE-1")
        assert built.flow is not None
        assert built.flow.health().records_lost == 0
        assert built.flow.health().journal_backlog == 1
        assert "queue_unavailable" in built.degraded
    finally:
        built.close()
    assert b"NOQUEUE-1" not in (tmp_path / "scan.jnl").read_bytes()  # sealed
    (tmp_path / "queue.db").unlink()
    assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        keys=InMemoryKeyProvider(KEY),
        hasher=FAST_HASHER,
    ).close()
    assert [i["barcode"] for i in _queued(tmp_path) if i["type"] == "scan"] == ["NOQUEUE-1"]


def test_scans_are_recorded_when_the_journal_and_auth_store_cannot_open(
    tmp_path: Path,
) -> None:
    (tmp_path / "scan.jnl").mkdir()
    (tmp_path / "auth.db").mkdir()
    built = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        keys=InMemoryKeyProvider(KEY),
        roster=StaticRosterSource(dev_bundle(FAST_HASHER.hash(DEV_PIN))),
        hasher=FAST_HASHER,
    )
    try:
        assert {"journal_unavailable", "auth_store_unavailable"} <= set(built.degraded)
        assert built.manager is not None
        login = built.manager.login(PIN_METHOD_NAME, PinCredentials(DEV_EMPLOYEE_ID, DEV_PIN))
        assert login.session is not None  # in-memory lockout store
        _scan(built, "NOJOURNAL-1")
    finally:
        built.close()
    assert [i["barcode"] for i in _queued(tmp_path) if i["type"] == "scan"] == ["NOJOURNAL-1"]


def test_session_records_fall_back_to_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2.R H6: a session_start the queue refused is journaled and recovered, so
    the session's scans are not orphaned on the server."""
    built = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        keys=InMemoryKeyProvider(KEY),
        roster=StaticRosterSource(dev_bundle(FAST_HASHER.hash(DEV_PIN))),
        hasher=FAST_HASHER,
    )
    try:
        original = EventQueue.enqueue

        def locked(self: EventQueue, *args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(EventQueue, "enqueue", locked)
        assert built.manager is not None and built.flow is not None
        login = built.manager.login(PIN_METHOD_NAME, PinCredentials(DEV_EMPLOYEE_ID, DEV_PIN))
        assert login.session is not None
        assert built.flow.health().journal_backlog == 1
        monkeypatch.setattr(EventQueue, "enqueue", original)
        assert built.flow.recover_journal().enqueued == 1
        assert built.recovery is not None  # and it runs by itself while the app runs
    finally:
        built.close()
    starts = [i for i in _queued(tmp_path) if i["type"] == "session_start"]
    assert len(starts) == 1
