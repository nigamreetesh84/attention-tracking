"""Encrypted local event queue: storage, ordering, results, backoff and dead letters."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import (
    SCHEMA_REVISION,
    EventBatch,
    EventType,
    ItemResult,
    ItemStatus,
    ScanPayload,
)
from ag_shared.ids import is_uuid7, new_uuid7
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.clock import FakeClock
from ag_workstation.queue import (
    MAX_BACKOFF_S,
    DeadLetterReason,
    EventQueue,
    InMemoryKeyProvider,
    PayloadCipher,
    PayloadIntegrityError,
    record_associated_data,
)

KEY = bytes(range(32))
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
SENTINEL = "SENTINEL-barcode-7f3a9c"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_utc=T0)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "queue.db"


@pytest.fixture
def q(db: Path, clock: FakeClock) -> Iterator[EventQueue]:
    queue = EventQueue.open(db, InMemoryKeyProvider(KEY), clock, rng=lambda: 1.0)
    yield queue
    queue.close()


def _scan(barcode: str = "ABC") -> ScanPayload:
    return ScanPayload(
        barcode=barcode, state_at_scan=AttentionState.ATTENTIVE, outcome=ScanOutcome.ATTENTIVE
    )


def test_open_uses_wal(q: EventQueue, db: Path) -> None:
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_enqueue_and_claim_round_trip(q: EventQueue, clock: FakeClock) -> None:
    rec = q.enqueue(EventType.SCAN, _scan(SENTINEL), occurred_at=T0)
    assert is_uuid7(rec.id)
    assert rec.seq == 1
    clock.advance(1)
    [claimed] = q.claim_batch()
    assert claimed.id == rec.id
    assert claimed.seq == 1
    assert claimed.type == "scan"
    assert claimed.schema == SCHEMA_REVISION
    assert claimed.created_at == T0
    assert claimed.occurred_at == T0
    assert claimed.attempts == 1
    assert claimed.payload["barcode"] == SENTINEL
    # The wire form is a valid batch item.
    batch = EventBatch(batch_id=new_uuid7(), sent_at=T0, items=(claimed.to_item(),))  # type: ignore[arg-type]  # validated from the raw dict
    assert batch.items[0].id == rec.id
    assert batch.items[0].type == "scan"


def test_enqueue_dict_payload_and_explicit_id(q: EventQueue) -> None:
    rid = new_uuid7()
    rec = q.enqueue("local_alert", {"kind": "disk_full", "detail": {"x": 1}}, T0, record_id=rid)
    assert rec.id == rid
    [claimed] = q.claim_batch()
    assert claimed.payload == {"kind": "disk_full", "detail": {"x": 1}}


def test_enqueue_rejects_bad_input(q: EventQueue) -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        q.enqueue("scan", {}, T0, record_id=UUID(int=1))
    with pytest.raises(ValueError, match="timezone"):
        q.enqueue("scan", {}, datetime(2026, 1, 1))
    rid = new_uuid7()
    q.enqueue("scan", {}, T0, record_id=rid)
    with pytest.raises(ValueError, match="already queued"):
        q.enqueue("scan", {}, T0, record_id=rid)
    # A failed enqueue does not burn a sequence number.
    assert q.enqueue("scan", {}, T0).seq == 2


def test_no_plaintext_on_disk(q: EventQueue, db: Path, clock: FakeClock) -> None:
    for i in range(20):
        q.enqueue(EventType.SCAN, _scan(f"{SENTINEL}-{i}"), T0)
    claimed = q.claim_batch(max_items=5)
    q.apply_results(
        claimed, [ItemResult(id=claimed[0].id, status=ItemStatus.REJECTED, reason=SENTINEL)]
    )
    files = [p for p in db.parent.iterdir() if p.name.startswith(db.name)]
    assert {p.name for p in files} >= {db.name, db.name + "-wal"}
    for path in files:
        data = path.read_bytes()
        assert SENTINEL.encode() not in data, path.name
        assert SENTINEL.encode("utf-16-le") not in data, path.name
    q.checkpoint()
    assert SENTINEL.encode() not in db.read_bytes()


def test_seq_monotonic_and_survives_deletion(q: EventQueue) -> None:
    seqs = [q.enqueue("scan", {}, T0).seq for _ in range(3)]
    assert seqs == [1, 2, 3]
    claimed = q.claim_batch()
    q.mark_done([r.id for r in claimed])
    assert q.stats().depth == 0
    assert q.enqueue("scan", {}, T0).seq == 4


def test_claim_is_oldest_first_and_limited(q: EventQueue) -> None:
    ids = [q.enqueue("scan", {"n": i}, T0).id for i in range(10)]
    first = q.claim_batch(max_items=4)
    assert [r.id for r in first] == ids[:4]
    second = q.claim_batch(max_items=100)
    assert [r.id for r in second] == ids[4:]
    assert q.claim_batch() == []


def test_claim_respects_byte_limit_but_returns_at_least_one(q: EventQueue) -> None:
    for _ in range(3):
        q.enqueue("dispute", {"message": "x" * 1000}, T0)
    batch = q.claim_batch(max_bytes=2500)
    assert len(batch) == 2
    assert sum(r.wire_size() for r in batch) <= 2500
    assert len(q.claim_batch(max_bytes=10)) == 1


def test_claim_validates_limits(q: EventQueue) -> None:
    with pytest.raises(ValueError, match="max_items"):
        q.claim_batch(max_items=0)
    with pytest.raises(ValueError, match="max_items"):
        q.claim_batch(max_items=201)


def test_apply_results(q: EventQueue, clock: FakeClock) -> None:
    recs = [q.enqueue("scan", {"n": i}, T0) for i in range(5)]
    claimed = q.claim_batch()
    results = [
        ItemResult(id=recs[0].id, status=ItemStatus.ACCEPTED),
        ItemResult(id=recs[1].id, status=ItemStatus.DUPLICATE),
        ItemResult(id=recs[2].id, status=ItemStatus.REJECTED, reason="bad field"),
        ItemResult(id=recs[3].id, status=ItemStatus.RETRY_LATER, reason="parent missing"),
        ItemResult(id=new_uuid7(), status=ItemStatus.ACCEPTED),  # not ours: ignored
        # recs[4] has no result: kept and retried
    ]
    summary = q.apply_results(claimed, results)
    assert (summary.done, summary.dead, summary.retry) == (2, 1, 2)
    stats = q.stats()
    assert stats.depth == 2
    assert stats.sending == 0
    assert stats.dead_letter_count == 1
    dead = q.dead_letters()
    assert [d.id for d in dead] == [recs[2].id]
    assert dead[0].reason is DeadLetterReason.REJECTED
    assert dead[0].detail == "bad field"
    assert dead[0].dead_at == T0
    # Retried items wait for their backoff.
    assert q.claim_batch() == []
    clock.advance(1.0)
    assert [r.id for r in q.claim_batch()] == [recs[3].id, recs[4].id]


def test_dead_letter_payload_still_readable(q: EventQueue) -> None:
    rec = q.enqueue("scan", {"barcode": "B"}, T0)
    q.claim_batch()
    q.dead_letter(rec.id, DeadLetterReason.REJECTED, "nope " * 200)
    [dead] = q.dead_letters()
    assert dead.payload == {"barcode": "B"}
    assert dead.detail is not None
    assert len(dead.detail) == 500


def test_backoff_schedule(q: EventQueue, clock: FakeClock) -> None:
    rec = q.enqueue("scan", {}, T0)
    waits = []
    for _ in range(12):
        [claimed] = q.claim_batch()
        q.retry([claimed.id])
        due = q.next_attempt_at(rec.id)
        assert due is not None
        waits.append((due - clock.now()).total_seconds())
        clock.advance(waits[-1] - 0.5)
        assert q.claim_batch() == []  # not before it is due
        clock.advance(0.5)
    assert waits == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]


def test_backoff_uses_injected_rng(db: Path, clock: FakeClock) -> None:
    with EventQueue.open(db, InMemoryKeyProvider(KEY), clock, rng=lambda: 0.0) as q:
        rec = q.enqueue("scan", {}, T0)
        for _ in range(4):
            q.claim_batch()
            q.retry([rec.id])
            clock.advance(MAX_BACKOFF_S)
        q.claim_batch()
        q.retry([rec.id])
        assert q.next_attempt_at(rec.id) == clock.now() + timedelta(seconds=8)  # 16 / 2


def test_clock_step_back_does_not_strand_records(q: EventQueue, db: Path) -> None:
    rec = q.enqueue("scan", {}, T0)
    q.claim_batch()
    q.retry([rec.id])
    # Simulate a wall clock that was far ahead when the retry was stored.
    with sqlite3.connect(db) as conn:
        far = int((T0 + timedelta(days=3)).timestamp() * 1000)
        conn.execute("UPDATE queue_record SET next_attempt_at = ?", (far,))
    assert [r.id for r in q.claim_batch()] == [rec.id]


def test_retry_only_touches_sending(q: EventQueue) -> None:
    rec = q.enqueue("scan", {}, T0)
    q.retry([rec.id])  # still pending: unchanged
    assert q.next_attempt_at(rec.id) == T0
    assert q.next_attempt_at(new_uuid7()) is None


def test_stats(q: EventQueue, clock: FakeClock) -> None:
    empty = q.stats()
    assert (empty.depth, empty.sending, empty.oldest_unsent_at, empty.dead_letter_count) == (
        0,
        0,
        None,
        0,
    )
    q.enqueue("scan", {}, T0)
    clock.advance(5)
    q.enqueue("scan", {}, T0)
    q.claim_batch(max_items=1)
    stats = q.stats()
    assert stats.depth == 2
    assert stats.sending == 1
    assert stats.oldest_unsent_at == T0


def test_recover_sending_on_open(db: Path, clock: FakeClock) -> None:
    keys = InMemoryKeyProvider(KEY)
    with EventQueue.open(db, keys, clock) as q:
        q.enqueue("scan", {}, T0)
        q.enqueue("scan", {}, T0)
        q.claim_batch()
        assert q.stats().sending == 2
    # A writer-only process must not steal the sender's in-flight records.
    with EventQueue.open(db, keys, clock, recover_sending=False) as q:
        assert q.stats().sending == 2
    with EventQueue.open(db, keys, clock) as q:
        stats = q.stats()
        assert (stats.sending, stats.depth) == (0, 2)
        assert len(q.claim_batch()) == 2


def test_tampered_ciphertext_is_dead_lettered(q: EventQueue, db: Path) -> None:
    a = q.enqueue("scan", {"n": 1}, T0)
    b = q.enqueue("scan", {"n": 2}, T0)
    with sqlite3.connect(db) as conn:
        (blob,) = conn.execute("SELECT payload FROM queue_record WHERE seq = 1").fetchone()
        conn.execute(
            "UPDATE queue_record SET payload = ? WHERE seq = 1", (bytes([blob[0] ^ 1]) + blob[1:],)
        )
    claimed = q.claim_batch()
    assert [r.id for r in claimed] == [b.id]
    [dead] = q.dead_letters()
    assert dead.id == a.id
    assert dead.reason is DeadLetterReason.INTEGRITY
    assert dead.payload is None
    with pytest.raises(PayloadIntegrityError, match=str(a.id)):
        q.read_dead_letter_payload(a.id)


def test_swapped_ciphertexts_detected(q: EventQueue, db: Path) -> None:
    a = q.enqueue("scan", {"n": 1}, T0)
    b = q.enqueue("scan", {"n": 2}, T0)
    with sqlite3.connect(db) as conn:
        rows = {
            seq: (nonce, payload)
            for seq, nonce, payload in conn.execute("SELECT seq, nonce, payload FROM queue_record")
        }
        for seq, other in ((1, 2), (2, 1)):
            conn.execute(
                "UPDATE queue_record SET nonce = ?, payload = ? WHERE seq = ?", (*rows[other], seq)
            )
    assert q.claim_batch() == []
    assert {d.id for d in q.dead_letters()} == {a.id, b.id}
    assert q.stats().depth == 0


def test_swapped_metadata_detected(q: EventQueue, db: Path) -> None:
    """Changing a record's type (part of the associated data) is detected."""
    q.enqueue("scan", {"n": 1}, T0)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE queue_record SET type = 'dispute'")
    assert q.claim_batch() == []
    assert q.stats().dead_letter_count == 1


