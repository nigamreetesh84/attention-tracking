"""Consent and capture gate (PLAN 2.5): each condition, mid-burst changes, fail-safe, structure."""

import ast
import copy
import pickle
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from ag_shared.api import (
    AssignWorkerCommand,
    ConfigBundle,
    ImagesWithheld,
    RetentionLocal,
    SessionMethod,
)
from ag_shared.config import Shift, ShiftBreak
from ag_shared.ids import new_uuid7
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_workstation.auth import (
    AuthStateStore,
    CurrentSession,
    LoginMethodRegistry,
    SessionManager,
    StaticRosterSource,
)
from ag_workstation.clock import FakeClock
from ag_workstation.consent import (
    CaptureGate,
    CaptureOutcome,
    CapturePermit,
    GateDecision,
    GatedEvidenceSink,
    GateReason,
    PermitError,
    break_intervals,
    images_withheld_for,
    on_break,
)
from ag_workstation.consent import permit as permit_module
from ag_workstation.engine_api import BurstKind, EvidenceBurst, EvidenceSink
from ag_workstation.evidence import EvidenceStore, RemovalReason, SaveResult
from ag_workstation.fakes import BurstBuilder
from ag_workstation.fakes.engine import FakeStationEngine
from ag_workstation.queue import InMemoryKeyProvider
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

Json = dict[str, Any]  # Any: free-form JSON test fixtures

KEY = bytes(range(32))
# 08:00 UTC is 13:30 in Asia/Kolkata (UTC+05:30).
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
TZ = "Asia/Kolkata"
STATION = UUID("0193a0b0-0000-7000-8000-00000000d001")
W1 = UUID("0193a0b0-0000-7000-8000-00000000c001")
W2 = UUID("0193a0b0-0000-7000-8000-00000000c002")
DAY_SHIFT = UUID("0193a0b0-0000-7000-8000-00000000a001")
NIGHT_SHIFT = UUID("0193a0b0-0000-7000-8000-00000000a002")
# Lunch 12:00-12:30 IST = 06:30-07:00 UTC (the next day, so fixture clocks only move forward).
# Night break 02:00-02:30 IST on the 21st = 20:30-21:00 UTC on the 20th.
LUNCH_START = datetime(2026, 11, 21, 6, 30, tzinfo=UTC)
NIGHT_BREAK_START = datetime(2026, 11, 20, 20, 30, tzinfo=UTC)
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


def _worker(user_id: UUID, employee_id: str, *, consent: bool) -> Json:
    return {
        "user_id": str(user_id),
        "employee_id": employee_id,
        "display_name": f"Worker {employee_id}",
        "pin_hash": None,
        "has_consent": consent,
        "ppe_profile_override": None,
        "preferred_language": None,
        "is_active": True,
    }


def bundle(
    *, timezone: str = TZ, w1_consent: bool = True, shifts: bool = True, version: int = 1
) -> ConfigBundle:
    data: Json = {
        "version": version,
        "generated_at": "2026-11-20T00:00:00Z",
        "site": {"timezone": timezone, "settings": {}},
        "workstation": {
            "id": str(STATION),
            "name": "Line 1 / WS 3",
            "line_id": str(STATION),
            "camera_count": 1,
        },
        "cameras": [{"slot": 1, "role": "primary", "kind": "usb", "uri": "0"}],
        "ppe_profile": builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json"),
        "thresholds": {},
        "roster": [_worker(W1, "EMP001", consent=w1_consent), _worker(W2, "EMP002", consent=False)],
        "shifts": [
            {
                "id": str(DAY_SHIFT),
                "name": "Day",
                "start_time": "06:00:00",
                "end_time": "14:00:00",
                "days_of_week": ALL_DAYS,
                "breaks": [{"name": "Lunch", "start_time": "12:00:00", "end_time": "12:30:00"}],
            },
            {
                "id": str(NIGHT_SHIFT),
                "name": "Night",
                "start_time": "22:00:00",
                "end_time": "06:00:00",
                "days_of_week": ALL_DAYS,
                "breaks": [{"name": "Night", "start_time": "02:00:00", "end_time": "02:30:00"}],
            },
        ]
        if shifts
        else [],
    }
    return ConfigBundle.model_validate(data)


def session(
    *, consent: bool = True, started_at: datetime = T0 - timedelta(hours=1)
) -> CurrentSession:
    return CurrentSession(
        session_id=new_uuid7(),
        user_id=W1,
        employee_id="EMP001",
        display_name="Worker EMP001",
        has_consent=consent,
        preferred_language=None,
        ppe_profile_id=None,
        method=SessionMethod.EMPLOYEE_ID_PIN,
        station_assignment_id=None,
        started_at=started_at,
        valid_until=None,
    )


