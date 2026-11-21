"""'My record' presenter and its navigation (PLAN 2.10): only the logged-in
worker's data, a dispute sent once, idle/logout/scan return to the scan screen."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from ag_shared.api import AttentionEventKind, SessionMethod
from ag_shared.i18n import Translator
from ag_shared.ids import new_uuid7
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.auth import CurrentSession
from ag_workstation.clock import FakeClock
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.record import (
    DisputeOutcome,
    DisputeResult,
    DisputeStatus,
    RecordEvent,
    RecordSnapshot,
    SessionSummary,
)
from ag_workstation.scan import KeyboardWedgeParser, QueryStatus, ScanMessageKey, ScanResult
from ag_workstation.ui import (
    AppPresenter,
    InlineRunner,
    LoginPresenter,
    MessageKind,
    ScanPresenter,
    Screen,
    StatusPoller,
)
from ag_workstation.ui.record_presenter import (
    DisputeReason,
    RecordPresenter,
    RecordPresenterConfig,
    english,
)

T0 = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
TR = Translator("en", strict=True)
ASHA = UUID("0193a0b0-0000-7000-8000-00000000a001")
RAVI = UUID("0193a0b0-0000-7000-8000-00000000a002")


def make_session(user: UUID = ASHA, name: str = "Asha K") -> CurrentSession:
    return CurrentSession(
        session_id=new_uuid7(),
        user_id=user,
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


def summary(
    session_id: UUID, user: UUID, *, start: datetime = T0, end: datetime | None = None
) -> SessionSummary:
    return SessionSummary(session_id, user, start, end, 120, 2, 5, 1)


def rec_event(
    session_id: UUID,
    user: UUID,
    *,
    kind: AttentionEventKind = AttentionEventKind.SCAN_WARNING,
    seconds: float | None = None,
    dispute: DisputeStatus | None = None,
    low: bool = False,
    images: int = 0,
) -> RecordEvent:
    return RecordEvent(
        id=new_uuid7(),
        session_id=session_id,
        user_id=user,
        kind=kind,
        state=AttentionState.DROWSY,
        started_at=T0 + timedelta(minutes=10),
        ended_at=T0 + timedelta(minutes=10, seconds=seconds) if seconds is not None else None,
        low_confidence=low,
        images_saved=images,
        dispute=dispute,
    )


@dataclass
class Sessions:
    session: CurrentSession | None = None
    fail: bool = False

    def current(self) -> CurrentSession | None:
        if self.fail:
            raise RuntimeError("broken")
        return self.session

    def logout(self) -> bool:
        self.session = None
        return True


@dataclass
class Source:
    """Holds rows of several workers; answers like `PersonalRecordStore.snapshot`."""

    sessions: list[SessionSummary] = field(default_factory=list)
    events: list[RecordEvent] = field(default_factory=list)
    asked: list[UUID] = field(default_factory=list)
    fail: bool = False
    leak: bool = False
    """Misbehave: return every worker's rows (the presenter must still filter)."""

    def snapshot(self, user_id: UUID) -> RecordSnapshot:
        self.asked.append(user_id)
        if self.fail:
            raise OSError("locked")
        if self.leak:
            return RecordSnapshot(user_id, tuple(self.sessions), tuple(self.events))
        return RecordSnapshot(
            user_id,
            tuple(s for s in self.sessions if s.user_id == user_id),
            tuple(e for e in self.events if e.user_id == user_id),
        )


@dataclass
class Disputes:
    calls: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    outcome: DisputeOutcome = DisputeOutcome.QUEUED
    on_submit: Callable[[UUID], None] | None = None

    def submit(self, user_id: UUID, attention_event_id: UUID, message: str) -> DisputeResult:
        self.calls.append((user_id, attention_event_id, message))
        if self.on_submit is not None:
            self.on_submit(attention_event_id)
        return DisputeResult(self.outcome, new_uuid7())


class ManualRunner:
    def __init__(self) -> None:
        self.jobs: list[Callable[[], None]] = []
        self.closed = False

    def submit(self, job: Callable[[], None]) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.jobs.append(job)

    def run(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_utc=T0)


