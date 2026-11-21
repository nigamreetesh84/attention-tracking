"""The worker's local record and disputes (PLAN 2.10): data scoping, minimization,
retention, the mirror never touching the scan path, and a dispute queued once."""

import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import (
    AttentionEventKind,
    AttentionEventPayload,
    DisputePayload,
    DisputeReasonCode,
    EventType,
    ImagesWithheld,
    ScanPayload,
    SessionEndPayload,
    SessionEndReason,
    SessionMethod,
    SessionStartPayload,
)
from ag_shared.ids import Uuid7Generator, new_uuid7
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.clock import FakeClock
from ag_workstation.queue import EventQueue, InMemoryKeyProvider
from ag_workstation.record import (
    DisputeOutcome,
    DisputeService,
    DisputeStatus,
    PersonalRecordStore,
    RecordMirror,
    apply_copy,
)
from pydantic import BaseModel, JsonValue

T0 = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)
ASHA = UUID("0193a0b0-0000-7000-8000-00000000a001")
RAVI = UUID("0193a0b0-0000-7000-8000-00000000a002")
KEY = InMemoryKeyProvider(b"k" * 32)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_utc=T0)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Iterator[PersonalRecordStore]:
    s = PersonalRecordStore.open(tmp_path / "my_record.db", KEY, clock)
    yield s
    s.close()


@pytest.fixture
def queue(tmp_path: Path, clock: FakeClock) -> Iterator[EventQueue]:
    q = EventQueue.open(tmp_path / "queue.db", KEY, clock)
    yield q
    q.close()


def event(
    user: UUID | None,
    session: UUID | None,
    *,
    state: AttentionState = AttentionState.DISTRACTED,
    at: datetime = T0,
    kind: AttentionEventKind = AttentionEventKind.SCAN_WARNING,
    images: int = 0,
) -> AttentionEventPayload:
    episode = kind is AttentionEventKind.STATE_EPISODE
    return AttentionEventPayload(
        kind=kind,
        state=state,
        confidence=0.8,
        started_at=at,
        ended_at=at + timedelta(seconds=75) if episode else None,
        scan_id=None if episode else new_uuid7(),
        session_id=session,
        user_id=user,
        signals={"yaw": 31.0},
        images_withheld=ImagesWithheld.NONE,
        image_ids=tuple(new_uuid7() for _ in range(images)),
    )


def scan(user: UUID | None, session: UUID | None, outcome: ScanOutcome) -> ScanPayload:
    state = {
        ScanOutcome.ATTENTIVE: AttentionState.ATTENTIVE,
        ScanOutcome.WARNED: AttentionState.DROWSY,
        ScanOutcome.UNMONITORED: AttentionState.UNKNOWN,
    }[outcome]
    return ScanPayload(
        session_id=session,
        user_id=user,
        barcode="SECRET-BARCODE-1",
        state_at_scan=state,
        outcome=outcome,
    )


def shift(store: PersonalRecordStore, user: UUID, at: datetime = T0) -> UUID:
    sid = new_uuid7()
    store.note_session_start(sid, user, at)
    return sid


# -- store: scoping -----------------------------------------------------------------------


def test_snapshot_shows_only_the_requested_workers_data(store: PersonalRecordStore) -> None:
    asha_shift = shift(store, ASHA)
    ravi_shift = shift(store, RAVI, T0 + timedelta(hours=8))
    for outcome in (ScanOutcome.ATTENTIVE, ScanOutcome.WARNED, ScanOutcome.UNMONITORED):
        store.note_scan(asha_shift, ASHA, T0, outcome)
    store.note_scan(ravi_shift, RAVI, T0, ScanOutcome.WARNED)
    asha_event, ravi_event = new_uuid7(), new_uuid7()
    store.note_event(asha_event, event(ASHA, asha_shift))
    store.note_event(ravi_event, event(RAVI, ravi_shift))

    mine = store.snapshot(ASHA)
    assert [s.session_id for s in mine.sessions] == [asha_shift]
    assert {s.user_id for s in mine.sessions} == {ASHA}
    (summary,) = mine.sessions
    assert (summary.scans, summary.warned, summary.unmonitored, summary.events) == (3, 1, 1, 1)
    assert [e.id for e in mine.events] == [asha_event]
    assert all(e.user_id == ASHA for e in mine.events)

    theirs = store.snapshot(RAVI)
    assert [e.id for e in theirs.events] == [ravi_event]
    assert store.snapshot(new_uuid7()).sessions == ()