class Sessions:
    """A `SessionSource` the test drives; can log out after N calls or fail."""

    def __init__(self, current: CurrentSession | None = None) -> None:
        self.session = current
        self.calls = 0
        self.logout_after: int | None = None
        self.fail: BaseException | None = None

    def current(self) -> CurrentSession | None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if self.logout_after is not None and self.calls > self.logout_after:
            return None
        return self.session


BUILDER = BurstBuilder(secondary_slots=(2,))


def burst(at: datetime = T0 - timedelta(seconds=10)) -> EvidenceBurst:
    return BUILDER.build(new_uuid7(), BurstKind.SCAN_WARNING, at)


def open_store(root: Path, clock: FakeClock, *, mb: int = 50) -> EvidenceStore:
    return EvidenceStore.open(
        root,
        InMemoryKeyProvider(KEY),
        clock,
        RetentionLocal(max_disk_mb=mb, max_age_days=30),
        disk_free=lambda _: 10**12,
        min_free_mb=1,
    )


def files(root: Path) -> list[Path]:
    images = root / "images"
    return sorted(images.iterdir()) if images.exists() else []


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_utc=T0)


@pytest.fixture
def sessions() -> Sessions:
    return Sessions(session())


@pytest.fixture
def roster() -> StaticRosterSource:
    return StaticRosterSource(bundle())


@pytest.fixture
def gate(sessions: Sessions, roster: StaticRosterSource, clock: FakeClock) -> CaptureGate:
    return CaptureGate(sessions=sessions, bundles=roster, clock=clock)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "evidence"


@pytest.fixture
def store(root: Path, clock: FakeClock) -> Iterator[EvidenceStore]:
    s = open_store(root, clock)
    yield s
    s.close()


@pytest.fixture
def outcomes() -> list[CaptureOutcome]:
    return []


@pytest.fixture
def sink(
    gate: CaptureGate, store: EvidenceStore, outcomes: list[CaptureOutcome]
) -> GatedEvidenceSink:
    return GatedEvidenceSink(gate, store, on_outcome=outcomes.append)


# -- each gate condition -------------------------------------------------------


def test_open_with_session_consent_and_no_break(gate: CaptureGate, sessions: Sessions) -> None:
    decision = gate.decide(burst())
    assert decision.open
    assert decision.reason is GateReason.OPEN
    assert decision.images_withheld is ImagesWithheld.NONE
    assert decision.decided_at == T0
    assert sessions.session is not None
    assert decision.session_id == sessions.session.session_id


def test_no_session_is_closed(gate: CaptureGate, sessions: Sessions) -> None:
    sessions.session = None
    decision = gate.decide(burst())
    assert decision.reason is GateReason.NO_SESSION
    assert decision.images_withheld is ImagesWithheld.NO_WORKER
    assert decision.session_id is None


def test_no_consent_is_closed(gate: CaptureGate, sessions: Sessions) -> None:
    sessions.session = session(consent=False)
    decision = gate.decide(burst())
    assert decision.reason is GateReason.NO_CONSENT
    assert decision.images_withheld is ImagesWithheld.NO_CONSENT


def test_session_that_started_after_the_first_frame_is_closed(
    gate: CaptureGate, sessions: Sessions
) -> None:
    b = burst(at=T0 - timedelta(seconds=10))
    first = min(f.captured_at for f in b.frames)
    sessions.session = session(started_at=first + timedelta(milliseconds=1))
    assert gate.decide(b).reason is GateReason.SESSION_CHANGED
    assert gate.decide(b).images_withheld is ImagesWithheld.NO_WORKER
    sessions.session = session(started_at=first)
    assert gate.decide(b).open
    # A frame taken now (live) is covered by a session that started before now.
    sessions.session = session(started_at=T0)
    assert gate.decide_live().open


def test_engine_on_break_is_closed(sessions: Sessions, roster: StaticRosterSource) -> None:
    flag = {"on": True}
    gate = CaptureGate(
        sessions=sessions, bundles=roster, clock=FakeClock(start_utc=T0),
        engine_on_break=lambda: flag["on"],
    )  # fmt: skip
    assert gate.decide(burst()).reason is GateReason.ON_BREAK
    assert gate.decide(burst()).images_withheld is ImagesWithheld.NO_WORKER
    flag["on"] = False
    assert gate.decide(burst()).open


def test_scheduled_break_in_site_timezone(
    gate: CaptureGate, clock: FakeClock, sessions: Sessions
) -> None:
    # Clock moves to just before lunch (12:00 IST = 06:30 UTC).
    clock_at(clock, LUNCH_START - timedelta(seconds=1))
    sessions.session = session(started_at=LUNCH_START - timedelta(hours=2))
    assert gate.decide_live().open
    clock.advance(1)  # exactly 12:00:00 IST: on break
    assert gate.decide_live().reason is GateReason.ON_BREAK
    clock.advance(30 * 60 - 1)  # 12:29:59 IST
    assert gate.decide_live().reason is GateReason.ON_BREAK
    clock.advance(1)  # 12:30:00 IST: break over (end exclusive)
    assert gate.decide_live().open


