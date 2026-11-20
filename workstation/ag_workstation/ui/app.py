"""Workstation app entry point: `python -m ag_workstation.ui.app [--dev] [--windowed]`.

`assemble` wires the real scan flow, journal, encrypted queue, session
manager and PIN login to an engine-side source. The source is the service
over IPC when `AG_IPC_AUTHKEY` (hex) is set and the service answers; else,
with `--dev`, the `FakeStationEngine` (with a dev roster: employee ID
`DEV001`, PIN `1234`); else a source that is always unreachable, so the
screen runs fail-open with "Monitoring unavailable".

Release wiring (DPAPI queue keys, roster from the synced config bundle,
evidence bursts and config over IPC) belongs to the service and sync tasks;
here the queue key is per run and evidence requests are dropped unless the
fake engine is used.
"""

import argparse
import logging
import os
import secrets
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from ag_shared.api import ConfigBundle
from ag_shared.config import EngineConfig
from ag_shared.health import EngineStatus
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState
from argon2 import PasswordHasher

from ag_workstation.auth import (
    AuthStateStore,
    LoginMethodRegistry,
    LoginThrottle,
    PinLoginMethod,
    RosterSource,
    SessionManager,
    StaticRosterSource,
)
from ag_workstation.clock import Clock, SystemClock
from ag_workstation.engine_api import (
    BurstKind,
    ConfigApplyResult,
    EngineHealth,
    EngineUnavailableError,
    EvidenceRequester,
    StationState,
)
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.queue import EventQueue, InMemoryKeyProvider, PayloadCipher
from ag_workstation.scan import KeyboardWedgeParser, ScanFlow, ScanJournal
from ag_workstation.service import IpcClient, PipeTransport, default_address
from ag_workstation.service.ipc_client import CallStatus
from ag_workstation.ui.app_presenter import AppPresenter
from ag_workstation.ui.backends import SessionManagerBackend
from ag_workstation.ui.login_presenter import LoginPresenter
from ag_workstation.ui.runner import ThreadRunner
from ag_workstation.ui.scan_presenter import ScanPresenter
from ag_workstation.ui.status import StatusPoller

logger = logging.getLogger(__name__)

AUTHKEY_ENV: Final = "AG_IPC_AUTHKEY"
DEV_EMPLOYEE_ID: Final = "DEV001"
DEV_PIN: Final = "1234"
_DEV_STATION: Final = UUID("0193a0b0-0000-7000-8000-0000000de001")
_DEV_WORKER: Final = UUID("0193a0b0-0000-7000-8000-0000000de002")


class UnreachableEngine:
    """No service: every query fails, so the state is Unknown (fail-open)."""

    def scan_state(self) -> StationState:
        """Always unavailable."""
        raise EngineUnavailableError("no service")

    def current_state(self) -> StationState:
        """Always unavailable."""
        raise EngineUnavailableError("no service")

    def health(self) -> EngineHealth:
        """Engine down."""
        return EngineHealth(status=EngineStatus.DOWN, last_error="EngineUnavailableError")


class DroppedEvidence:
    """`EvidenceRequester` when bursts cannot reach the engine yet: the event is
    still queued, with images withheld after the capture wait."""

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        """Drop the request."""
        logger.info("evidence burst not requested (no engine link)")


class AcceptingConfigTarget:
    """`EngineConfigTarget` when the engine is out of process (the service applies config)."""

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        """Accept; the service applies profiles to the real engine."""
        return ConfigApplyResult(True, config.config_version)


def dev_bundle(pin_hash: str) -> ConfigBundle:
    """A one-worker bundle for manual checks (employee `DEV001`)."""
    profile = builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json")
    return ConfigBundle.model_validate(
        {
            "version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "site": {"timezone": "UTC", "settings": {}},
            "workstation": {
                "id": str(_DEV_STATION),
                "name": "Dev station",
                "line_id": str(_DEV_STATION),
                "camera_count": 1,
            },
            "cameras": [{"slot": 1, "role": "primary", "kind": "usb", "uri": "0"}],
            "ppe_profile": profile,
            "thresholds": {},
            "profile_thresholds": [],
            "roster": [
                {
                    "user_id": str(_DEV_WORKER),
                    "employee_id": DEV_EMPLOYEE_ID,
                    "display_name": "Dev Worker",
                    "pin_hash": pin_hash,
                    "has_consent": True,
                    "ppe_profile_override": None,
                    "preferred_language": None,
                    "is_active": True,
                }
            ],
        }
    )


@dataclass
class Assembly:
    """The presenter and what must be closed on exit."""

    presenter: AppPresenter
    engine_mode: str
    """`ipc`, `fake` or `unreachable`."""
    fake_engine: FakeStationEngine | None = None
    closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        """Close everything, last opened first; errors are logged."""
        for close in reversed(self.closers):
            try:
                close()
            except Exception as exc:
                logger.warning("close failed: %s", type(exc).__name__)
        self.closers.clear()


