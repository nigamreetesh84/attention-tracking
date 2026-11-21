"""Workstation app entry point: `python -m ag_workstation.ui.app [--dev] [--windowed]`.

`assemble` wires the real scan flow, journal, encrypted queue, session
manager and PIN login to an engine-side source.

- Default: the real engine in the service's engine child, over IPC
  (`AG_IPC_AUTHKEY`, hex). The clients are created whether or not the service
  answers yet and reconnect on every call, so a service that starts later (or
  restarts) is picked up; until then the state is Unknown (fail-open). Two
  clients are used: one for the 150 ms state queries, one for the control
  operations (session push, PPE config, evidence bursts and their outcomes),
  so a slow control call never costs a scan its query.
- `--dev`: the in-process `FakeStationEngine` with a dev roster (employee ID
  `DEV001`, PIN `1234`).
- No authkey and no `--dev`: a source that is always unreachable, so the
  screen runs fail-open with "Monitoring unavailable".

The scan flow, the scan journal and login stay in this process (rule 1: a
scan is recorded even with the service dead). The capture gate is asked here
before a burst is requested (closed: nothing is requested) and enforced
again in the engine child, which writes the frames.

With the shared data key (`AG_DATA_KEY`, or DPAPI on Windows) the roster is
the config bundle the service's sync client stored, kept current by a
`SyncConsumer` thread that also runs supervisor session commands (2.9).
Without it the queue key is per run and there is no roster without `--dev`.

"My record" (2.10) reads `PersonalRecordStore` (`AG_DATA_DIR/my_record.db`),
fed by a `RecordMirror` around the queue that the scan flow and the session
manager write to (the engine child mirrors its episodes into the same file).
If the record cannot be opened the screen is not offered; scanning is not
affected. Disputes are queued here by `DisputeService` (enqueue only).
"""

import argparse
import logging
import os
import secrets
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

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
from ag_workstation.consent import CaptureGate
from ag_workstation.engine_api import (
    BurstKind,
    ConfigApplyResult,
    EngineHealth,
    EngineUnavailableError,
    EvidenceRequester,
    StationState,
)
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.queue import EventQueue, InMemoryKeyProvider, KeyProvider, PayloadCipher
from ag_workstation.record import RECORD_FILE, DisputeService, PersonalRecordStore, RecordMirror
from ag_workstation.scan import KeyboardWedgeParser, ScanFlow, ScanJournal
from ag_workstation.service import IpcClient, PipeTransport, default_address
from ag_workstation.service.engine_factory import DATA_DIR_ENV, key_provider_from_env
from ag_workstation.service.remote import RemoteEvidence, SessionPusher
from ag_workstation.sync.commands import INBOX_FILE, CommandInbox, CommandPump
from ag_workstation.sync.config_store import BundleFileWatcher, bundle_path
from ag_workstation.sync.consumer import SyncConsumer
from ag_workstation.ui.app_presenter import AppPresenter
from ag_workstation.ui.backends import SessionManagerBackend
from ag_workstation.ui.login_presenter import LoginPresenter
from ag_workstation.ui.record_presenter import RecordPresenter, RecordPresenterConfig
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
    flow: ScanFlow | None = None
    """The scan flow the presenter records through (tests, health)."""
    manager: SessionManager | None = None
    pusher: SessionPusher | None = None
    """Pushes the open session to the engine child (None without a control client)."""
    closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        """Close everything, last opened first; errors are logged."""
        for close in reversed(self.closers):
            try:
                close()
            except Exception as exc:
                logger.warning("close failed: %s", type(exc).__name__)
        self.closers.clear()


CONTROL_TIMEOUT_S: Final = 1.0
"""Deadline of the control calls (session push, burst request and poll)."""


