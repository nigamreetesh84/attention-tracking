"""Scan journal: write-ahead intents, emergency items, recovery and compaction (PLAN 2.6)."""

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
