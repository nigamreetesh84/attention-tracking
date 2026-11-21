"""UI assembly against the service's engine child over IPC (2.9-pre wiring, no Qt)."""

import shutil
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
from ag_workstation.auth import StaticRosterSource
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.consent import CaptureOutcome, GateReason
from ag_workstation.engine_api import BurstKind, ConfigApplyResult
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.service import IpcClient, IpcServer, PipeTransport
from ag_workstation.service.station import BurstStatus, PushedSession
from ag_workstation.ui.app import DEV_EMPLOYEE_ID, DEV_PIN, assemble, dev_bundle
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