def test_undecodable_plaintext_is_dead_lettered(q: EventQueue, db: Path) -> None:
    rec = q.enqueue("scan", {}, T0)
    nonce, blob = PayloadCipher(InMemoryKeyProvider(KEY)).encrypt(
        b"not json", record_associated_data(rec.id, "scan", rec.seq, SCHEMA_REVISION)
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE queue_record SET nonce = ?, payload = ?", (nonce, blob))
    assert q.claim_batch() == []
    [dead] = q.dead_letters()
    assert dead.reason is DeadLetterReason.UNDECODABLE


def test_purge_dead_letters(q: EventQueue, clock: FakeClock) -> None:
    old = q.enqueue("scan", {}, T0)
    q.claim_batch()
    q.dead_letter(old.id, DeadLetterReason.REJECTED)
    clock.advance(timedelta(days=20).total_seconds())
    new = q.enqueue("scan", {}, T0)
    q.claim_batch()
    q.dead_letter(new.id, DeadLetterReason.REJECTED)
    clock.advance(timedelta(days=10, seconds=1).total_seconds())
    assert q.purge_dead_letters() == 1
    assert [d.id for d in q.dead_letters()] == [new.id]
    assert q.purge_dead_letters(retention=timedelta(days=1)) == 1
    assert q.stats().dead_letter_count == 0


def test_dead_letter_unknown_id_is_noop(q: EventQueue) -> None:
    q.dead_letter(new_uuid7(), DeadLetterReason.REJECTED)
    assert q.stats().dead_letter_count == 0


def test_wrong_key_dead_letters_instead_of_crashing(db: Path, clock: FakeClock) -> None:
    with EventQueue.open(db, InMemoryKeyProvider(KEY), clock) as q:
        q.enqueue("scan", {}, T0)
    with EventQueue.open(db, InMemoryKeyProvider(bytes(32)), clock) as q:
        assert q.claim_batch() == []
        assert q.stats().dead_letter_count == 1


def test_refuses_newer_database_version(db: Path, clock: FakeClock) -> None:
    EventQueue.open(db, InMemoryKeyProvider(KEY), clock).close()
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        EventQueue.open(db, InMemoryKeyProvider(KEY), clock)


def test_close_is_idempotent(db: Path, clock: FakeClock) -> None:
    q = EventQueue.open(db, InMemoryKeyProvider(KEY), clock)
    q.close()
    q.close()


def test_failed_transaction_rolls_back(q: EventQueue) -> None:
    with pytest.raises(TypeError):
        q.enqueue("scan", {"x": object()}, T0)  # type: ignore[dict-item]  # not JSON on purpose
    assert q.stats().depth == 0
    assert q.enqueue("scan", {}, T0).seq == 1


def test_read_dead_letter_payload(q: EventQueue) -> None:
    rec = q.enqueue("scan", {"barcode": "B"}, T0)
    q.dead_letter(rec.id, DeadLetterReason.REJECTED)
    assert q.read_dead_letter_payload(rec.id) == {"barcode": "B"}
    with pytest.raises(KeyError):
        q.read_dead_letter_payload(new_uuid7())


def test_sealed_item_with_wrong_shape_is_undecodable(q: EventQueue, db: Path) -> None:
    rec = q.enqueue("scan", {}, T0)
    nonce, blob = PayloadCipher(InMemoryKeyProvider(KEY)).encrypt(
        b'{"occurred_at": "2026-01-01T00:00:00", "payload": {}}',  # naive time
        record_associated_data(rec.id, "scan", rec.seq, SCHEMA_REVISION),
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE queue_record SET nonce = ?, payload = ?", (nonce, blob))
    assert q.claim_batch() == []
    assert q.dead_letters()[0].reason is DeadLetterReason.UNDECODABLE


def test_tampered_dead_letter_detail_reads_as_none(q: EventQueue, db: Path) -> None:
    rec = q.enqueue("scan", {}, T0)
    q.dead_letter(rec.id, DeadLetterReason.REJECTED, "why")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE dead_letter SET detail = zeroblob(length(detail))")
    [dead] = q.dead_letters()
    assert dead.detail is None
    assert dead.payload == {}