def _ipc_client(clock: Clock) -> IpcClient | None:
    raw = os.environ.get(AUTHKEY_ENV, "")
    if not raw:
        return None
    try:
        client = IpcClient(PipeTransport(default_address(), bytes.fromhex(raw)), clock)
    except ValueError as exc:
        logger.error("%s is not a valid key: %s", AUTHKEY_ENV, type(exc).__name__)
        return None
    if client.ping() is CallStatus.OK:
        return client
    client.close()
    return None


def assemble(
    *,
    data_dir: Path,
    dev: bool,
    clock: Clock | None = None,
    ipc: IpcClient | None = None,
    roster: RosterSource | None = None,
    hasher: PasswordHasher | None = None,
    dev_state: AttentionState = AttentionState.ATTENTIVE,
) -> Assembly:
    """Wire the scan and login screens' presenters (no Qt)."""
    clock = clock or SystemClock()
    data_dir.mkdir(parents=True, exist_ok=True)
    closers: list[Callable[[], None]] = []
    fake: FakeStationEngine | None = None
    engine: IpcClient | FakeStationEngine | UnreachableEngine
    evidence: EvidenceRequester
    if ipc is not None:
        engine, evidence, mode = ipc, DroppedEvidence(), "ipc"
        closers.append(ipc.close)
    elif dev:
        fake = FakeStationEngine(clock)
        fake.set_state(dev_state)
        engine, evidence, mode = fake, fake, "fake"
    else:
        engine, evidence, mode = UnreachableEngine(), DroppedEvidence(), "unreachable"

    hasher = hasher or PasswordHasher()
    if roster is None:
        roster = StaticRosterSource(dev_bundle(hasher.hash(DEV_PIN)) if dev else None)
    # Per-run key: release keys come from DPAPI through the service (PLAN 2.2).
    keys = InMemoryKeyProvider(secrets.token_bytes(32))
    queue = EventQueue.open(data_dir / "queue.db", keys, clock)
    closers.append(queue.close)
    journal = ScanJournal.open(data_dir / "scan.jnl", PayloadCipher(keys))
    closers.append(journal.close)
    auth_state = AuthStateStore.open(data_dir / "auth.db")
    closers.append(auth_state.close)

    registry = LoginMethodRegistry()
    registry.register(
        PinLoginMethod(roster, LoginThrottle(auth_state, clock), clock, hasher=hasher)
    )
    manager = SessionManager(
        registry=registry,
        roster=roster,
        records=queue,
        engine=fake if fake is not None else AcceptingConfigTarget(),
        state=auth_state,
        clock=clock,
    )
    manager.recover()
    closers.append(manager.shutdown)
    flow = ScanFlow(
        queue=queue,
        journal=journal,
        engine=engine,
        evidence=evidence,
        sessions=manager,
        clock=clock,
    )
    flow.recover_journal()
    closers.append(flow.close)
    backend = SessionManagerBackend(manager)

    scan_runner = ThreadRunner("scan")
    poll_runner = ThreadRunner("poll")
    auth_runner = ThreadRunner("auth")
    closers.extend([scan_runner.close, poll_runner.close, auth_runner.close])
    scan = ScanPresenter(
        flow=flow,
        poller=StatusPoller(engine, poll_runner, clock),
        sessions=backend,
        runner=scan_runner,
        clock=clock,
    )
    login = LoginPresenter(backend=backend, runner=auth_runner, clock=clock)
    presenter = AppPresenter(scan=scan, login=login, parser=KeyboardWedgeParser(clock))
    return Assembly(presenter, mode, fake, closers)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ag-workstation-ui")
    parser.add_argument("--dev", action="store_true", help="fake engine and dev roster")
    parser.add_argument("--windowed", action="store_true", help="do not go full screen")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--sound", type=Path, default=None, help="WAV file for the warning")
    parser.add_argument(
        "--dev-state",
        choices=[s.value for s in AttentionState],
        default=AttentionState.ATTENTIVE.value,
        help="state the fake engine reports",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the scan screen."""
    from PySide6.QtWidgets import QApplication

    from ag_workstation.ui.qt_views import MainWindow, SoundPlayer

    args = _parse(argv)
    logging.basicConfig(level=logging.INFO)
    clock = SystemClock()
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="attentionguard-ui-"))
    assembly = assemble(
        data_dir=data_dir,
        dev=args.dev,
        clock=clock,
        ipc=_ipc_client(clock),
        dev_state=AttentionState(args.dev_state),
    )
    logger.info("engine source: %s; data in %s", assembly.engine_mode, data_dir)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(assembly.presenter, sound=SoundPlayer(args.sound))
    if args.windowed:
        window.show()
    else:
        window.showFullScreen()
    try:
        return int(app.exec())
    finally:
        assembly.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