def test_utc_times_matching_local_break_clock_are_not_a_break(
    gate: CaptureGate, clock: FakeClock, sessions: Sessions
) -> None:
    # 12:10 UTC is 17:40 IST: the shift's wall times are local, not UTC.
    clock_at(clock, datetime(2026, 11, 20, 12, 10, tzinfo=UTC))
    sessions.session = session(started_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC))
    assert gate.decide_live().open


def test_burst_whose_frames_reach_into_a_break_is_closed(
    gate: CaptureGate, clock: FakeClock, sessions: Sessions
) -> None:
    sessions.session = session(started_at=LUNCH_START - timedelta(hours=2))
    clock_at(clock, LUNCH_START + timedelta(seconds=2))
    b = burst(at=LUNCH_START - timedelta(seconds=5))  # starts before, ends in the break
    assert gate.decide(b).reason is GateReason.ON_BREAK


def test_overnight_shift_break_after_midnight(
    gate: CaptureGate, clock: FakeClock, sessions: Sessions
) -> None:
    # Night shift starts 22:00 IST on the 20th; its break is 02:00 IST on the 21st.
    sessions.session = session(started_at=NIGHT_BREAK_START - timedelta(hours=3))
    clock_at(clock, NIGHT_BREAK_START + timedelta(minutes=10))
    assert gate.decide_live().reason is GateReason.ON_BREAK
    clock_at(clock, NIGHT_BREAK_START + timedelta(minutes=31))
    assert gate.decide_live().open


def test_break_only_on_listed_days() -> None:
    weekday_only = Shift(
        id=DAY_SHIFT,
        name="Weekdays",
        start_time=time(6),
        end_time=time(14),
        days_of_week=(0, 1, 2, 3, 4),
        breaks=(ShiftBreak(name="Lunch", start_time=time(12), end_time=time(12, 30)),),
    )
    friday = datetime(2026, 11, 20, 6, 40, tzinfo=UTC)  # 12:10 IST, Friday
    saturday = friday + timedelta(days=1)
    assert on_break([weekday_only], TZ, friday, friday)
    assert not on_break([weekday_only], TZ, saturday, saturday)


def test_break_across_a_dst_change() -> None:
    # America/New_York leaves DST on 2026-11-01; lunch 12:00 local is 17:00 UTC after it.
    lunch = Shift(
        id=DAY_SHIFT,
        name="Day",
        start_time=time(8),
        end_time=time(16),
        days_of_week=tuple(ALL_DAYS),
        breaks=(ShiftBreak(name="Lunch", start_time=time(12), end_time=time(12, 30)),),
    )
    before = datetime(2026, 10, 30, 16, 10, tzinfo=UTC)  # 12:10 EDT
    after = datetime(2026, 11, 2, 17, 10, tzinfo=UTC)  # 12:10 EST
    assert on_break([lunch], "America/New_York", before, before)
    assert on_break([lunch], "America/New_York", after, after)
    assert not on_break(
        [lunch], "America/New_York", after - timedelta(hours=1), after - timedelta(hours=1)
    )
    spans = list(break_intervals([lunch], __import__("zoneinfo").ZoneInfo("UTC"), after, after))
    assert all(end - start == timedelta(minutes=30) for start, end in spans)


def test_on_break_rejects_naive_times() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        on_break([], TZ, datetime(2026, 1, 1), datetime(2026, 1, 1))


def test_no_bundle_means_no_scheduled_break(sessions: Sessions, clock: FakeClock) -> None:
    gate = CaptureGate(sessions=sessions, bundles=StaticRosterSource(None), clock=clock)
    clock_at(clock, LUNCH_START + timedelta(minutes=5))
    sessions.session = session(started_at=LUNCH_START - timedelta(hours=1))
    assert gate.decide_live().open


def clock_at(clock: FakeClock, at: datetime) -> None:
    clock.advance((at - clock.now()).total_seconds())


# -- roster in force (real SessionManager) -------------------------------------