def _ipc_client(clock: Clock, *, timeout_s: float | None = None) -> IpcClient | None:
    """A client for the service's engine child, or None without a valid authkey.

    It does not wait for the service: calls fail fast (Unknown) until it
    answers and reconnect by themselves."""
    raw = os.environ.get(AUTHKEY_ENV, "")
    if not raw:
        return None
    try:
        transport = PipeTransport(default_address(), bytes.fromhex(raw))
    except ValueError as exc:
        logger.error("%s is not a valid key: %s", AUTHKEY_ENV, type(exc).__name__)
        return None
    if timeout_s is None:
        return IpcClient(transport, clock)
    return IpcClient(transport, clock, timeout_s=timeout_s)


def _push_station_config(control: IpcClient, roster: RosterSource) -> None:
    """Send the station engine config from the bundle (in the background)."""
    bundle = roster.current_bundle()
    if bundle is None:
        return
    try:
        config = bundle.engine_config()
    except Exception as exc:
        logger.error("station engine config not built (%s)", type(exc).__name__)
        return
    result = control.apply_config(config)
    if not result.accepted:
        logger.warning("engine child did not take the station config (%s)", result.reason)


def _sync_consumer(
    data_dir: Path,
    clock: Clock,
    watcher: BundleFileWatcher | None,
    roster: RosterSource,
    manager: SessionManager,
    control: IpcClient | None,
) -> SyncConsumer | None:
    """Bundle updates and session commands from the service's sync client."""
    try:
        inbox = CommandInbox.open(data_dir / INBOX_FILE, clock)
    except Exception as exc:
        logger.error("command inbox unavailable (%s)", type(exc).__name__)
        return None
    target = roster if isinstance(roster, StaticRosterSource) else None

    def on_bundle(bundle: ConfigBundle) -> None:
        if control is not None:
            _push_station_config(control, roster)
        manager.on_bundle_applied()

    return SyncConsumer(
        watcher=watcher if target is not None else None,
        roster=target if target is not None else StaticRosterSource(),
        pump=CommandPump(inbox, manager),
        on_bundle=on_bundle,
        on_close=inbox.close,
    )


