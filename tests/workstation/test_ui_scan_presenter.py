"""Scan screen presenter (PLAN 2.8): every state and banner, monitoring
unavailable, live view, unattributed scans, logout, and non-blocking polls."""

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest
from ag_shared.api import SessionMethod
from ag_shared.health import CameraStatus
from ag_shared.i18n import Translator
from ag_shared.ids import new_uuid7
from ag_shared.states import WARNED_STATES, AttentionState, ScanOutcome, scan_outcome
from ag_workstation.auth import CurrentSession
from ag_workstation.clock import FakeClock
from ag_workstation.engine_api import CameraState, EngineHealth, StationState
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.scan import QueryStatus, ScanMessageKey, ScanResult
from ag_workstation.ui import (
    BannerKind,
    InlineRunner,
    ScanPresenter,
    ScanPresenterConfig,
    ScanViewModel,
    StatusLevel,
    StatusPoller,
    ThreadRunner,
)
from ag_workstation.ui.scan_presenter import STATE_KEYS

T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
TR = Translator("en", strict=True)
WORKER = UUID("0193a0b0-0000-7000-8000-00000000c001")


def session(name: str = "Asha K") -> CurrentSession:
    return CurrentSession(
        session_id=new_uuid7(),
        user_id=WORKER,
        employee_id="EMP001",
        display_name=name,
        has_consent=True,
        preferred_language=None,
        ppe_profile_id=None,
        method=SessionMethod.EMPLOYEE_ID_PIN,
        station_assignment_id=None,
        started_at=T0,
        valid_until=None,
    )


@dataclass
class FakeSessions:
    session: CurrentSession | None = None
    fail_current: bool = False
    fail_logout: bool = False
    logouts: int = 0

    def current(self) -> CurrentSession | None:
        if self.fail_current:
            raise RuntimeError("store broken")
        return self.session

    def logout(self) -> bool:
        self.logouts += 1
        if self.fail_logout:
            raise OSError("queue locked")
        was_open = self.session is not None
        self.session = None
        return was_open


def _message(outcome: ScanOutcome, state: AttentionState, escalated: bool) -> ScanMessageKey:
    if outcome is ScanOutcome.ATTENTIVE:
        return ScanMessageKey.SCAN_OK
    if outcome is ScanOutcome.WARNED:
        return ScanMessageKey.SCAN_WARNED_REPEATED if escalated else ScanMessageKey.SCAN_WARNED
    if state is AttentionState.ON_BREAK:
        return ScanMessageKey.SCAN_ON_BREAK
    return ScanMessageKey.SCAN_UNMONITORED


@dataclass
class FakeFlow:
    """`ScanFlow.on_barcode` stand-in answering with a scripted state."""

    sessions: FakeSessions
    state: AttentionState = AttentionState.ATTENTIVE
    escalated: bool = False
    raises: bool = False
    seen: list[str] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)

    def on_barcode(self, barcode: str) -> ScanResult:
        self.seen.append(barcode)
        self.threads.append(threading.current_thread().name)
        if self.raises:
            raise RuntimeError("bug")
        current = self.sessions.session
        state = self.state if current is not None else AttentionState.UNKNOWN
        outcome = scan_outcome(state)
        return ScanResult(
            scan_id=new_uuid7(),
            barcode=barcode,
            scanned_at=T0,
            outcome=outcome,
            state=state,
            confidence=None,
            query_status=QueryStatus.OK,
            message_key=(
                ScanMessageKey.SCAN_LOGIN_REQUIRED
                if current is None
                else _message(outcome, state, self.escalated)
            ),
            session_id=current.session_id if current else None,
            escalated=self.escalated and outcome is ScanOutcome.WARNED,
            prompt_login=current is None,
        )


class BrokenRunner:
    def submit(self, job: Callable[[], None]) -> None:
        raise RuntimeError("runner closed")


@dataclass
class Rig:
    presenter: ScanPresenter
    clock: FakeClock
    engine: FakeStationEngine
    sessions: FakeSessions
    flow: FakeFlow

    def tick(self) -> ScanViewModel:
        return self.presenter.tick()

    def settle(self) -> ScanViewModel:
        """Two ticks: the first starts a poll (inline), the second sees it."""
        self.presenter.tick()
        return self.presenter.tick()


CONFIG = ScanPresenterConfig(
    warn_banner_s=4.0, escalated_banner_s=8.0, unavailable_after_s=5.0, poll_interval_s=0.5
)


