"""Scan journal: write-ahead intents, emergency items, recovery and compaction (PLAN 2.6)."""

import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import EventType, ScanPayload
from ag_shared.ids import Uuid7Generator
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.clock import FakeClock
from ag_workstation.queue import EventQueue, InMemoryKeyProvider, PayloadCipher
from ag_workstation.scan import ScanJournal
from ag_workstation.scan.journal import JournalFallbackWriter, PeriodicJournalRecovery
from pydantic import BaseModel, JsonValue

KEY = bytes(range(32))
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
SENTINEL = "SENTINEL-journal-barcode-91c2"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_utc=T0)


@pytest.fixture
def ids(clock: FakeClock) -> Uuid7Generator:
    return Uuid7Generator(now=clock.now)


@pytest.fixture
def cipher() -> PayloadCipher:
    return PayloadCipher(InMemoryKeyProvider(KEY))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "journal" / "scans.jnl"


@pytest.fixture
def queue(tmp_path: Path, clock: FakeClock) -> Iterator[EventQueue]:
    q = EventQueue.open(tmp_path / "queue.db", InMemoryKeyProvider(KEY), clock)
    yield q
    q.close()


def _scan(state: AttentionState = AttentionState.UNKNOWN) -> ScanPayload:
    outcome = ScanOutcome.WARNED if state is AttentionState.DROWSY else ScanOutcome.UNMONITORED
    return ScanPayload(barcode=SENTINEL, state_at_scan=state, outcome=outcome)


def _queued(queue: EventQueue) -> dict[UUID, dict[str, JsonValue]]:
    return {r.id: r.payload for r in queue.claim_batch()}


def test_intent_is_sealed_and_survives_reopen(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator
) -> None:
    j = ScanJournal.open(path, cipher)
    scan_id = ids()
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())
    j.close()
    j.close()
    assert SENTINEL.encode() not in path.read_bytes()
    reopened = ScanJournal.open(path, cipher)
    assert reopened.backlog == 1
    (entry,) = reopened.pending()
    assert (entry.id, entry.final, entry.occurred_at) == (scan_id, False, T0)
    assert reopened.path == path
    reopened.close()


def test_commit_clears_backlog_and_compacts(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator
) -> None:
    j = ScanJournal.open(path, cipher, compact_bytes=1)
    scan_id = ids()
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())
    assert path.stat().st_size > 0
    j.commit(scan_id)
    j.commit(scan_id)  # unknown id: no-op
    assert j.backlog == 0
    assert path.stat().st_size == 0
    j.close()
    assert ScanJournal.open(path, cipher).backlog == 0


def test_commit_without_compaction_is_replayed_on_reopen(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator
) -> None:
    j = ScanJournal.open(path, cipher)
    a, b = ids(), ids()
    j.write_intent(a, EventType.SCAN, T0, _scan())
    j.write_intent(b, EventType.SCAN, T0, _scan())
    j.commit(a)
    j.close()
    assert [e.id for e in ScanJournal.open(path, cipher).pending()] == [b]


def test_item_supersedes_intent_and_not_vice_versa(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator
) -> None:
    j = ScanJournal.open(path, cipher)
    scan_id = ids()
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())
    j.write_item(scan_id, EventType.SCAN, T0, _scan(AttentionState.DROWSY))
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())
    (entry,) = j.pending()
    assert entry.final and entry.payload["state_at_scan"] == "drowsy"
    j.close()
    (entry,) = ScanJournal.open(path, cipher).pending()
    assert entry.final and entry.payload["state_at_scan"] == "drowsy"


def test_recover_moves_entries_and_skips_already_queued(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator, queue: EventQueue
) -> None:
    j = ScanJournal.open(path, cipher)
    intent_only, final, already = ids(), ids(), ids()
    j.write_intent(intent_only, EventType.SCAN, T0, _scan())
    j.write_item(final, EventType.SCAN, T0, _scan(AttentionState.DROWSY))
    j.write_intent(already, EventType.SCAN, T0, _scan())
    queue.enqueue(EventType.SCAN, _scan(AttentionState.DROWSY), T0, record_id=already)
    result = j.recover(queue)
    assert (result.enqueued, result.duplicates, result.failed) == (2, 1, 0)
    assert j.backlog == 0
    items = _queued(queue)
    assert items[intent_only]["state_at_scan"] == "unknown"
    assert items[intent_only]["outcome"] == "unmonitored"
    assert items[final]["state_at_scan"] == "drowsy"
    assert items[already]["state_at_scan"] == "drowsy"
    assert j.recover(queue).enqueued == 0


class _BrokenQueue:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        raise self.error