@dataclass
class Rig:
    presenter: RecordPresenter
    source: Source
    disputes: Disputes
    sessions: Sessions
    runner: ManualRunner
    me: CurrentSession
    clock: FakeClock


@pytest.fixture
def rig(clock: FakeClock) -> Rig:
    me = make_session()
    other_shift = new_uuid7()
    source = Source(
        sessions=[
            summary(me.session_id, ASHA),
            summary(other_shift, RAVI, start=T0 - timedelta(hours=8)),
        ],
        events=[rec_event(me.session_id, ASHA), rec_event(other_shift, RAVI)],
    )
    sessions = Sessions(me)
    runner = ManualRunner()
    disputes = Disputes()
    presenter = RecordPresenter(
        source=source,
        disputes=disputes,
        sessions=sessions,
        runner=runner,
        clock=clock,
        translate=TR.t,
        timezone=lambda: ZoneInfo("Asia/Kolkata"),
    )
    return Rig(presenter, source, disputes, sessions, runner, me, clock)


def opened(rig: Rig) -> None:
    rig.presenter.open(rig.me)
    rig.runner.run()


def test_loads_for_the_session_user_and_shows_only_their_rows(rig: Rig) -> None:
    rig.presenter.open(rig.me)
    vm = rig.presenter.tick()
    assert vm.empty_text == "Loading..."
    rig.runner.run()
    vm = rig.presenter.tick()
    assert rig.source.asked == [ASHA]
    assert [r.session_id for r in vm.sessions] == [rig.me.session_id]
    assert vm.sessions[0].selected
    # 02:30 UTC is 08:00 in the site timezone (Asia/Kolkata).
    assert vm.sessions[0].title_text == "This shift (since 08:00)"
    assert vm.sessions[0].counts_text == ("Scans: 120   Warnings: 2   Not monitored: 5   Events: 1")
    assert len(vm.events) == 1 and vm.events[0].text == "08:10:00  Warning at scan: Drowsy"
    assert vm.worker_text == "Logged in: Asha K"
    assert "7 days" in vm.scope_text and vm.empty_text == ""


def test_another_workers_rows_never_reach_the_view_even_if_the_source_leaks(rig: Rig) -> None:
    rig.source.leak = True
    opened(rig)
    vm = rig.presenter.tick()
    assert [r.session_id for r in vm.sessions] == [rig.me.session_id]
    assert len(vm.events) == 1
    ravi_events = [e.id for e in rig.source.events if e.user_id == RAVI]
    assert all(row.event_id not in ravi_events for row in vm.events)


def test_snapshot_for_another_user_is_dropped(rig: Rig, clock: FakeClock) -> None:
    rig.presenter.open(rig.me)
    rig.runner.jobs.clear()
    rig.presenter._inbox.append((RAVI, rig.source.snapshot(RAVI)))
    vm = rig.presenter.tick()
    assert vm.sessions == () and vm.events == ()
    # Mislabelled snapshot (right key, wrong owner) is emptied too.
    rig.presenter._inbox.append((ASHA, RecordSnapshot(RAVI, tuple(rig.source.sessions), ())))
    assert rig.presenter.tick().sessions == ()


def test_a_load_finishing_after_the_worker_changed_is_dropped(rig: Rig) -> None:
    rig.presenter.open(rig.me)
    ravi = make_session(RAVI, "Ravi")
    rig.sessions.session = ravi
    rig.presenter.tick()  # session changed: everything forgotten
    rig.runner.run()  # Asha's load completes late
    vm = rig.presenter.tick()
    assert vm.sessions == () and vm.events == () and vm.worker_text == ""


def test_logout_clears_the_screen(rig: Rig) -> None:
    opened(rig)
    assert rig.presenter.tick().sessions
    rig.sessions.session = None
    vm = rig.presenter.tick()
    assert vm.sessions == () and rig.presenter.session_id is None


def test_session_lookup_failure_clears_the_screen(rig: Rig) -> None:
    opened(rig)
    rig.sessions.fail = True
    assert rig.presenter.tick().sessions == ()