def make_rig(
    *, logged_in: bool = True, runner: object | None = None, poll_runner: object | None = None
) -> Rig:
    clock = FakeClock(start_utc=T0)
    engine = FakeStationEngine(clock)
    sessions = FakeSessions(session() if logged_in else None)
    flow = FakeFlow(sessions)
    presenter = ScanPresenter(
        flow=flow,
        poller=StatusPoller(engine, poll_runner or InlineRunner(), clock, interval_s=0.5),  # type: ignore[arg-type]  # test runners
        sessions=sessions,
        runner=runner or InlineRunner(),  # type: ignore[arg-type]  # test runners
        clock=clock,
        translate=TR.t,
        config=CONFIG,
    )
    return Rig(presenter, clock, engine, sessions, flow)


@pytest.fixture
def rig() -> Rig:
    return make_rig()


# -- status indicator ----------------------------------------------------------

LEVELS = {
    AttentionState.ATTENTIVE: StatusLevel.ATTENTIVE,
    AttentionState.DISTRACTED: StatusLevel.CAUTION,
    AttentionState.DROWSY: StatusLevel.CAUTION,
    AttentionState.SLEEPING: StatusLevel.ALERT,
    AttentionState.ABSENT: StatusLevel.ALERT,
    AttentionState.ON_BREAK: StatusLevel.NEUTRAL,
    AttentionState.UNKNOWN: StatusLevel.NEUTRAL,
}


@pytest.mark.parametrize("state", list(AttentionState))
def test_status_indicator_for_every_state(rig: Rig, state: AttentionState) -> None:
    rig.engine.set_state(state)
    vm = rig.settle()
    assert vm.status_level is LEVELS[state]
    assert vm.status_text == TR.t(STATE_KEYS[state])
    assert vm.unavailable_text == ""
    expected_label = state not in (AttentionState.UNKNOWN, AttentionState.ON_BREAK)
    assert (vm.monitoring_text == TR.t("ui.monitoring_active")) is expected_label


def test_every_state_label_is_in_the_catalog() -> None:
    assert set(STATE_KEYS) == set(AttentionState)
    for key in [*STATE_KEYS.values(), "ui.state.unavailable"]:
        assert TR.has(key)


def test_initial_screen_before_any_poll(rig: Rig) -> None:
    rig.engine.go_down()
    vm = rig.tick()
    assert vm.status_level is StatusLevel.NEUTRAL
    assert vm.status_text == "Unknown"
    assert vm.last_item_text == "No item scanned yet."
    assert vm.message_text == "Ready to scan."
    assert vm.banner_kind is BannerKind.NONE and vm.banner_text == ""
    assert vm.live_view_text == ""
    assert vm.worker_text == "Logged in: Asha K"
    assert vm.logged_in


# -- scans -----------------------------------------------------------------------


def test_attentive_scan_shows_item_and_ok(rig: Rig) -> None:
    rig.presenter.on_barcode("ITEM-001")
    vm = rig.tick()
    assert vm.last_item_text == "Last item: ITEM-001"
    assert vm.message_text == "Scan recorded."
    assert vm.banner_kind is BannerKind.NONE
    assert vm.sound_token == 0
    assert vm.scans_pending == 0


@pytest.mark.parametrize(
    "state",
    sorted(WARNED_STATES),
)
def test_warned_scan_banner_sound_and_auto_dismiss(rig: Rig, state: AttentionState) -> None:
    rig.flow.state = state
    rig.presenter.on_barcode("ITEM-002")
    vm = rig.tick()
    assert vm.banner_kind is BannerKind.WARNING
    assert vm.banner_text == "Please stay attentive."
    assert vm.message_text == "Scan recorded. Please stay focused on your work."
    assert vm.sound_token == 1
    rig.clock.advance(3.9)
    assert rig.tick().banner_kind is BannerKind.WARNING
    rig.clock.advance(0.1)
    vm = rig.tick()
    assert vm.banner_kind is BannerKind.NONE and vm.banner_text == ""
    assert vm.sound_token == 1  # no replay
    assert vm.last_item_text == "Last item: ITEM-002"


def test_each_warned_scan_plays_the_sound_once(rig: Rig) -> None:
    rig.flow.state = AttentionState.DISTRACTED
    for code in ("A1", "A2", "A3"):
        rig.presenter.on_barcode(code)
    assert rig.tick().sound_token == 3