class _NullRecords:
    def enqueue(
        self,
        item_type: str,
        payload: BaseModel,
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        return None


def test_consent_follows_the_roster_in_force(clock: FakeClock) -> None:
    roster = StaticRosterSource(bundle(shifts=False))
    with AuthStateStore.in_memory() as state:
        manager = SessionManager(
            registry=LoginMethodRegistry(),
            roster=roster,
            records=_NullRecords(),
            engine=FakeStationEngine(clock),
            state=state,
            clock=clock,
        )
        gate = CaptureGate(sessions=manager, bundles=roster, clock=clock)
        assert gate.decide_live().reason is GateReason.NO_SESSION
        manager.handle_assign_worker(
            AssignWorkerCommand(
                id=new_uuid7(),
                assignment_id=new_uuid7(),
                user_id=W1,
                valid_until=T0 + timedelta(hours=8),
            )
        )
        clock.advance(60)
        assert gate.decide(burst(at=clock.now() - timedelta(seconds=10))).open
        # Consent withdrawn in a newer bundle: closed from the next decision.
        roster.set_bundle(bundle(w1_consent=False, shifts=False, version=2))
        assert gate.decide_live().reason is GateReason.NO_CONSENT
        # Worker no longer listed at all: consent withdrawn too.
        roster.set_bundle(bundle(shifts=False, version=3).model_copy(update={"roster": ()}))
        assert gate.decide_live().reason is GateReason.NO_CONSENT
        roster.set_bundle(bundle(shifts=False, version=4))
        assert gate.decide_live().open
        manager.logout()
        assert gate.decide_live().reason is GateReason.NO_SESSION


# -- time base: device time on both sides (contract 6 "Clock"; 2.11 fix) ----------


class _Offset:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def offset_ms(self) -> int:
        return self.ms


@pytest.mark.parametrize(
    ("login_offset_ms", "burst_offset_ms", "burst_after_login_s", "opens"),
    [
        # The server offset grows by 30 s after login. A burst that began 10 s
        # before the session would look 20 s *after* it in corrected UTC; it
        # must stay closed (frames of the previous worker or of nobody).
        (0, 30_000, -10, False),
        # The offset shrinks by 30 s: a burst 10 s into the session would look
        # 20 s before it in corrected UTC; it is the worker's and may open.
        (30_000, 0, 10, True),
        (5_000, 5_000, -1, False),
        (5_000, 5_000, 1, True),
    ],
)
def test_the_gate_compares_session_and_burst_in_device_time(
    clock: FakeClock,
    login_offset_ms: int,
    burst_offset_ms: int,
    burst_after_login_s: int,
    opens: bool,
) -> None:
    """The UI stamps records with corrected UTC, but what it pushes to the
    engine child (the session start) and what the child compares it with (the
    burst's frames) are both device time, so no offset change can open the
    gate for a burst that started before the session."""
    from ag_workstation.corrected_time import StampingClock
    from ag_workstation.service.station import PushedSession, PushedSessions

    roster = StaticRosterSource(bundle(shifts=False))
    offset = _Offset(login_offset_ms)
    with AuthStateStore.in_memory() as state:
        manager = SessionManager(
            registry=LoginMethodRegistry(),
            roster=roster,
            records=_NullRecords(),
            engine=FakeStationEngine(clock),
            state=state,
            clock=clock,
            utc=StampingClock(clock, offset),
        )
        clock.advance(60)
        manager.handle_assign_worker(
            AssignWorkerCommand(
                id=new_uuid7(),
                assignment_id=new_uuid7(),
                user_id=W1,
                valid_until=T0 + timedelta(hours=8),
            )
        )
        login_at = clock.now()
        clock.advance(30)
        offset.ms = burst_offset_ms
        current = manager.current()
        assert current is not None
        # What `SessionPusher` sends, as the engine child stores it.
        pushed = PushedSessions(clock)
        pushed.set(
            PushedSession(current.session_id, current.user_id, current.started_at, True), 5.0
        )
        child_gate = CaptureGate(sessions=pushed, bundles=None, clock=clock)
        # A burst whose earliest frame is `burst_after_login_s` from the login.
        probe = BUILDER.build(new_uuid7(), BurstKind.SCAN_WARNING, login_at)
        lead = login_at - min(f.captured_at for f in probe.frames)
        first_frame = login_at + timedelta(seconds=burst_after_login_s)
        b = BUILDER.build(new_uuid7(), BurstKind.SCAN_WARNING, first_frame + lead)
        assert min(f.captured_at for f in b.frames) == first_frame
        assert child_gate.decide(b).open is opens


# -- fail-safe -----------------------------------------------------------------


def test_session_source_exception_closes(gate: CaptureGate, sessions: Sessions) -> None:
    sessions.fail = RuntimeError("roster db locked")
    decision = gate.decide(burst())
    assert decision.reason is GateReason.ERROR
    assert decision.images_withheld is ImagesWithheld.NO_CONSENT
    assert isinstance(gate.permit(burst()), GateDecision)


def test_bad_timezone_closes(sessions: Sessions, clock: FakeClock) -> None:
    # Since Phase 1 (1.7) a bundle with an unknown zone fails validation, so the
    # bad zone is injected after validation to keep testing the gate's own guard.
    good = bundle()
    bad = good.model_copy(
        update={"site": good.site.model_copy(update={"timezone": "Mars/Olympus"})}
    )
    gate = CaptureGate(sessions=sessions, bundles=StaticRosterSource(bad), clock=clock)
    assert gate.decide_live().reason is GateReason.ERROR


def test_bundle_with_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="Mars/Olympus"):
        bundle(timezone="Mars/Olympus")