def test_past_shifts_and_selection(rig: Rig) -> None:
    past = new_uuid7()
    open_past = new_uuid7()
    rig.source.sessions += [
        summary(past, ASHA, start=T0 - timedelta(days=1), end=T0 - timedelta(hours=16)),
        summary(open_past, ASHA, start=T0 - timedelta(days=2)),
    ]
    rig.source.events.append(
        rec_event(past, ASHA, kind=AttentionEventKind.STATE_EPISODE, seconds=75, images=2)
    )
    opened(rig)
    vm = rig.presenter.tick()
    titles = [r.title_text for r in vm.sessions]
    assert titles[1] == "Shift on 2026-11-20, 08:00 to 16:00"
    assert titles[2] == "Shift on 2026-11-19, from 08:00"
    rig.presenter.select_session(past)
    vm = rig.presenter.tick()
    assert vm.sessions[1].selected and not vm.sessions[0].selected
    assert vm.events[0].text == "08:10:00  Drowsy for 1 min 15 s (photos saved: 2)"
    rig.presenter.select_session(open_past)
    assert rig.presenter.tick().empty_text == "No events in this shift."
    rig.presenter.select_session(new_uuid7())  # unknown: ignored
    assert rig.presenter.tick().sessions[2].selected


def test_event_texts(rig: Rig) -> None:
    sid = rig.me.session_id
    rig.source.events = [
        rec_event(sid, ASHA, kind=AttentionEventKind.STATE_EPISODE, seconds=12, low=True),
        rec_event(sid, ASHA, kind=AttentionEventKind.STATE_EPISODE),
        rec_event(sid, ASHA, dispute=DisputeStatus.PENDING),
    ]
    opened(rig)
    texts = [e.text for e in rig.presenter.tick().events]
    assert texts == [
        "08:10:00  Drowsy for 12 s (low confidence)",
        "08:10:00  Drowsy",
        "08:10:00  Warning at scan: Drowsy (disputed)",
    ]
    assert [e.can_dispute for e in rig.presenter.tick().events] == [True, True, False]


def test_nothing_recorded(rig: Rig) -> None:
    rig.source.sessions = []
    rig.source.events = []
    opened(rig)
    assert rig.presenter.tick().empty_text == (
        "No shifts recorded at this station in the last 7 days."
    )


def test_load_failure_and_refresh(rig: Rig) -> None:
    rig.source.fail = True
    opened(rig)
    vm = rig.presenter.tick()
    assert vm.message_text == TR.t("ui.record.load_failed")
    assert vm.message_kind is MessageKind.ERROR
    rig.source.fail = False
    rig.presenter.refresh()
    rig.runner.run()
    assert rig.presenter.tick().sessions


def test_runner_gone_shows_an_error(rig: Rig) -> None:
    rig.runner.closed = True
    rig.presenter.open(rig.me)
    assert rig.presenter.tick().message_text == TR.t("ui.record.load_failed")


def dispute_flow(rig: Rig) -> UUID:
    opened(rig)
    event_id = rig.presenter.tick().events[0].event_id
    rig.presenter.select_event(event_id)
    assert rig.presenter.tick().can_dispute
    rig.presenter.start_dispute()
    vm = rig.presenter.tick()
    assert vm.dispute_open and not vm.can_submit
    assert [r.text for r in vm.reasons] == [TR.t(r.key) for r in DisputeReason]
    rig.presenter.choose_reason(DisputeReason.CAMERA_VIEW)
    vm = rig.presenter.tick()
    assert vm.can_submit and [r.selected for r in vm.reasons] == [False, True, False, False]
    return event_id