def test_escalated_scan_shows_escalation_longer(rig: Rig) -> None:
    rig.flow.state = AttentionState.SLEEPING
    rig.flow.escalated = True
    rig.presenter.on_barcode("ITEM-003")
    vm = rig.tick()
    assert vm.banner_kind is BannerKind.ESCALATED
    assert vm.banner_text == "Please stay attentive. Your supervisor will check in."
    assert vm.message_text == TR.t("scan.warned_repeated")
    assert vm.sound_token == 1
    rig.clock.advance(5.0)
    assert rig.tick().banner_kind is BannerKind.ESCALATED
    rig.clock.advance(3.0)
    assert rig.tick().banner_kind is BannerKind.NONE


def test_attentive_scan_clears_a_warning_banner(rig: Rig) -> None:
    rig.flow.state = AttentionState.DROWSY
    rig.presenter.on_barcode("W")
    assert rig.tick().banner_kind is BannerKind.WARNING
    rig.flow.state = AttentionState.ATTENTIVE
    rig.presenter.on_barcode("OK")
    assert rig.tick().banner_kind is BannerKind.NONE


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (AttentionState.UNKNOWN, "Scan recorded. Monitoring is not available right now."),
        (AttentionState.ON_BREAK, "Scan recorded during a break."),
    ],
)
def test_unmonitored_scan(rig: Rig, state: AttentionState, message: str) -> None:
    rig.flow.state = state
    rig.presenter.on_barcode("ITEM-004")
    vm = rig.tick()
    assert vm.message_text == message
    assert vm.banner_kind is BannerKind.NONE
    assert vm.sound_token == 0
    assert not rig.presenter.take_login_prompt()


def test_unattributed_scan_prompts_login_once() -> None:
    rig = make_rig(logged_in=False)
    rig.presenter.on_barcode("ITEM-005")
    vm = rig.tick()
    assert vm.message_text == "Scan recorded. Please log in with your employee ID."
    assert vm.last_item_text == "Last item: ITEM-005"
    assert vm.worker_text == "Nobody is logged in."
    assert not vm.logged_in
    assert vm.monitoring_text == ""  # nobody to monitor
    assert rig.presenter.take_login_prompt()
    assert not rig.presenter.take_login_prompt()


def test_last_item_shows_at_once_before_the_result() -> None:
    held: list[Callable[[], None]] = []

    class HoldingRunner:
        def submit(self, job: Callable[[], None]) -> None:
            held.append(job)

    rig = make_rig(runner=HoldingRunner())
    rig.presenter.on_barcode("FAST-1")
    vm = rig.tick()
    assert vm.last_item_text == "Last item: FAST-1"
    assert vm.scans_pending == 1
    assert vm.message_text == "Ready to scan."
    held.pop()()
    vm = rig.tick()
    assert vm.scans_pending == 0
    assert vm.message_text == "Scan recorded."


def test_scan_is_recorded_inline_when_the_runner_is_gone() -> None:
    rig = make_rig(runner=BrokenRunner())
    rig.presenter.on_barcode("ITEM-006")
    assert rig.flow.seen == ["ITEM-006"]
    assert rig.tick().message_text == "Scan recorded."


def test_scan_flow_exception_does_not_break_the_screen(rig: Rig) -> None:
    rig.flow.raises = True
    rig.presenter.on_barcode("ITEM-007")
    vm = rig.tick()
    assert vm.scans_pending == 0
    assert vm.last_item_text == "Last item: ITEM-007"


def test_scans_run_off_the_ui_thread() -> None:
    runner = ThreadRunner("test-scan")
    try:
        rig = make_rig(runner=runner)
        rig.presenter.on_barcode("T-1")
        deadline = time.monotonic() + 2
        while rig.tick().scans_pending and time.monotonic() < deadline:
            time.sleep(0.005)
        assert rig.flow.threads == ["ag-ui-test-scan"]
        assert rig.tick().message_text == "Scan recorded."
    finally:
        runner.close()


# -- monitoring unavailable ---------------------------------------------------------