@pytest.mark.parametrize("error", [OSError("disk"), ValueError("record_id must be a UUIDv7")])
def test_recover_keeps_entries_when_queue_fails(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator, error: Exception
) -> None:
    j = ScanJournal.open(path, cipher)
    j.write_intent(ids(), EventType.SCAN, T0, _scan())
    result = j.recover(_BrokenQueue(error))
    assert (result.enqueued, result.failed) == (0, 1)
    assert j.backlog == 1


def test_torn_and_foreign_lines_are_skipped(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator
) -> None:
    j = ScanJournal.open(path, cipher)
    good = ids()
    j.write_intent(good, EventType.SCAN, T0, _scan())
    j.close()
    other = PayloadCipher(InMemoryKeyProvider(bytes(32)))
    k = ScanJournal.open(path, other)
    k.write_intent(ids(), EventType.SCAN, T0, _scan())
    k.close()
    with path.open("ab") as handle:
        handle.write(b'{"v":1,"op":"inte')  # crash mid-write
    reopened = ScanJournal.open(path, cipher)
    assert [e.id for e in reopened.pending()] == [good]
    assert reopened.corrupt_lines == 2


def test_no_cipher_writes_clear_and_cannot_read_sealed(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator
) -> None:
    j = ScanJournal.open(path)
    j.write_intent(ids(), EventType.SCAN, T0, {"barcode": SENTINEL})
    assert j.unencrypted_writes == 1
    assert SENTINEL.encode() in path.read_bytes()
    j.close()
    sealed = ScanJournal.open(path, cipher)
    sealed.write_intent(ids(), EventType.SCAN, T0, _scan())
    sealed.close()
    clear = ScanJournal.open(path)
    assert (clear.backlog, clear.corrupt_lines) == (1, 1)


class _BrokenCipher(PayloadCipher):
    def encrypt(self, plaintext: bytes, associated_data: bytes) -> tuple[bytes, bytes]:
        raise RuntimeError("key gone")


def test_sealing_failure_still_records_in_clear(path: Path, ids: Uuid7Generator) -> None:
    j = ScanJournal.open(path, _BrokenCipher(InMemoryKeyProvider(KEY)))
    j.write_intent(ids(), EventType.SCAN, T0, _scan())
    assert (j.backlog, j.unencrypted_writes) == (1, 1)


def test_write_after_close_raises(path: Path, ids: Uuid7Generator) -> None:
    j = ScanJournal.open(path)
    j.close()
    with pytest.raises(OSError):
        j.write_item(ids(), EventType.SCAN, T0, _scan())


def test_open_failure_closes_file(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: ScanJournal) -> None:
        raise RuntimeError("load failed")

    monkeypatch.setattr(ScanJournal, "_load", boom)
    with pytest.raises(RuntimeError):
        ScanJournal.open(path)


def test_compaction_failure_is_harmless(
    path: Path, ids: Uuid7Generator, monkeypatch: pytest.MonkeyPatch
) -> None:
    j = ScanJournal.open(path, compact_bytes=1)
    scan_id = ids()
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())

    def no_truncate(fd: int, length: int) -> None:
        raise OSError("busy")

    monkeypatch.setattr("os.ftruncate", no_truncate)
    j.commit(scan_id)
    assert j.backlog == 0


# -- 2.R: plaintext is bounded, recovery can run while scanning ---------------------


def test_clear_line_is_erased_as_soon_as_nothing_is_pending(
    path: Path, ids: Uuid7Generator
) -> None:
    """2.R M2: a line written in the clear must not outlive its commit (old code
    kept it until the file reached `compact_bytes`, 64 KiB by default)."""
    j = ScanJournal.open(path, _BrokenCipher(InMemoryKeyProvider(KEY)))
    scan_id = ids()
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())
    assert SENTINEL.encode() in path.read_bytes()
    j.commit(scan_id)
    assert path.stat().st_size == 0
    j.close()


def test_clear_journal_is_erased_after_recovery_on_the_next_start(
    path: Path, ids: Uuid7Generator, queue: EventQueue
) -> None:
    """2.R C1/M2: the no-key journal is drained into the sealed queue at the next
    start and then emptied, whatever its size."""
    clear = ScanJournal.open(path)
    scan_id = ids()
    clear.write_item(scan_id, EventType.SCAN, T0, _scan(AttentionState.DROWSY))
    clear.close()
    reopened = ScanJournal.open(path)
    assert reopened.recover(queue).enqueued == 1
    assert path.stat().st_size == 0
    assert _queued(queue)[scan_id]["state_at_scan"] == "drowsy"
    reopened.close()