def test_engine_signal_exception_closes(sessions: Sessions, roster: StaticRosterSource) -> None:
    def broken() -> bool:
        raise OSError("pipe closed")

    gate = CaptureGate(
        sessions=sessions, bundles=roster, clock=FakeClock(start_utc=T0), engine_on_break=broken
    )
    assert gate.decide_live().reason is GateReason.ERROR


class _BrokenClock:
    def monotonic(self) -> float:
        raise RuntimeError("clock")

    def now(self) -> datetime:
        raise RuntimeError("clock")


def test_clock_failure_closes(sessions: Sessions, roster: StaticRosterSource) -> None:
    gate = CaptureGate(sessions=sessions, bundles=roster, clock=_BrokenClock())
    decision = gate.decide_live()
    assert decision.reason is GateReason.ERROR
    assert decision.decided_at is None


def test_permit_issue_failure_closes(gate: CaptureGate, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object) -> CapturePermit:
        raise RuntimeError("mint")

    monkeypatch.setattr("ag_workstation.consent.gate._issue", boom)
    result = gate.permit(burst())
    assert isinstance(result, GateDecision)
    assert result.reason is GateReason.ERROR


@pytest.mark.parametrize(
    ("reason", "wire"),
    [
        (GateReason.OPEN, ImagesWithheld.NONE),
        (GateReason.NO_SESSION, ImagesWithheld.NO_WORKER),
        (GateReason.SESSION_CHANGED, ImagesWithheld.NO_WORKER),
        (GateReason.NO_CONSENT, ImagesWithheld.NO_CONSENT),
        (GateReason.ON_BREAK, ImagesWithheld.NO_WORKER),
        (GateReason.ERROR, ImagesWithheld.NO_CONSENT),
    ],
)
def test_reason_mapping(reason: GateReason, wire: ImagesWithheld) -> None:
    assert images_withheld_for(reason) is wire
    assert GateDecision(reason, T0).images_withheld is wire
    assert GateDecision(reason, T0).open is (reason is GateReason.OPEN)


def test_every_reason_is_mapped() -> None:
    for reason in GateReason:
        assert isinstance(images_withheld_for(reason), ImagesWithheld)


# -- sink ----------------------------------------------------------------------


def test_sink_is_an_evidence_sink(sink: GatedEvidenceSink) -> None:
    as_protocol: EvidenceSink = sink
    assert as_protocol is sink


def test_open_gate_stores_the_burst(
    sink: GatedEvidenceSink, root: Path, outcomes: list[CaptureOutcome]
) -> None:
    b = burst()
    sink.submit(b)  # returns nothing; the outcome goes to the listener
    (outcome,) = outcomes
    assert outcome.event_id == b.event_id
    assert outcome.gate_reason is GateReason.OPEN
    assert outcome.images_withheld is ImagesWithheld.NONE
    assert len(outcome.image_ids) == len(b.frames) == outcome.frames
    assert len(files(root)) == len(b.frames)