def test_monitoring_unavailable_when_engine_down_for_n_seconds(rig: Rig) -> None:
    assert rig.settle().status_level is StatusLevel.ATTENTIVE
    rig.engine.go_down()
    rig.clock.advance(0.5)
    vm = rig.settle()
    assert vm.status_level is StatusLevel.NEUTRAL  # a blip only greys the indicator
    assert vm.unavailable_text == ""
    assert vm.monitoring_text == ""
    for _ in range(10):
        rig.clock.advance(0.5)
        vm = rig.settle()
    assert vm.status_level is StatusLevel.UNAVAILABLE
    assert vm.status_text == "Unavailable"
    assert vm.unavailable_text == "Monitoring unavailable. Keep scanning as usual."
    assert vm.monitoring_text == ""
    # Scanning still works.
    rig.presenter.on_barcode("ITEM-008")
    assert rig.tick().last_item_text == "Last item: ITEM-008"
    rig.engine.come_up()
    rig.clock.advance(0.5)
    vm = rig.settle()
    assert vm.unavailable_text == ""
    assert vm.status_level is StatusLevel.ATTENTIVE


def test_monitoring_unavailable_when_no_camera_online(rig: Rig) -> None:
    rig.engine.set_state(
        AttentionState.ATTENTIVE,
        cameras=(CameraState(1, CameraStatus.OFFLINE, AttentionState.UNKNOWN),),
    )
    rig.settle()
    rig.clock.advance(5.0)
    vm = rig.settle()
    assert vm.unavailable_text != ""


def test_monitoring_unavailable_when_unknown_for_n_seconds(rig: Rig) -> None:
    rig.engine.set_state(AttentionState.UNKNOWN)
    rig.settle()
    rig.clock.advance(4.5)
    assert rig.settle().unavailable_text == ""
    rig.clock.advance(0.5)
    assert rig.settle().unavailable_text != ""


class DeadIpc:
    """What `IpcClient` returns with the service down: Unknown and DOWN, never raising."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def current_state(self) -> StationState:
        return StationState.unknown(self.clock.now(), self.clock.monotonic())

    def health(self) -> EngineHealth:
        from ag_shared.health import EngineStatus

        return EngineHealth(status=EngineStatus.DOWN, last_error="unavailable")


def test_monitoring_unavailable_when_ipc_down() -> None:
    clock = FakeClock(start_utc=T0)
    sessions = FakeSessions(session())
    presenter = ScanPresenter(
        flow=FakeFlow(sessions),
        poller=StatusPoller(DeadIpc(clock), InlineRunner(), clock),
        sessions=sessions,
        runner=InlineRunner(),
        clock=clock,
        translate=TR.t,
        config=CONFIG,
    )
    presenter.tick()
    assert presenter.tick().status_level is StatusLevel.NEUTRAL
    for _ in range(11):
        clock.advance(0.5)
        presenter.tick()
    vm = presenter.tick()
    assert vm.status_level is StatusLevel.UNAVAILABLE
    assert vm.unavailable_text != ""


class RaisingSource:
    def current_state(self) -> StationState:
        raise RuntimeError("boom")

    def health(self) -> EngineHealth:
        raise RuntimeError("boom")


def test_raising_source_is_unknown_then_unavailable() -> None:
    clock = FakeClock(start_utc=T0)
    sessions = FakeSessions(session())
    poller = StatusPoller(RaisingSource(), InlineRunner(), clock)
    presenter = ScanPresenter(
        flow=FakeFlow(sessions),
        poller=poller,
        sessions=sessions,
        runner=InlineRunner(),
        clock=clock,
        translate=TR.t,
        config=CONFIG,
    )
    presenter.tick()
    reading = poller.latest
    assert reading is not None and reading.state is None and reading.health is None
    assert presenter.tick().status_level is StatusLevel.NEUTRAL
    clock.advance(5.0)
    presenter.tick()
    assert presenter.tick().status_level is StatusLevel.UNAVAILABLE


class HangingSource:
    """Hangs on `current_state` until released."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.release = threading.Event()
        self.calls = 0

    def current_state(self) -> StationState:
        self.calls += 1
        self.release.wait(10)
        return StationState.unknown(self.clock.now(), self.clock.monotonic())

    def health(self) -> EngineHealth:
        from ag_shared.health import EngineStatus

        return EngineHealth(status=EngineStatus.UNKNOWN)


@pytest.fixture
def hanging() -> Iterator[tuple[HangingSource, ThreadRunner, FakeClock]]:
    clock = FakeClock(start_utc=T0)
    source = HangingSource(clock)
    runner = ThreadRunner("test-poll")
    yield source, runner, clock
    source.release.set()
    runner.close()