def test_dispute_is_submitted_once_with_the_english_reason(rig: Rig) -> None:
    event_id = dispute_flow(rig)
    rig.presenter.submit_dispute()
    rig.presenter.submit_dispute()  # double tap while busy: ignored
    vm = rig.presenter.tick()
    assert vm.busy and vm.message_text == "Sending..." and not vm.can_submit
    rig.runner.run()
    assert rig.disputes.calls == [(ASHA, event_id, "The camera could not see me properly.")]
    vm = rig.presenter.tick()
    assert not vm.busy and not vm.dispute_open
    assert vm.message_text == TR.t("ui.record.dispute.sent")
    rig.runner.run()
    assert rig.source.asked == [ASHA, ASHA]  # reloaded to show "disputed"
    rig.presenter.submit_dispute()  # panel closed: nothing
    rig.runner.run()
    assert len(rig.disputes.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "key", "kind", "closes"),
    [
        (DisputeOutcome.SAVED, "ui.record.dispute.saved", MessageKind.INFO, True),
        (DisputeOutcome.ALREADY_DISPUTED, "ui.record.dispute.already", MessageKind.INFO, True),
        (DisputeOutcome.NOT_FOUND, "ui.record.dispute.not_found", MessageKind.ERROR, False),
        (DisputeOutcome.FAILED, "ui.record.dispute.failed", MessageKind.ERROR, False),
    ],
)
def test_dispute_outcomes(
    rig: Rig, outcome: DisputeOutcome, key: str, kind: MessageKind, closes: bool
) -> None:
    rig.disputes.outcome = outcome
    dispute_flow(rig)
    rig.presenter.submit_dispute()
    rig.runner.run()
    vm = rig.presenter.tick()
    assert (vm.message_text, vm.message_kind) == (TR.t(key), kind)
    assert vm.dispute_open is not closes


def test_dispute_runner_gone(rig: Rig) -> None:
    dispute_flow(rig)
    rig.runner.closed = True
    rig.presenter.submit_dispute()
    vm = rig.presenter.tick()
    assert vm.message_text == TR.t("ui.record.dispute.failed") and not vm.busy


def test_dispute_result_after_logout_is_dropped(rig: Rig) -> None:
    dispute_flow(rig)
    rig.presenter.submit_dispute()
    rig.sessions.session = None
    rig.presenter.tick()
    rig.runner.run()
    vm = rig.presenter.tick()
    assert vm.message_text == "" and not vm.busy


def test_cancel_and_guards(rig: Rig) -> None:
    opened(rig)
    rig.presenter.start_dispute()  # nothing selected
    assert not rig.presenter.tick().dispute_open
    rig.presenter.choose_reason(DisputeReason.ON_BREAK)  # panel closed: ignored
    rig.presenter.select_event(new_uuid7())  # not shown: ignored
    assert not rig.presenter.tick().can_dispute
    dispute_flow(rig)
    rig.presenter.cancel_dispute()
    vm = rig.presenter.tick()
    assert not vm.dispute_open and vm.reasons == ()


def test_disputed_event_cannot_be_disputed_again(rig: Rig) -> None:
    rig.source.events = [rec_event(rig.me.session_id, ASHA, dispute=DisputeStatus.QUEUED)]
    opened(rig)
    rig.presenter.select_event(rig.presenter.tick().events[0].event_id)
    rig.presenter.start_dispute()
    vm = rig.presenter.tick()
    assert not vm.can_dispute and not vm.dispute_open


def test_idle(rig: Rig, clock: FakeClock) -> None:
    opened(rig)
    clock.advance(59)
    assert not rig.presenter.idle()
    rig.presenter.refresh()  # a touch
    clock.advance(59)
    assert not rig.presenter.idle()
    clock.advance(2)
    assert rig.presenter.idle()


def test_busy_is_never_idle(rig: Rig, clock: FakeClock) -> None:
    dispute_flow(rig)
    rig.presenter.submit_dispute()
    clock.advance(600)
    assert not rig.presenter.idle()


def test_timezone_failure_shows_utc(rig: Rig) -> None:
    def broken() -> ZoneInfo:
        raise KeyError("tz")

    rig.presenter._timezone = broken
    opened(rig)
    assert rig.presenter.tick().sessions[0].title_text == "This shift (since 02:30)"


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        RecordPresenterConfig(idle_s=0)


def test_english_reason_text() -> None:
    assert english(DisputeReason.WAS_ATTENTIVE.key) == "I was paying attention."


# -- navigation (AppPresenter) ----------------------------------------------------------------