def test_event_of_another_worker_in_my_session_is_not_shown(store: PersonalRecordStore) -> None:
    mine = shift(store, ASHA)
    store.note_event(new_uuid7(), event(RAVI, mine))
    assert store.snapshot(ASHA).events == ()


def test_scan_of_another_worker_does_not_change_my_counts(store: PersonalRecordStore) -> None:
    mine = shift(store, ASHA)
    store.note_scan(mine, RAVI, T0, ScanOutcome.WARNED)
    (summary,) = store.snapshot(ASHA).sessions
    assert summary.scans == 0


def test_event_for_checks_the_owner(store: PersonalRecordStore) -> None:
    sid = shift(store, ASHA)
    eid = new_uuid7()
    store.note_event(eid, event(ASHA, sid))
    assert store.event_for(ASHA, eid) is not None
    assert store.event_for(RAVI, eid) is None
    assert store.event_for(ASHA, new_uuid7()) is None


def test_unattributed_items_are_not_kept(store: PersonalRecordStore) -> None:
    assert not store.note_event(new_uuid7(), event(None, None))
    assert not store.note_event(new_uuid7(), event(ASHA, None))


# -- store: minimization, sealing, retention ------------------------------------------------


def test_nothing_identifying_is_stored_in_the_clear(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "r.db"
    with PersonalRecordStore.open(path, KEY, clock) as s:
        sid = shift(s, ASHA)
        s.note_scan(sid, ASHA, T0, ScanOutcome.WARNED)
        s.note_event(new_uuid7(), event(ASHA, sid, images=2))
    raw = path.read_bytes() + (
        (tmp_path / "r.db-wal").read_bytes() if (tmp_path / "r.db-wal").exists() else b""
    )
    assert str(ASHA).encode() not in raw
    assert b"distracted" not in raw
    assert b"yaw" not in raw


def test_images_are_only_counted(store: PersonalRecordStore) -> None:
    sid = shift(store, ASHA)
    store.note_event(new_uuid7(), event(ASHA, sid, images=3))
    (shown,) = store.snapshot(ASHA).events
    assert shown.images_saved == 3
    assert not hasattr(shown, "image_ids")


def test_rows_under_another_key_are_skipped(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "r.db"
    with PersonalRecordStore.open(path, KEY, clock) as s:
        sid = shift(s, ASHA)
        s.note_event(new_uuid7(), event(ASHA, sid))
    with PersonalRecordStore.open(path, InMemoryKeyProvider(b"x" * 32), clock) as other:
        snap = other.snapshot(ASHA)
        assert snap.sessions == () and snap.unreadable == 1


def test_a_row_moved_to_another_id_fails(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "r.db"
    with PersonalRecordStore.open(path, KEY, clock) as s:
        sid = shift(s, ASHA)
        eid = new_uuid7()
        s.note_event(eid, event(ASHA, sid))
    conn = sqlite3.connect(path)
    conn.execute("UPDATE record_event SET id = ?", (str(new_uuid7()),))
    conn.commit()
    conn.close()
    with PersonalRecordStore.open(path, KEY, clock) as s:
        snap = s.snapshot(ASHA)
        assert snap.events == () and snap.unreadable == 1


def test_retention_purges_old_rows(store: PersonalRecordStore, clock: FakeClock) -> None:
    old = shift(store, ASHA)
    store.note_event(new_uuid7(), event(ASHA, old))
    clock.advance(timedelta(days=8).total_seconds())
    recent = shift(store, ASHA, clock.now())
    snap = store.snapshot(ASHA)
    assert [s.session_id for s in snap.sessions] == [recent]
    assert snap.events == ()
    assert store.purge() == 0


def test_session_end_and_newest_first(store: PersonalRecordStore) -> None:
    first = shift(store, ASHA)
    store.note_session_end(first, T0 + timedelta(hours=8))
    second = shift(store, ASHA, T0 + timedelta(days=1))
    store.note_session_start(second, ASHA, T0)  # idempotent, keeps the first start
    sessions = store.snapshot(ASHA).sessions
    assert [s.session_id for s in sessions] == [second, first]
    assert sessions[1].ended_at == T0 + timedelta(hours=8)
    assert sessions[0].ended_at is None and sessions[0].started_at == T0 + timedelta(days=1)


def test_scan_without_a_mirrored_start_creates_the_session(store: PersonalRecordStore) -> None:
    sid = new_uuid7()
    store.note_scan(sid, ASHA, T0 + timedelta(minutes=3), ScanOutcome.ATTENTIVE)
    (summary,) = store.snapshot(ASHA).sessions
    assert summary.started_at == T0 + timedelta(minutes=3) and summary.scans == 1


def test_duplicate_event_is_kept_once(store: PersonalRecordStore) -> None:
    sid = shift(store, ASHA)
    eid = new_uuid7()
    assert store.note_event(eid, event(ASHA, sid))
    assert not store.note_event(eid, event(ASHA, sid))
    assert len(store.snapshot(ASHA).events) == 1


def test_open_rejects_bad_retention_and_newer_db(tmp_path: Path, clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="retention"):
        PersonalRecordStore.open(tmp_path / "a.db", KEY, clock, retention=timedelta(0))
    conn = sqlite3.connect(tmp_path / "b.db")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        PersonalRecordStore.open(tmp_path / "b.db", KEY, clock)


# -- mirror ---------------------------------------------------------------------------------


@dataclass
class ListQueue:
    items: list[tuple[str, dict[str, JsonValue], UUID | None]] = field(default_factory=list)
    fail: bool = False

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        if self.fail:
            raise OSError("disk")
        body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        self.items.append((item_type, body, record_id))
        return None


def test_mirror_copies_sessions_scans_and_events(store: PersonalRecordStore) -> None:
    inner = ListQueue()
    mirror = RecordMirror(inner, store, start=False)
    sid = new_uuid7()
    mirror.enqueue(
        EventType.SESSION_START,
        SessionStartPayload(user_id=ASHA, method=SessionMethod.EMPLOYEE_ID_PIN),
        T0,
        record_id=sid,
    )
    mirror.enqueue(EventType.SCAN, scan(ASHA, sid, ScanOutcome.WARNED), T0)
    mirror.enqueue(EventType.SCAN, scan(None, None, ScanOutcome.UNMONITORED), T0)
    eid = new_uuid7()
    mirror.enqueue(EventType.ATTENTION_EVENT, event(ASHA, sid), T0, record_id=eid)
    mirror.enqueue(
        EventType.SESSION_END,
        SessionEndPayload(session_id=sid, end_reason=SessionEndReason.LOGOUT),
        T0 + timedelta(hours=1),
    )
    mirror.enqueue(EventType.LOCAL_ALERT, {"kind": "other", "detail": {}}, T0)
    assert len(inner.items) == 6  # the real queue got everything, unchanged
    assert mirror.flush()
    snap = store.snapshot(ASHA)
    (summary,) = snap.sessions
    assert (summary.scans, summary.warned, summary.events) == (1, 1, 1)
    assert summary.ended_at == T0 + timedelta(hours=1)
    assert [e.id for e in snap.events] == [eid]
    mirror.close()


def test_mirror_propagates_queue_failure_and_copies_nothing(store: PersonalRecordStore) -> None:
    mirror = RecordMirror(ListQueue(fail=True), store, start=False)
    with pytest.raises(OSError):
        mirror.enqueue(EventType.SCAN, scan(ASHA, new_uuid7(), ScanOutcome.WARNED), T0)
    assert mirror.drain() == 0


class BrokenWriter:
    def __getattr__(self, name: str) -> object:
        def fail(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        return fail


def test_mirror_store_failure_never_reaches_the_writer() -> None:
    inner = ListQueue()
    mirror = RecordMirror(inner, BrokenWriter(), start=False)  # type: ignore[arg-type]  # duck-typed failing writer
    mirror.enqueue(EventType.SCAN, scan(ASHA, new_uuid7(), ScanOutcome.ATTENTIVE), T0)
    assert mirror.drain() == 1
    assert mirror.failed == 1 and len(inner.items) == 1


class SlowWriter:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.scans = 0

    def note_scan(self, *args: object) -> None:
        self.release.wait(5)
        self.scans += 1

    def __getattr__(self, name: str) -> object:
        return lambda *a, **k: None


def test_a_slow_record_file_never_delays_enqueue() -> None:
    writer = SlowWriter()
    mirror = RecordMirror(ListQueue(), writer, capacity=2)  # type: ignore[arg-type]  # duck-typed slow writer
    started = time.monotonic()
    for _ in range(5):
        mirror.enqueue(EventType.SCAN, scan(ASHA, new_uuid7(), ScanOutcome.ATTENTIVE), T0)
    assert time.monotonic() - started < 0.5
    assert mirror.dropped >= 1  # the bounded buffer drops copies, never blocks
    writer.release.set()
    assert mirror.flush(5)
    mirror.close()
    mirror.enqueue(EventType.SCAN, scan(ASHA, new_uuid7(), ScanOutcome.ATTENTIVE), T0)
    assert mirror.dropped >= 2  # after close: dropped, the queue still took it


def test_mirror_rejects_bad_capacity(store: PersonalRecordStore) -> None:
    with pytest.raises(ValueError):
        RecordMirror(ListQueue(), store, capacity=0)


def test_apply_copy_takes_mappings_and_skips_idless_items(store: PersonalRecordStore) -> None:
    apply_copy(
        store,
        EventType.SESSION_START,
        {"user_id": str(ASHA), "method": "employee_id_pin"},
        T0,
        None,
    )
    apply_copy(store, EventType.ATTENTION_EVENT, event(ASHA, new_uuid7()), T0, None)
    assert store.snapshot(ASHA).sessions == ()


def test_mirror_uses_the_queue_assigned_id(store: PersonalRecordStore, queue: EventQueue) -> None:
    mirror = RecordMirror(queue, store, start=False)
    sid = shift(store, ASHA)
    queued = mirror.enqueue(EventType.ATTENTION_EVENT, event(ASHA, sid), T0)
    mirror.drain()
    (shown,) = store.snapshot(ASHA).events
    assert shown.id == queued.id  # type: ignore[attr-defined]  # EventQueue returns QueuedRecord


# -- disputes -------------------------------------------------------------------------------


def disputes_in(queue: EventQueue) -> list[DisputePayload]:
    out = []
    for record in queue.claim_batch():
        if record.type == EventType.DISPUTE:
            out.append(DisputePayload.model_validate(record.payload))
    return out


def queued_ids(queue: EventQueue) -> list[UUID]:
    records = [r for r in queue.claim_batch() if r.type == EventType.DISPUTE]
    queue.recover_sending()
    return [r.id for r in records]


@pytest.fixture
def my_event(store: PersonalRecordStore) -> UUID:
    sid = shift(store, ASHA)
    eid = new_uuid7()
    store.note_event(eid, event(ASHA, sid))
    return eid


def test_dispute_is_queued_with_the_contract_payload(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    service = DisputeService(store, queue, clock)
    result = service.submit(ASHA, my_event, "  I was paying\nattention. ")
    assert result.outcome is DisputeOutcome.QUEUED
    (record,) = queue.claim_batch()
    assert record.type == EventType.DISPUTE and record.id == result.dispute_id
    payload = DisputePayload.model_validate(record.payload)
    assert payload == DisputePayload(
        attention_event_id=my_event, user_id=ASHA, message="I was paying attention."
    )
    (shown,) = store.snapshot(ASHA).events
    assert shown.dispute is DisputeStatus.QUEUED


def test_dispute_reason_code_is_queued_and_survives_recovery(
    tmp_path: Path, clock: FakeClock, queue: EventQueue, my_event: UUID, store: PersonalRecordStore
) -> None:
    """3.1b (owner decision 1d): the optional reason code is sent, and a dispute
    saved before a crash is re-queued with the same code."""
    flaky = FlakyQueue(queue)
    saved = DisputeService(store, flaky, clock).submit(
        ASHA, my_event, "x", reason=DisputeReasonCode.ON_BREAK
    )
    assert saved.outcome is DisputeOutcome.SAVED
    store.close()
    with PersonalRecordStore.open(tmp_path / "my_record.db", KEY, clock) as reopened:
        assert DisputeService(reopened, queue, clock).recover() == 1
    (record,) = queue.claim_batch()
    assert record.id == saved.dispute_id
    payload = DisputePayload.model_validate(record.payload)
    assert payload.reason is DisputeReasonCode.ON_BREAK
    assert payload.message == "x"


def test_dispute_without_a_reason_code_sends_none(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    DisputeService(store, queue, clock).submit(ASHA, my_event, "x")
    (record,) = queue.claim_batch()
    assert DisputePayload.model_validate(record.payload).reason is None


def test_double_tap_queues_one_dispute(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    service = DisputeService(store, queue, clock)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(service.submit(ASHA, my_event, "x")))
        for _ in range(5)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count(DisputeOutcome.QUEUED) == 1
    assert outcomes.count(DisputeOutcome.ALREADY_DISPUTED) == 4
    assert len({r.dispute_id for r in results}) == 1
    assert len(queued_ids(queue)) == 1


def test_resubmit_after_restart_queues_nothing_new(
    tmp_path: Path, clock: FakeClock, queue: EventQueue, my_event: UUID, store: PersonalRecordStore
) -> None:
    first = DisputeService(store, queue, clock).submit(ASHA, my_event, "x")
    store.close()
    with PersonalRecordStore.open(tmp_path / "my_record.db", KEY, clock) as reopened:
        again = DisputeService(reopened, queue, clock).submit(ASHA, my_event, "other reason")
    assert again.outcome is DisputeOutcome.ALREADY_DISPUTED
    assert again.dispute_id == first.dispute_id
    assert len(queued_ids(queue)) == 1


def test_resubmit_after_the_dispute_was_sent_queues_nothing(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    service = DisputeService(store, queue, clock)
    service.submit(ASHA, my_event, "x")
    queue.mark_done([r.id for r in queue.claim_batch()])  # the server accepted it
    assert service.submit(ASHA, my_event, "x").outcome is DisputeOutcome.ALREADY_DISPUTED
    assert service.recover() == 0
    assert queue.stats().depth == 0


class FlakyQueue:
    def __init__(self, inner: EventQueue) -> None:
        self.inner = inner
        self.fail = True

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        if self.fail:
            raise sqlite3.OperationalError("database is locked")
        return self.inner.enqueue(item_type, payload, occurred_at, record_id=record_id)


def test_queue_failure_saves_the_dispute_and_recover_queues_it_once(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    flaky = FlakyQueue(queue)
    service = DisputeService(store, flaky, clock)
    saved = service.submit(ASHA, my_event, "x")
    assert saved.outcome is DisputeOutcome.SAVED
    assert store.snapshot(ASHA).events[0].dispute is DisputeStatus.PENDING
    # Retry while the queue still fails: still one saved dispute, nothing queued.
    assert service.submit(ASHA, my_event, "x").outcome is DisputeOutcome.ALREADY_DISPUTED
    flaky.fail = False
    assert service.recover() == 1
    assert service.recover() == 0
    assert queued_ids(queue) == [saved.dispute_id]
    assert store.snapshot(ASHA).events[0].dispute is DisputeStatus.QUEUED


def test_crash_between_queue_write_and_mark_is_not_duplicated(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    service = DisputeService(store, queue, clock)
    result = service.submit(ASHA, my_event, "x")
    conn = sqlite3.connect(store_path(store))
    conn.execute("UPDATE record_dispute SET status = 'pending'")  # the mark was lost
    conn.commit()
    conn.close()
    assert service.recover() == 1  # "already queued": marked, nothing new
    assert queued_ids(queue) == [result.dispute_id]


def store_path(store: PersonalRecordStore) -> str:
    conn: sqlite3.Connection = store._conn
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row[2])


def test_resubmit_retries_a_pending_dispute(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    flaky = FlakyQueue(queue)
    service = DisputeService(store, flaky, clock)
    saved = service.submit(ASHA, my_event, "x")
    flaky.fail = False
    # The next submit first recovers pending disputes, then finds the saved one.
    assert service.submit(ASHA, my_event, "x").outcome is DisputeOutcome.ALREADY_DISPUTED
    assert queued_ids(queue) == [saved.dispute_id]


def test_cannot_dispute_another_workers_event(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    service = DisputeService(store, queue, clock)
    assert service.submit(RAVI, my_event, "x").outcome is DisputeOutcome.NOT_FOUND
    assert service.submit(ASHA, new_uuid7(), "x").outcome is DisputeOutcome.NOT_FOUND
    assert queue.stats().depth == 0


def test_empty_message_and_store_failure_are_failed(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    service = DisputeService(store, queue, clock)
    assert service.submit(ASHA, my_event, "  ").outcome is DisputeOutcome.FAILED
    store.close()
    assert service.submit(ASHA, my_event, "x").outcome is DisputeOutcome.FAILED
    assert service.recover() == 0


def test_long_message_is_truncated(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    DisputeService(store, queue, clock).submit(ASHA, my_event, "a" * 5000)
    (payload,) = disputes_in(queue)
    assert len(payload.message) == 2000


def test_dispute_id_is_a_uuid7_from_the_injected_generator(
    store: PersonalRecordStore, queue: EventQueue, clock: FakeClock, my_event: UUID
) -> None:
    ids = Uuid7Generator(now=clock.now)
    result = DisputeService(store, queue, clock, ids=ids).submit(ASHA, my_event, "x")
    assert result.dispute_id is not None and result.dispute_id.version == 7


class RejectingQueue:
    def enqueue(self, *args: object, **kwargs: object) -> object:
        raise ValueError("record_id must be a UUIDv7")


def test_other_queue_value_errors_keep_the_dispute_pending(
    store: PersonalRecordStore, clock: FakeClock, my_event: UUID
) -> None:
    service = DisputeService(store, RejectingQueue(), clock)
    assert service.submit(ASHA, my_event, "x").outcome is DisputeOutcome.SAVED
    assert len(store.pending_disputes()) == 1


# -- end to end with the real scan flow --------------------------------------------------------


@dataclass(frozen=True)
class OpenSession:
    session_id: UUID
    user_id: UUID
    started_at: datetime
    has_consent: bool = True


@dataclass
class OneSession:
    session: OpenSession | None

    def current(self) -> OpenSession | None:
        return self.session


class NeverScheduler:
    def schedule(self, delay_s: float, callback: object) -> object:
        class Handle:
            def cancel(self) -> None:
                pass

        return Handle()


def test_scan_flow_through_the_mirror_fills_my_record(
    tmp_path: Path, clock: FakeClock, store: PersonalRecordStore, queue: EventQueue
) -> None:
    from ag_workstation.fakes import FakeStationEngine
    from ag_workstation.queue import PayloadCipher
    from ag_workstation.scan import ScanFlow, ScanJournal

    engine = FakeStationEngine(clock)
    engine.set_state(AttentionState.DISTRACTED)
    mirror = RecordMirror(queue, store, start=False)
    journal = ScanJournal.open(tmp_path / "scan.jnl", PayloadCipher(KEY))
    me = OpenSession(new_uuid7(), ASHA, T0)
    sessions = OneSession(me)
    flow = ScanFlow(
        queue=mirror,
        journal=journal,
        engine=engine,
        evidence=engine,
        sessions=sessions,
        clock=clock,
        scheduler=NeverScheduler(),  # type: ignore[arg-type]  # timers never fire here
    )
    warned = flow.on_barcode("BOX-1")
    assert warned.warn and warned.event_id is not None
    engine.set_state(AttentionState.ATTENTIVE)
    flow.on_barcode("BOX-2")
    sessions.session = None
    flow.on_barcode("BOX-3")  # unattributed: nobody's record
    flow.close()  # queues the warned scan's event (no capture outcome)
    mirror.drain()
    snap = store.snapshot(ASHA)
    (summary,) = snap.sessions
    assert (summary.scans, summary.warned, summary.events) == (2, 1, 1)
    assert [e.id for e in snap.events] == [warned.event_id]
    assert queue.stats().depth == 4  # 3 scans and 1 event, unchanged by the mirror
    journal.close()