def test_hung_state_source_does_not_block_the_tick(
    hanging: tuple[HangingSource, ThreadRunner, FakeClock],
) -> None:
    source, runner, clock = hanging
    sessions = FakeSessions(session())
    poller = StatusPoller(source, runner, clock, interval_s=0.5)
    presenter = ScanPresenter(
        flow=FakeFlow(sessions),
        poller=poller,
        sessions=sessions,
        runner=InlineRunner(),
        clock=clock,
        translate=TR.t,
        config=CONFIG,
    )
    worst = 0.0
    vm = presenter.tick()
    for _ in range(12):
        clock.advance(0.5)
        started = time.perf_counter()
        vm = presenter.tick()
        worst = max(worst, time.perf_counter() - started)
    assert worst < 0.2
    assert poller.in_flight
    deadline = time.monotonic() + 2
    while source.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert source.calls == 1  # no pile-up of stuck polls
    assert vm.status_level is StatusLevel.UNAVAILABLE
    assert vm.unavailable_text != ""
    # A scan still goes through while the poll is stuck.
    presenter.on_barcode("ITEM-009")
    assert presenter.tick().last_item_text == "Last item: ITEM-009"


def test_poller_survives_a_broken_runner() -> None:
    clock = FakeClock(start_utc=T0)
    poller = StatusPoller(FakeStationEngine(clock), BrokenRunner(), clock)
    assert poller.poll() is None
    assert not poller.in_flight


def test_poller_respects_the_interval() -> None:
    clock = FakeClock(start_utc=T0)
    engine = FakeStationEngine(clock)
    poller = StatusPoller(engine, InlineRunner(), clock, interval_s=0.5)
    poller.poll()
    poller.poll()
    assert engine.current_queries == 1
    clock.advance(0.5)
    poller.poll()
    assert engine.current_queries == 2
    with pytest.raises(ValueError):
        StatusPoller(engine, InlineRunner(), clock, interval_s=0)


class GarbageSource:
    def current_state(self) -> StationState:
        return "attentive"  # type: ignore[return-value]  # a buggy source

    def health(self) -> EngineHealth:
        return None  # type: ignore[return-value]  # a buggy source


def test_poller_drops_garbage_answers() -> None:
    clock = FakeClock(start_utc=T0)
    poller = StatusPoller(GarbageSource(), InlineRunner(), clock)
    poller.poll()
    reading = poller.latest
    assert reading is not None and reading.state is None and reading.health is None


# -- live view, worker, logout ---------------------------------------------------------


def test_live_view_indicator(rig: Rig) -> None:
    assert rig.tick().live_view_text == ""
    rig.presenter.set_live_view_active(True)
    assert rig.tick().live_view_text == "Live view active"
    rig.presenter.set_live_view_active(False)
    assert rig.tick().live_view_text == ""


def test_logout(rig: Rig) -> None:
    assert rig.tick().logged_in
    rig.presenter.logout()
    vm = rig.tick()
    assert rig.sessions.logouts == 1
    assert not vm.logged_in
    assert vm.worker_text == "Nobody is logged in."
    assert vm.message_text == "You are logged out."


def test_logout_failure_is_reported(rig: Rig) -> None:
    rig.sessions.fail_logout = True
    rig.presenter.logout()
    assert rig.tick().message_text == "Logout did not complete. Please try again."


def test_logout_with_broken_runner() -> None:
    rig = make_rig(runner=BrokenRunner())
    rig.presenter.logout()
    assert rig.tick().message_text == "Logout did not complete. Please try again."


def test_welcome_message_after_login() -> None:
    rig = make_rig(logged_in=False)
    s = session("Ravi P")
    rig.sessions.session = s
    rig.presenter.show_welcome(s)
    vm = rig.tick()
    assert vm.message_text == "Welcome, Ravi P."
    assert vm.worker_text == "Logged in: Ravi P"


def test_session_lookup_failure_shows_nobody(rig: Rig) -> None:
    rig.sessions.fail_current = True
    vm = rig.tick()
    assert not vm.logged_in


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        ScanPresenterConfig(warn_banner_s=0)


# -- runners ----------------------------------------------------------------------------


def test_runners_log_job_errors_and_close() -> None:
    def boom() -> None:
        raise RuntimeError("bug")

    InlineRunner().submit(boom)  # logged, not raised
    runner = ThreadRunner("test-close")
    done = threading.Event()
    runner.submit(boom)
    runner.submit(done.set)
    assert done.wait(2)
    runner.close()
    runner.close()
    with pytest.raises(RuntimeError):
        runner.submit(done.set)