def test_sealed_journal_keeps_the_size_rule(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator
) -> None:
    j = ScanJournal.open(path, cipher)
    scan_id = ids()
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())
    j.commit(scan_id)
    assert path.stat().st_size > 0  # sealed lines: compacted at compact_bytes only
    assert j.unencrypted_writes == 0
    j.close()


def test_recover_skips_entries_younger_than_min_age(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator, queue: EventQueue
) -> None:
    """2.R M1: periodic recovery must not take an intent whose scan is still
    being decided (its final item would then be refused as already queued)."""
    now = [100.0]
    j = ScanJournal.open(path, cipher, monotonic=lambda: now[0])
    scan_id = ids()
    j.write_intent(scan_id, EventType.SCAN, T0, _scan())
    now[0] += 1.0
    assert j.recover(queue, min_age_s=5.0).enqueued == 0
    assert j.backlog == 1
    now[0] += 5.0
    assert j.recover(queue, min_age_s=5.0).enqueued == 1
    assert j.backlog == 0
    j.close()


def test_entries_loaded_from_disk_are_old_enough(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator, queue: EventQueue
) -> None:
    j = ScanJournal.open(path, cipher)
    j.write_intent(ids(), EventType.SCAN, T0, _scan())
    j.close()
    reopened = ScanJournal.open(path, cipher, monotonic=lambda: 0.0)
    assert reopened.recover(queue, min_age_s=3600.0).enqueued == 1
    reopened.close()


def test_fallback_writer_journals_what_the_queue_refuses(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator, queue: EventQueue
) -> None:
    """2.R H6: session records get the scans' journal fallback, so a queue
    failure cannot orphan the scans of a session on the server."""
    j = ScanJournal.open(path, cipher)
    broken = JournalFallbackWriter(_BrokenQueue(OSError("db locked")), j)
    rid = ids()
    broken.enqueue(EventType.SESSION_START, {"user_id": None}, T0, record_id=rid)
    no_id = broken.enqueue(EventType.SESSION_END, {"x": 1}, T0)
    assert isinstance(no_id, UUID)
    assert {e.id for e in j.pending()} == {rid, no_id}
    assert all(e.final for e in j.pending())
    assert j.recover(queue).enqueued == 2
    assert set(_queued(queue)) == {rid, no_id}
    j.close()


def test_fallback_writer_passes_through_and_does_not_journal_duplicates(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator, queue: EventQueue
) -> None:
    j = ScanJournal.open(path, cipher)
    writer = JournalFallbackWriter(queue, j)
    rid = ids()
    writer.enqueue(EventType.SCAN, _scan(), T0, record_id=rid)
    with pytest.raises(ValueError, match="already queued"):
        writer.enqueue(EventType.SCAN, _scan(), T0, record_id=rid)
    assert j.backlog == 0
    j.close()


def test_fallback_writer_raises_when_the_journal_fails_too(path: Path, ids: Uuid7Generator) -> None:
    j = ScanJournal.open(path)
    j.close()
    writer = JournalFallbackWriter(_BrokenQueue(OSError("db locked")), j)
    with pytest.raises(OSError, match="db locked"):
        writer.enqueue(EventType.SESSION_START, {}, T0, record_id=ids())


def test_periodic_recovery_drains_the_journal(
    path: Path, cipher: PayloadCipher, ids: Uuid7Generator, queue: EventQueue
) -> None:
    """2.R M1: the journal is drained while the app runs, not only at start."""
    now = [0.0]
    j = ScanJournal.open(path, cipher, monotonic=lambda: now[0])
    j.write_item(ids(), EventType.SCAN, T0, _scan(AttentionState.DROWSY))
    loop = PeriodicJournalRecovery(j, queue, interval_s=0.01, min_age_s=2.0)
    loop.run_once()  # too young to take
    assert j.backlog == 1
    now[0] = 10.0
    loop.start()
    try:
        end = time.monotonic() + 2.0
        while j.backlog and time.monotonic() < end:
            time.sleep(0.01)
    finally:
        loop.close()
    assert j.backlog == 0
    assert len(_queued(queue)) == 1
    j.close()


def test_periodic_recovery_survives_errors(path: Path, cipher: PayloadCipher) -> None:
    class Exploding:
        backlog = 1

        def recover(self, queue: object, *, min_age_s: float = 0.0) -> object:
            raise RuntimeError("boom")

    loop = PeriodicJournalRecovery(Exploding(), _BrokenQueue(OSError()), interval_s=0.01)  # type: ignore[arg-type]  # duck-typed journal
    assert loop.run_once() is None