@pytest.mark.parametrize("reason", ["no_session", "no_consent", "session_changed", "on_break"])
def test_closed_gate_writes_nothing(
    reason: str,
    sessions: Sessions,
    clock: FakeClock,
    store: EvidenceStore,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_break = reason == "on_break"
    gate = CaptureGate(
        sessions=sessions,
        bundles=StaticRosterSource(bundle()),
        clock=clock,
        engine_on_break=lambda: engine_break,
    )
    if reason == "no_session":
        sessions.session = None
    elif reason == "no_consent":
        sessions.session = session(consent=False)
    elif reason == "session_changed":
        sessions.session = session(started_at=T0)
    calls: list[EvidenceBurst] = []
    real = store.save_burst

    def spy(b: EvidenceBurst, event_id: UUID | None = None, *, permit: CapturePermit) -> SaveResult:
        calls.append(b)
        return real(b, event_id, permit=permit)

    monkeypatch.setattr(store, "save_burst", spy)
    b = burst()
    outcome = GatedEvidenceSink(gate, store).save(b)
    assert outcome.gate_reason is GateReason(reason)
    assert outcome.images_withheld is images_withheld_for(GateReason(reason))
    assert outcome.image_ids == ()
    assert calls == []
    assert files(root) == []


def test_closed_gate_keeps_no_reference_to_the_burst(
    gate: CaptureGate, sessions: Sessions, store: EvidenceStore
) -> None:
    sessions.session = None
    sink = GatedEvidenceSink(gate, store)
    b = burst()
    before = sys.getrefcount(b)
    sink.submit(b)
    assert sys.getrefcount(b) == before


def test_store_refusal_disk_full_propagates(
    gate: CaptureGate, clock: FakeClock, tmp_path: Path
) -> None:
    with open_store(tmp_path / "tiny", clock, mb=1) as tiny:
        big = BurstBuilder(jpeg=lambda tag: b"\xff\xd8" + tag.encode() + b"\x00" * 600_000)
        b = big.build(new_uuid7(), BurstKind.SCAN_WARNING, T0 - timedelta(seconds=10))
        outcome = GatedEvidenceSink(gate, tiny).save(b)
    assert outcome.gate_reason is GateReason.OPEN
    assert outcome.images_withheld is ImagesWithheld.DISK_FULL
    assert 0 < len(outcome.image_ids) < len(b.frames)


class _FailingStore:
    def save_burst(
        self, burst: EvidenceBurst, event_id: UUID | None = None, *, permit: CapturePermit
    ) -> SaveResult:
        raise sqlite3.OperationalError("database is locked")


def test_store_exception_never_raises(gate: CaptureGate) -> None:
    outcome = GatedEvidenceSink(gate, _FailingStore()).save(burst())
    assert outcome.images_withheld is ImagesWithheld.DISK_FULL
    assert outcome.image_ids == ()


class _RaisingGate(CaptureGate):
    def permit(self, burst: EvidenceBurst) -> CapturePermit | GateDecision:
        raise RuntimeError("bug")


def test_gate_exception_in_sink_is_closed(
    sessions: Sessions, roster: StaticRosterSource, store: EvidenceStore, root: Path
) -> None:
    gate = _RaisingGate(sessions=sessions, bundles=roster, clock=FakeClock(start_utc=T0))
    outcome = GatedEvidenceSink(gate, store).save(burst())
    assert outcome.gate_reason is GateReason.ERROR
    assert outcome.images_withheld is ImagesWithheld.NO_CONSENT
    assert files(root) == []


def test_outcome_listener_failure_is_swallowed(gate: CaptureGate, store: EvidenceStore) -> None:
    def bad(_: CaptureOutcome) -> None:
        raise RuntimeError("listener")

    sink = GatedEvidenceSink(gate, store, on_outcome=bad)
    assert sink.save(burst()).images_withheld is ImagesWithheld.NONE


# -- mid-burst changes ---------------------------------------------------------


def test_logout_while_frames_are_written_withholds_the_burst(
    sink: GatedEvidenceSink, sessions: Sessions, root: Path, store: EvidenceStore
) -> None:
    b = burst()
    # permit() asks once; the store asks before each frame: log out after the third frame.
    sessions.logout_after = 4
    outcome = sink.save(b)
    assert outcome.images_withheld is ImagesWithheld.NO_WORKER
    assert outcome.image_ids == ()
    assert files(root) == []
    rows = store.event_images(b.event_id)
    assert len(rows) == 3
    assert {r.removed_reason for r in rows} == {RemovalReason.GATE_CLOSED}


def test_consent_withdrawn_after_the_last_frame_withholds(
    sink: GatedEvidenceSink, sessions: Sessions, root: Path
) -> None:
    b = burst()
    assert sessions.session is not None
    original: CurrentSession = sessions.session
    frames = len(b.frames)

    class Flip(Sessions):
        def current(self) -> CurrentSession | None:
            self.calls += 1
            # 1 (permit) + one per frame, then the final re-check sees no consent.
            return original if self.calls <= 1 + frames else replace(original, has_consent=False)

    gate = CaptureGate(
        sessions=Flip(), bundles=StaticRosterSource(bundle()), clock=FakeClock(start_utc=T0)
    )
    store = sink._store
    outcome = GatedEvidenceSink(gate, store).save(b)
    assert outcome.images_withheld is ImagesWithheld.NO_CONSENT
    assert outcome.image_ids == ()
    assert files(root) == []


def test_logout_between_trigger_and_submit_withholds(
    sink: GatedEvidenceSink, sessions: Sessions, root: Path
) -> None:
    b = burst()
    sessions.session = None  # logged out before the post-frames came in
    assert sink.save(b).images_withheld is ImagesWithheld.NO_WORKER
    sessions.session = session(started_at=T0)  # another worker logged in: frames predate them
    assert sink.save(b).images_withheld is ImagesWithheld.NO_WORKER
    assert files(root) == []


def test_withdraw_deletes_files_even_if_the_index_fails(
    sink: GatedEvidenceSink,
    sessions: Sessions,
    root: Path,
    store: EvidenceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    deletes: list[UUID] = []
    real_delete = store._files.delete

    def flaky_delete(image_id: UUID) -> bool:
        deletes.append(image_id)
        if len(deletes) == 1:
            raise PermissionError("locked by scanner")
        return real_delete(image_id)

    monkeypatch.setattr(store, "_remove", broken)
    monkeypatch.setattr(store._files, "delete", flaky_delete)
    sessions.logout_after = 4
    assert sink.save(burst()).images_withheld is ImagesWithheld.NO_WORKER
    # The one file whose delete failed is the only one left; recovery handles it.
    assert len(files(root)) == 1


# -- the permit ------------------------------------------------------------------


def test_store_refuses_without_a_permit(store: EvidenceStore, root: Path) -> None:
    with pytest.raises(PermitError):
        store.save_burst(burst(), permit=None)  # type: ignore[arg-type]  # the point of the test
    assert files(root) == []


def test_permit_cannot_be_forged(gate: CaptureGate) -> None:
    b = burst()
    decision = gate.decide(b)
    with pytest.raises(PermitError):
        CapturePermit(object(), b, decision, lambda: decision)


def test_closed_decision_cannot_become_a_permit() -> None:
    with pytest.raises(PermitError):
        permit_module._issue(burst(), GateDecision(GateReason.NO_SESSION, T0), lambda: None)  # type: ignore[arg-type,return-value]  # a closed decision


def test_permit_is_single_use_and_bound_to_its_burst(
    gate: CaptureGate, store: EvidenceStore, root: Path
) -> None:
    b, other = burst(), burst()
    permit = gate.permit(b)
    assert isinstance(permit, CapturePermit)
    with pytest.raises(PermitError, match="another burst"):
        store.save_burst(other, permit=permit)
    assert files(root) == []
    permit = gate.permit(b)
    assert isinstance(permit, CapturePermit)
    store.save_burst(b, permit=permit)
    with pytest.raises(PermitError, match="already used"):
        store.save_burst(b, permit=permit)


def test_permit_cannot_be_copied_or_pickled(gate: CaptureGate) -> None:
    permit = gate.permit(burst())
    assert isinstance(permit, CapturePermit)
    for attempt in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            attempt(permit)


def test_permit_recheck_fails_closed(gate: CaptureGate) -> None:
    b = burst()
    decision = gate.decide(b)

    def boom() -> GateDecision:
        raise RuntimeError("gate")

    assert permit_module._issue(b, decision, boom).recheck().reason is GateReason.ERROR
    weird = permit_module._issue(b, decision, lambda: "open")  # type: ignore[arg-type,return-value]  # a buggy callback
    assert weird.recheck().reason is GateReason.ERROR


# -- structure: the gate is the only path to disk ---------------------------------

SOURCES = Path(__file__).resolve().parents[2] / "workstation" / "ag_workstation"


def _sources() -> Iterator[tuple[str, ast.AST]]:
    for path in sorted(SOURCES.rglob("*.py")):
        rel = path.relative_to(SOURCES).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=rel)


def _uses(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.alias) and name in (node.name, node.asname):
            return True
        if isinstance(node, ast.Constant) and node.value == name:
            return True
    return False


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        # Only the gated sink calls the store's write API.
        ("save_burst", {"consent/sink.py"}),
        # Only the gate mints permits.
        ("_issue", {"consent/permit.py", "consent/gate.py"}),
        ("_MINT", {"consent/permit.py"}),
        # Only the store writes image files.
        ("EncryptedImageFiles", {"evidence/files.py", "evidence/store.py", "evidence/__init__.py"}),
    ],
)
def test_only_the_gate_path_reaches_the_frame_writer(name: str, allowed: set[str]) -> None:
    users = {rel for rel, tree in _sources() if _uses(tree, name)}
    assert users <= allowed, f"{name} used outside the capture gate path: {users - allowed}"


