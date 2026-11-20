"""Login presenter (PLAN 2.8; D7; Q3/Q4 defaults): keypad and keyboard input,
validation, success, generic failure, lockout countdown, and the app presenter's
screen switching and keystroke routing."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from ag_shared.api import SessionMethod
from ag_shared.i18n import Translator
from ag_shared.ids import new_uuid7
from ag_shared.states import AttentionState, ScanOutcome, scan_outcome
from ag_workstation.auth import AuthOutcome, CurrentSession, LoginResult
from ag_workstation.clock import FakeClock
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.scan import KeyboardWedgeParser, QueryStatus, ScanMessageKey, ScanResult
from ag_workstation.ui import (
    KEYPAD_DIGITS,
    KEYPAD_LETTERS,
    AppPresenter,
    InlineRunner,
    LoginField,
    LoginPresenter,
    LoginPresenterConfig,
    MessageKind,
    ScanPresenter,
    Screen,
    StatusPoller,
)

T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
TR = Translator("en", strict=True)
W1 = UUID("0193a0b0-0000-7000-8000-00000000c001")
WORKER = W1


# Small fakes, as in test_ui_scan_presenter (test modules are not importable).
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


def _session(name: str = "Asha K") -> CurrentSession:
    return CurrentSession(
        session_id=new_uuid7(),
        user_id=W1,
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
class FakeLogin:
    """Accepts EMP001 / 1234; locks after `lock_after` failures."""

    sessions: FakeSessions | None = None
    lock_after: int = 3
    raises: bool = False
    broken_success: bool = False
    outcome: AuthOutcome | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    failures: int = 0

    def login(self, employee_id: str, pin: str) -> LoginResult:
        self.calls.append((employee_id, pin))
        if self.raises:
            raise RuntimeError(f"bad {pin}")
        if self.broken_success:
            return LoginResult(AuthOutcome.SUCCESS)
        if self.outcome is not None:
            return LoginResult(self.outcome)
        if self.failures >= self.lock_after:
            return LoginResult(AuthOutcome.LOCKED, retry_after=timedelta(minutes=14, seconds=30))
        if employee_id.upper() == "EMP001" and pin == "1234":
            s = _session()
            if self.sessions is not None:
                self.sessions.session = s
            return LoginResult(AuthOutcome.SUCCESS, session=s)
        self.failures += 1
        return LoginResult(AuthOutcome.FAILED, attempts_remaining=self.lock_after - self.failures)


class BrokenRunner:
    def submit(self, job: Callable[[], None]) -> None:
        raise RuntimeError("closed")


class HoldingRunner:
    def __init__(self) -> None:
        self.jobs: list[Callable[[], None]] = []

    def submit(self, job: Callable[[], None]) -> None:
        self.jobs.append(job)

    def run(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


def make(
    backend: FakeLogin | None = None, runner: object | None = None
) -> tuple[LoginPresenter, FakeLogin, FakeClock]:
    clock = FakeClock(start_utc=T0)
    backend = backend or FakeLogin()
    presenter = LoginPresenter(
        backend=backend,
        runner=runner or InlineRunner(),  # type: ignore[arg-type]  # test runners
        clock=clock,
        translate=TR.t,
        config=LoginPresenterConfig(idle_timeout_s=60),
    )
    return presenter, backend, clock


def type_in(p: LoginPresenter, text: str) -> None:
    for ch in text:
        p.press(ch)


# -- input ---------------------------------------------------------------------


def test_initial_view() -> None:
    p, _, _ = make()
    vm = p.tick()
    assert vm.employee_id == "" and vm.pin_mask == ""
    assert vm.active_field is LoginField.EMPLOYEE_ID
    assert vm.keypad_keys == KEYPAD_DIGITS
    assert vm.message_text == "" and vm.message_kind is MessageKind.NONE
    assert not vm.busy and not vm.locked


def test_keypad_digits_letters_toggle_and_masked_pin() -> None:
    p, _, _ = make()
    p.toggle_letters()
    assert p.tick().keypad_keys == KEYPAD_LETTERS
    type_in(p, "EMP")
    p.toggle_letters()
    type_in(p, "001")
    p.select_field(LoginField.PIN)
    type_in(p, "1234")
    vm = p.tick()
    assert vm.employee_id == "EMP001"
    assert vm.pin_mask == "•" * 4
    assert "1234" not in repr(vm)
    assert vm.active_field is LoginField.PIN


def test_keyboard_and_keypad_feed_the_same_model() -> None:
    a, _, _ = make()
    b, _, _ = make()
    a.key("emp001\t1234")
    type_in(b, "EMP001")
    b.select_field(LoginField.PIN)
    type_in(b, "1234")
    assert a.tick() == b.tick()


def test_id_is_upper_cased_and_limited_to_16() -> None:
    p, _, _ = make()
    p.key("abcdefghij0123456789")
    assert p.tick().employee_id == "ABCDEFGHIJ012345"


def test_pin_takes_only_digits_up_to_6() -> None:
    p, _, _ = make()
    p.select_field(LoginField.PIN)
    p.key("12a34-5678")
    assert p.tick().pin_mask == "•" * 6


def test_symbols_and_multichar_presses_are_ignored() -> None:
    p, _, _ = make()
    p.press("-")
    p.press("AB")
    p.press("é")
    assert p.tick().employee_id == ""


def test_backspace_clear_and_escape() -> None:
    p, _, _ = make()
    p.key("EMP01")
    p.backspace()
    assert p.tick().employee_id == "EMP0"
    p.key("\b")
    assert p.tick().employee_id == "EMP"
    p.key("\x7f")
    assert p.tick().employee_id == "EM"
    p.clear()
    assert p.tick().employee_id == ""
    p.key("\t99")
    p.key("\x1b")
    vm = p.tick()
    assert vm.pin_mask == ""
    p.key("12")
    p.backspace()
    assert p.tick().pin_mask == "•"
    p.key("\t")
    assert p.tick().active_field is LoginField.EMPLOYEE_ID


# -- validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "message", "field_"),
    [
        ("", "Enter your employee ID.", LoginField.EMPLOYEE_ID),
        ("EMP001\t12", "PIN: 4 to 6 digits.", LoginField.PIN),
        ("EMP001", "PIN: 4 to 6 digits.", LoginField.PIN),
    ],
)
def test_validation_messages(typed: str, message: str, field_: LoginField) -> None:
    p, backend, _ = make()
    p.key(typed)
    p.submit()
    vm = p.tick()
    assert vm.message_text == message
    assert vm.message_kind is MessageKind.ERROR
    assert vm.active_field is field_
    assert backend.calls == []


def test_id_format_message_for_a_bad_id() -> None:
    p, backend, _ = make()
    p._employee_id = "EMP 01"  # cannot be typed; guards against a future input path
    p.select_field(LoginField.PIN)
    p.key("1234")
    p.submit()
    vm = p.tick()
    assert vm.message_text == "Employee ID: letters and digits only, up to 16."
    assert vm.active_field is LoginField.EMPLOYEE_ID
    assert backend.calls == []


def test_typing_clears_a_validation_message() -> None:
    p, _, _ = make()
    p.submit()
    assert p.tick().message_text != ""
    p.press("E")
    assert p.tick().message_text == ""


# -- login results --------------------------------------------------------------------


def test_success_via_enter_key() -> None:
    p, backend, _ = make()
    p.key("emp001\t1234\r")
    assert backend.calls == [("EMP001", "1234")]
    vm = p.tick()
    assert vm.employee_id == "" and vm.pin_mask == ""
    assert vm.message_text == ""
    s = p.take_success()
    assert s is not None and s.display_name == "Asha K"
    assert p.take_success() is None


def test_busy_while_checking_and_input_ignored() -> None:
    runner = HoldingRunner()
    p, _, _ = make(runner=runner)
    p.key("EMP001\t1234")
    p.submit()
    vm = p.tick()
    assert vm.busy
    assert vm.message_text == "Checking..."
    assert vm.message_kind is MessageKind.INFO
    p.press("9")
    p.backspace()
    p.clear()
    p.submit()
    p.discard_scanned_input()
    assert p.tick().pin_mask == "•" * 4
    assert len(runner.jobs) == 1
    assert not p.idle()
    runner.run()
    assert not p.tick().busy
    assert p.take_success() is not None


def test_generic_failure_clears_pin() -> None:
    p, _, _ = make()
    p.key("EMP001\t9999\r")
    vm = p.tick()
    assert vm.message_text == "Employee ID or PIN is not correct."
    assert vm.message_kind is MessageKind.ERROR
    assert vm.pin_mask == ""
    assert vm.employee_id == "EMP001"
    assert vm.active_field is LoginField.PIN
    assert p.take_success() is None


def test_unknown_id_gets_the_same_message() -> None:
    p, _, _ = make()
    p.key("NOBODY\t1234\r")
    assert p.tick().message_text == "Employee ID or PIN is not correct."


def test_lockout_message_counts_down_and_expires() -> None:
    p, backend, clock = make(FakeLogin(lock_after=0))
    p.key("EMP001\t1234\r")
    vm = p.tick()
    assert vm.locked
    assert vm.message_text == "Too many attempts. Try again in 15 min."
    assert vm.message_kind is MessageKind.ERROR
    clock.advance(10 * 60)
    assert p.tick().message_text == "Too many attempts. Try again in 5 min."
    p.press("1")  # typing into the PIN keeps the lock message (same ID)
    assert p.tick().locked
    clock.advance(5 * 60)
    vm = p.tick()
    assert not vm.locked
    assert vm.message_text == ""
    assert backend.calls == [("EMP001", "1234")]


def test_lock_message_dropped_when_the_id_changes() -> None:
    p, _, _ = make(FakeLogin(lock_after=0))
    p.key("EMP001\t1234\r")
    assert p.tick().locked
    p.select_field(LoginField.EMPLOYEE_ID)
    p.backspace()
    vm = p.tick()
    assert not vm.locked
    assert vm.message_text == ""


def test_lockout_after_repeated_failures() -> None:
    p, _, _ = make(FakeLogin(lock_after=2))
    p.key("EMP001\t0000\r")
    assert p.tick().message_text == "Employee ID or PIN is not correct."
    p.key("0000\r")  # the PIN field stays selected after a failure
    assert p.tick().message_text == "Employee ID or PIN is not correct."
    p.key("0000\r")
    assert p.tick().locked


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (AuthOutcome.UNAVAILABLE, "Login is not available right now. Ask your supervisor."),
        (AuthOutcome.INVALID_INPUT, TR.t("login.invalid_input")),
    ],
)
def test_other_outcomes(outcome: AuthOutcome, message: str) -> None:
    p, _, _ = make(FakeLogin(outcome=outcome))
    p.key("EMP001\t1234\r")
    assert p.tick().message_text == message


def test_backend_exception_is_unavailable_and_not_logged_with_pin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    p, _, _ = make(FakeLogin(raises=True))
    p.key("EMP001\t4321\r")
    assert p.tick().message_text == TR.t("login.unavailable")
    assert "4321" not in caplog.text


def test_success_without_session_is_not_a_login() -> None:
    p, _, _ = make(FakeLogin(broken_success=True))
    p.key("EMP001\t1234\r")
    assert p.tick().message_text == TR.t("login.unavailable")
    assert p.take_success() is None


def test_broken_runner_is_unavailable() -> None:
    p, _, _ = make(runner=BrokenRunner())
    p.key("EMP001\t1234\r")
    vm = p.tick()
    assert vm.message_text == TR.t("login.unavailable")
    assert not vm.busy


def test_reset_with_prompt_and_idle() -> None:
    p, _, clock = make()
    p.key("EMP")
    p.reset(message_key="scan.login_required")
    vm = p.tick()
    assert vm.employee_id == ""
    assert vm.message_text == TR.t("scan.login_required")
    assert vm.message_kind is MessageKind.INFO
    assert not p.idle()
    clock.advance(60)
    assert p.idle()


# -- app presenter: routing and screens -------------------------------------------------


@dataclass
class App:
    app: AppPresenter
    sessions: FakeSessions
    flow: FakeFlow
    backend: FakeLogin
    clock: FakeClock


def make_app(*, logged_in: bool = False) -> App:
    clock = FakeClock(start_utc=T0)
    sessions = FakeSessions(session() if logged_in else None)
    flow = FakeFlow(sessions)
    backend = FakeLogin(sessions=sessions)
    scan = ScanPresenter(
        flow=flow,
        poller=StatusPoller(FakeStationEngine(clock), InlineRunner(), clock),
        sessions=sessions,
        runner=InlineRunner(),
        clock=clock,
        translate=TR.t,
    )
    login = LoginPresenter(backend=backend, runner=InlineRunner(), clock=clock, translate=TR.t)
    app = AppPresenter(scan=scan, login=login, parser=KeyboardWedgeParser(clock))
    return App(app, sessions, flow, backend, clock)


def test_wedge_keystrokes_become_a_scan() -> None:
    a = make_app(logged_in=True)
    for ch in "ITEM-42":
        a.app.key(ch)
    a.app.key("")  # modifier keys carry no text
    a.app.key("\r")
    vm = a.app.tick()
    assert a.flow.seen == ["ITEM-42"]
    assert vm.screen is Screen.SCAN
    assert vm.scan.last_item_text == "Last item: ITEM-42"


def test_unattributed_scan_opens_login_then_login_returns_to_scan() -> None:
    a = make_app()
    a.app.key("BOX7\r")
    vm = a.app.tick()
    assert vm.screen is Screen.LOGIN
    assert vm.login.message_text == TR.t("scan.login_required")
    # A person types slowly: the wedge parser ignores it, the form takes it.
    for ch in "emp001\t1234":
        a.clock.advance(0.3)
        a.app.key(ch)
    a.clock.advance(0.3)
    a.app.key("\r")
    vm = a.app.tick()
    assert a.flow.seen == ["BOX7"]
    assert vm.screen is Screen.SCAN
    assert vm.scan.message_text == "Welcome, Asha K."
    assert vm.scan.logged_in


def test_scan_on_login_screen_is_recorded_and_removed_from_the_form() -> None:
    a = make_app()
    a.app.show_login()
    assert a.app.screen is Screen.LOGIN
    a.clock.advance(1)
    a.app.key("E")
    a.clock.advance(1)
    a.app.key("\t")
    a.app.key("998877\r")  # scanner burst lands in the PIN field
    vm = a.app.tick()
    assert a.flow.seen == ["998877"]
    assert vm.screen is Screen.LOGIN
    assert vm.login.pin_mask == ""
    assert vm.login.employee_id == "E"  # kept: typed by the person
    assert a.backend.calls == []  # the scanner's Enter did not submit


def test_cancel_and_idle_return_to_scan() -> None:
    a = make_app()
    a.app.show_login()
    a.app.key("E")
    a.app.cancel_login()
    assert a.app.tick().screen is Screen.SCAN
    a.app.show_login()
    assert a.app.tick().login.employee_id == ""
    a.clock.advance(61)
    assert a.app.tick().screen is Screen.SCAN


def test_supervisor_assignment_closes_the_login_screen() -> None:
    a = make_app()
    a.app.show_login()
    a.sessions.session = session("Assigned")
    vm = a.app.tick()
    assert vm.screen is Screen.SCAN
    assert vm.scan.worker_text == "Logged in: Assigned"


def test_logout_from_app() -> None:
    a = make_app(logged_in=True)
    a.app.logout()
    vm = a.app.tick()
    assert not vm.scan.logged_in
    assert vm.scan.message_text == "You are logged out."