def assemble(
    *,
    data_dir: Path,
    dev: bool,
    clock: Clock | None = None,
    ipc: IpcClient | None = None,
    control: IpcClient | None = None,
    roster: RosterSource | None = None,
    keys: KeyProvider | None = None,
    hasher: PasswordHasher | None = None,
    dev_state: AttentionState = AttentionState.ATTENTIVE,
) -> Assembly:
    """Wire the scan and login screens' presenters (no Qt).

    `ipc` answers state queries; `control` (a second client) carries the
    session push, PPE config and evidence bursts to the engine child."""
    clock = clock or SystemClock()
    data_dir.mkdir(parents=True, exist_ok=True)
    closers: list[Callable[[], None]] = []
    fake: FakeStationEngine | None = None
    engine: IpcClient | FakeStationEngine | UnreachableEngine
    evidence: EvidenceRequester
    remote: RemoteEvidence | None = None
    if ipc is not None:
        engine, evidence, mode = ipc, DroppedEvidence(), "ipc"
        closers.append(ipc.close)
        if control is not None:
            closers.append(control.close)
            remote = RemoteEvidence(control, monotonic=clock.monotonic)
            evidence = remote
    elif dev:
        fake = FakeStationEngine(clock)
        fake.set_state(dev_state)
        engine, evidence, mode = fake, fake, "fake"
    else:
        engine, evidence, mode = UnreachableEngine(), DroppedEvidence(), "unreachable"

    hasher = hasher or PasswordHasher()
    # Shared with the engine child when configured (AG_DATA_KEY / DPAPI);
    # otherwise a per-run key (the queue is then this run's only).
    keys = keys or key_provider_from_env(os.environ, data_dir if not dev else None)
    # With the service's shared data key, the roster comes from the bundle
    # the sync client stored, and session commands from its inbox (2.9).
    synced = keys is not None and not dev
    watcher: BundleFileWatcher | None = None
    if roster is None:
        static = StaticRosterSource(dev_bundle(hasher.hash(DEV_PIN)) if dev else None)
        if synced and keys is not None:
            watcher = BundleFileWatcher(bundle_path(data_dir), keys)
            static.set_bundle(watcher.poll())
        roster = static
    if keys is None or dev:
        keys = InMemoryKeyProvider(secrets.token_bytes(32))
    # With a shared queue the service parent is the single sender: this
    # process only enqueues and must not reset the sender's in-flight records.
    queue = EventQueue.open(data_dir / "queue.db", keys, clock, recover_sending=not synced)
    closers.append(queue.close)
    record_store = _open_record(data_dir, keys, clock)
    # Writers go through the mirror; it closes after them (closers run in reverse).
    writer: EventQueue | RecordMirror = queue
    if record_store is not None:
        closers.append(record_store.close)
        writer = RecordMirror(queue, record_store)
        closers.append(writer.close)
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
        records=writer,
        engine=fake if fake is not None else (control or AcceptingConfigTarget()),
        state=auth_state,
        clock=clock,
    )
    manager.recover()
    closers.append(manager.shutdown)
    # Asked before a burst is requested (closed: nothing is requested); the
    # engine child enforces its own gate where the frames are written.
    gate = CaptureGate(sessions=manager, bundles=roster, clock=clock)
    flow = ScanFlow(
        queue=writer,
        journal=journal,
        engine=engine,
        evidence=evidence,
        sessions=manager,
        clock=clock,
        gate=gate,
    )
    flow.recover_journal()
    closers.append(flow.close)
    pusher: SessionPusher | None = None
    if remote is not None and control is not None:
        remote.bind(flow.on_capture_outcome)
        remote.start()
        closers.append(remote.close)
        pusher = SessionPusher(control, manager)
        pusher.start()
        closers.append(pusher.close)
        threading.Thread(
            target=_push_station_config, args=(control, roster), name="push-config", daemon=True
        ).start()
    backend = SessionManagerBackend(
        manager, on_change=pusher.push_now if pusher is not None else None
    )
    if synced:
        consumer = _sync_consumer(data_dir, clock, watcher, roster, manager, control)
        if consumer is not None:
            consumer.start()
            closers.append(consumer.close)

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
    record: RecordPresenter | None = None
    if record_store is not None:
        disputes = DisputeService(record_store, queue, clock)
        disputes.recover()
        record_runner = ThreadRunner("record")
        closers.append(record_runner.close)
        record = RecordPresenter(
            source=record_store,
            disputes=disputes,
            sessions=backend,
            runner=record_runner,
            clock=clock,
            timezone=lambda: _site_timezone(roster),
            config=RecordPresenterConfig(days_shown=record_store.retention.days),
        )
    presenter = AppPresenter(
        scan=scan, login=login, parser=KeyboardWedgeParser(clock), record=record
    )
    return Assembly(
        presenter, mode, fake, flow=flow, manager=manager, pusher=pusher, closers=closers
    )


def _open_record(data_dir: Path, keys: KeyProvider, clock: Clock) -> PersonalRecordStore | None:
    """The worker's local record, or None (the "My record" screen is then not offered)."""
    try:
        return PersonalRecordStore.open(data_dir / RECORD_FILE, keys, clock)
    except Exception as exc:
        logger.error("my record unavailable (%s); screen not offered", type(exc).__name__)
        return None


def _site_timezone(roster: RosterSource) -> tzinfo:
    """The site timezone from the applied bundle (display only), else UTC."""
    bundle = roster.current_bundle()
    return ZoneInfo(bundle.site.timezone) if bundle is not None else UTC


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
    shared = os.environ.get(DATA_DIR_ENV, "").strip()
    data_dir = (
        args.data_dir
        or (Path(shared) if shared and not args.dev else None)
        or Path(tempfile.mkdtemp(prefix="attentionguard-ui-"))
    )
    assembly = assemble(
        data_dir=data_dir,
        dev=args.dev,
        clock=clock,
        ipc=None if args.dev else _ipc_client(clock),
        control=None if args.dev else _ipc_client(clock, timeout_s=CONTROL_TIMEOUT_S),
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