def test_store_calls_files_write_only_inside_save_frame() -> None:
    tree = ast.parse((SOURCES / "evidence" / "store.py").read_text(encoding="utf-8"))
    writers = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_files"
    ]
    assert writers == ["_save_frame"]
    save_burst = next(
        fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name == "save_burst"
    )
    assert any(
        isinstance(n, ast.Attribute) and n.attr in ("redeem", "recheck")
        for n in ast.walk(save_burst)
    )


BINARY_WRITERS = {
    # module: why it may write bytes to disk (2.R: an explicit, reasoned list)
    "evidence/files.py": "the encrypted evidence image files (the only image writer)",
    "queue/keys.py": "DPAPI-wrapped key files (data key, IPC key)",
    "scan/journal.py": "sealed scan journal lines, os.write on an O_APPEND descriptor",
}
"""`sync/sealed_file.py` (bundle, credentials) and `corrected_time.py` write
ASCII text; they hold no frame bytes (see `test_frame_bytes_stay_on_the_evidence_path`)."""


def test_no_other_binary_file_writers() -> None:
    """Images reach disk only through evidence/files.py; the other binary writers are listed."""
    found = set()
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "imwrite",
                "write_bytes",
                "tofile",
            ):
                found.add(rel)
            # os.write takes raw bytes (2.R: the old guard did not see it).
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "write"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                found.add(rel)
            if isinstance(node, ast.Constant) and node.value in ("wb", "ab", "xb", "w+b", "r+b"):
                found.add(rel)
    assert found <= set(BINARY_WRITERS), found - set(BINARY_WRITERS)


FRAME_BYTE_READERS = {
    "engine_api.py": "defines EvidenceFrame (validation, hash, size)",
    "evidence/ring.py": "the in-memory ring (size accounting)",
    "evidence/burst.py": "assembles bursts from ring frames",
    "evidence/store.py": "writes permitted frames through EncryptedImageFiles",
    "sync/client.py": "uploads a stored image read back from the store",
}