@dataclass
class Flow:
    scans: list[str] = field(default_factory=list)

    def on_barcode(self, barcode: str) -> ScanResult:
        self.scans.append(barcode)
        return ScanResult(
            scan_id=new_uuid7(),
            barcode=barcode,
            scanned_at=T0,
            outcome=ScanOutcome.ATTENTIVE,
            state=AttentionState.ATTENTIVE,
            confidence=0.9,
            query_status=QueryStatus.OK,
            message_key=ScanMessageKey.SCAN_OK,
        )


@dataclass
class NavRig:
    app: AppPresenter
    flow: Flow
    rig: Rig


@pytest.fixture
def nav(rig: Rig, clock: FakeClock) -> NavRig:
    flow = Flow()
    runner = InlineRunner()
    scan = ScanPresenter(
        flow=flow,
        poller=StatusPoller(FakeStationEngine(clock), runner, clock),
        sessions=rig.sessions,
        runner=runner,
        clock=clock,
        translate=TR.t,
    )

    class NoLogin:
        def login(self, employee_id: str, pin: str) -> object:
            raise AssertionError("not used")

    login = LoginPresenter(
        backend=NoLogin(),  # type: ignore[arg-type]  # never called in these tests
        runner=runner,
        clock=clock,
        translate=TR.t,
    )
    app = AppPresenter(
        scan=scan, login=login, parser=KeyboardWedgeParser(clock), record=rig.presenter
    )
    app.tick()
    return NavRig(app, flow, rig)


def to_record(nav: NavRig) -> None:
    nav.app.show_record()
    nav.rig.runner.run()
    vm = nav.app.tick()
    assert vm.screen is Screen.RECORD and vm.record is not None and vm.record.sessions


def test_record_opens_for_the_logged_in_worker_and_back(nav: NavRig) -> None:
    vm = nav.app.tick()
    assert vm.record_available
    to_record(nav)
    nav.app.close_record()
    vm = nav.app.tick()
    assert vm.screen is Screen.SCAN and vm.record is not None and vm.record.sessions == ()


def test_record_not_offered_without_a_worker(nav: NavRig) -> None:
    nav.rig.sessions.session = None
    vm = nav.app.tick()
    assert not vm.record_available
    nav.app.show_record()
    assert nav.app.tick().screen is Screen.SCAN


def test_scan_on_the_record_screen_is_recorded_and_returns_to_scan(nav: NavRig) -> None:
    to_record(nav)
    nav.app.key("BOX-77\r")
    assert nav.flow.scans == ["BOX-77"]
    vm = nav.app.tick()
    assert vm.screen is Screen.SCAN and vm.scan.last_item_text == "Last item: BOX-77"


def test_non_barcode_keys_stay_on_the_record_screen(nav: NavRig) -> None:
    to_record(nav)
    nav.app.key("")
    nav.app.key("a")
    assert nav.app.tick().screen is Screen.RECORD


def test_logout_and_session_change_return_to_scan(nav: NavRig) -> None:
    to_record(nav)
    nav.rig.sessions.session = None
    vm = nav.app.tick()
    assert vm.screen is Screen.SCAN and vm.record is not None and vm.record.sessions == ()
    nav.rig.sessions.session = nav.rig.me
    nav.app.tick()
    to_record(nav)
    nav.rig.sessions.session = make_session(RAVI, "Ravi")
    assert nav.app.tick().screen is Screen.SCAN


def test_idle_record_screen_returns_to_scan(nav: NavRig, clock: FakeClock) -> None:
    to_record(nav)
    clock.advance(61)
    assert nav.app.tick().screen is Screen.SCAN


def test_app_without_a_record_presenter(rig: Rig, clock: FakeClock) -> None:
    runner = InlineRunner()
    scan = ScanPresenter(
        flow=Flow(),
        poller=StatusPoller(FakeStationEngine(clock), runner, clock),
        sessions=rig.sessions,
        runner=runner,
        clock=clock,
        translate=TR.t,
    )
    login = LoginPresenter(backend=None, runner=runner, clock=clock, translate=TR.t)  # type: ignore[arg-type]  # never called
    app = AppPresenter(scan=scan, login=login, parser=KeyboardWedgeParser(clock))
    app.tick()
    app.show_record()
    vm = app.tick()
    assert vm.screen is Screen.SCAN and vm.record is None and not vm.record_available
    app.close_record()