def test_frame_bytes_stay_on_the_evidence_path() -> None:
    """2.R: frame bytes (`.jpeg`) are touched only by the listed modules, so no
    other writer (journal, sealed files, record store) can receive them."""
    users = {
        rel
        for rel, tree in _sources()
        if not rel.startswith("capture/")
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "jpeg"
    }
    assert users <= set(FRAME_BYTE_READERS), users - set(FRAME_BYTE_READERS)


# -- property: no file appears while the gate is closed ---------------------------


Step = tuple[str, int]

_steps = st.lists(
    st.one_of(
        st.tuples(st.just("login"), st.integers(0, 1)),
        st.tuples(st.just("logout"), st.just(0)),
        st.tuples(st.just("consent"), st.integers(0, 1)),
        st.tuples(st.just("engine_break"), st.integers(0, 1)),
        st.tuples(st.just("advance"), st.integers(0, 20 * 60)),
        st.tuples(st.just("burst"), st.integers(1, 120)),
        st.tuples(st.just("burst"), st.integers(1, 600)),
        st.tuples(st.just("logout_mid_burst"), st.integers(1, 7)),
    ),
    min_size=1,
    max_size=25,
)


def _expected_closed(
    model: CurrentSession | None, engine_break: bool, b: EvidenceBurst, now: datetime
) -> bool:
    if model is None or not model.has_consent or engine_break:
        return True
    first = min([b.triggered_at, *(f.captured_at for f in b.frames)])
    if model.started_at > first:
        return True
    # The only break the bundle schedules near this window: lunch, 06:30-07:00 UTC daily.
    for days in (-1, 0, 1):
        start = LUNCH_START + timedelta(days=days)
        if start <= now and first < start + timedelta(minutes=30):
            return True
    return False


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(steps=_steps)
def test_property_no_file_while_the_gate_is_closed(steps: list[Step]) -> None:
    clock = FakeClock(start_utc=datetime(2026, 11, 20, 5, 0, tzinfo=UTC))
    sessions = Sessions(None)
    flags = {"break": False}
    roster = StaticRosterSource(bundle())
    gate = CaptureGate(
        sessions=sessions, bundles=roster, clock=clock, engine_on_break=lambda: flags["break"]
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "evidence"
        with open_store(root, clock) as store:
            sink = GatedEvidenceSink(gate, store)
            held = 0
            for action, arg in steps:
                if action == "login":
                    sessions.session = session(consent=bool(arg), started_at=clock.now())
                    clock.advance(300)  # the worker logs in, then works for a while
                elif action == "logout":
                    sessions.session = None
                elif action == "consent" and sessions.session is not None:
                    sessions.session = replace(sessions.session, has_consent=bool(arg))
                elif action == "engine_break":
                    flags["break"] = bool(arg)
                elif action == "advance":
                    clock.advance(arg)
                elif action in ("burst", "logout_mid_burst"):
                    b = burst(at=clock.now() - timedelta(seconds=arg if action == "burst" else 30))
                    closed = _expected_closed(sessions.session, flags["break"], b, clock.now())
                    sessions.calls = 0
                    sessions.logout_after = arg if action == "logout_mid_burst" else None
                    outcome = sink.save(b)
                    if action == "logout_mid_burst":
                        sessions.logout_after = None
                        sessions.session = None
                        closed = True
                    event(f"burst {outcome.images_withheld.value}")
                    if closed:
                        assert outcome.image_ids == ()
                        assert outcome.images_withheld is not ImagesWithheld.NONE
                    else:
                        assert outcome.images_withheld is ImagesWithheld.NONE
                        assert len(outcome.image_ids) == len(b.frames)
                        held += len(b.frames)
                    assert len(files(root)) == held


def test_engine_config_schedule_without_a_bundle(sessions: Sessions, clock: FakeClock) -> None:
    # 2.9-pre: the engine child has no bundle (no roster crosses IPC); it reads
    # the shifts and site timezone from the engine config.
    config = bundle().engine_config()
    gate = CaptureGate(sessions=sessions, bundles=None, clock=clock, schedule=lambda: config)
    clock_at(clock, LUNCH_START + timedelta(minutes=1))
    sessions.session = session(started_at=LUNCH_START - timedelta(hours=2))
    assert gate.decide_live().reason is GateReason.ON_BREAK
    clock.advance(60 * 60)
    assert gate.decide_live().open
    no_schedule = CaptureGate(sessions=sessions, bundles=None, clock=clock, schedule=lambda: None)
    assert no_schedule.decide_live().open
    broken = CaptureGate(
        sessions=sessions,
        bundles=None,
        clock=clock,
        schedule=lambda: 1 / 0,  # type: ignore[arg-type,return-value]  # a failing source
    )
    assert broken.decide_live().reason is GateReason.ERROR
