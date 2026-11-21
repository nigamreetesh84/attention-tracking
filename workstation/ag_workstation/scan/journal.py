"""Append-only local scan journal: the write-ahead record and the emergency fallback.

Why it exists (CLAUDE.md rule 1, PLAN 2.6 "the scan is persisted before any
engine or network call"): the server must receive one `scan` item that
already carries `state_at_scan` and `outcome` (contract 4.3; items are
insert-once by id, contract 5.1), but the state is only known after the
engine query, and the encrypted queue has no in-place update. So:

1. Before the engine is asked, the scan is written here as an *intent*: a
   complete, valid `scan` item with `state_at_scan = unknown` and
   `outcome = unmonitored` (what the scan is if the state is never learned),
   and fsynced.
2. After the decision, the final item (same id) goes to the `EventQueue`
   and a *commit* line is appended here.
3. If the queue write fails, the final item is appended here instead (an
   *item* line, fsynced), and stays uncommitted until `recover` moves it to
   the queue. Events use the same path.

`recover` (at service start and from housekeeping) enqueues every
uncommitted entry, the newest version of each id, with the entry's own id;
"already queued" counts as done, so a crash between the queue write and the
commit line cannot duplicate a scan. Once nothing is uncommitted the file is
truncated, so it stays small.

Lines are sealed with the queue's AES-256-GCM cipher when one is given.
Plaintext is the exception, bounded (2.R decision on rule 1 vs. encryption at
rest): rule 1 wins, so a scan is never lost to a crypto failure, but
- a journal opened without a cipher is used only when the data key cannot be
  produced at all (the UI then writes `scan-clear.jnl`, never the sealed
  file), and the next start with a key seals its entries into the queue;
- if sealing fails in a sealed journal, that line is written in the clear and
  `unencrypted_writes` is raised for health;
- a file that holds a clear line (written or loaded) is truncated as soon as
  nothing is pending, whatever its size, so plaintext lives only until its
  record is sealed in the queue.
Clear lines hold ids, barcode, state and times; never images, names or PINs.
Commit lines only hold the record id. A torn last line (crash mid-write) or a
line that does not authenticate is skipped and counted.

`JournalFallbackWriter` gives the other UI-side writers (session records)
the same fallback, and `PeriodicJournalRecovery` drains the journal while the
app runs, skipping entries younger than `min_age_s` so a scan that is still
being decided is not taken in its intent form.
"""

import base64
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, Self
from uuid import UUID

from ag_shared.ids import new_uuid7
from pydantic import BaseModel, JsonValue

from ag_workstation.queue.crypto import PayloadCipher

logger = logging.getLogger(__name__)

JOURNAL_VERSION: Final = 1
JOURNAL_AD: Final = b"ag.scan_journal.v1"
"""Associated data for sealed lines, so a queue ciphertext cannot be replayed here."""
DEFAULT_COMPACT_BYTES: Final = 64 * 1024
DEFAULT_RECOVERY_INTERVAL_S: Final = 30.0
DEFAULT_RECOVERY_MIN_AGE_S: Final = 10.0
"""Far above the scan flow's own bound (150 ms query plus a 5 s capture wait)."""


class ItemQueue(Protocol):
    """The durable queue's write API (`EventQueue.enqueue`)."""

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        """Seal and store one item; raises on failure."""
        ...


class Journal(Protocol):
    """What the scan flow and the fallback writer use (`ScanJournal` or `UnavailableJournal`)."""

    @property
    def backlog(self) -> int:
        """Uncommitted entries."""
        ...

    @property
    def unencrypted_writes(self) -> int:
        """Lines written in the clear since open."""
        ...

    def write_intent(
        self,
        record_id: UUID,
        item_type: str,
        occurred_at: datetime,
        payload: BaseModel | Mapping[str, JsonValue],
    ) -> None:
        """Durably record an item whose final form is not known yet."""
        ...

    def write_item(
        self,
        record_id: UUID,
        item_type: str,
        occurred_at: datetime,
        payload: BaseModel | Mapping[str, JsonValue],
    ) -> None:
        """Durably record a complete item."""
        ...

    def commit(self, record_id: UUID) -> None:
        """Mark an entry as safely in the queue."""
        ...

    def recover(self, queue: "ItemQueue", *, min_age_s: float = 0.0) -> "JournalRecovery":
        """Move uncommitted entries to `queue`."""
        ...


class _Op(StrEnum):
    INTENT = "intent"
    ITEM = "item"
    COMMIT = "commit"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One uncommitted item."""

    id: UUID
    type: str
    occurred_at: datetime
    payload: dict[str, JsonValue]
    final: bool
    """False for a scan intent (state not yet known), True for a complete item."""
    written_mono: float = float("-inf")
    """When this process wrote it (monotonic); entries loaded from disk are old."""


@dataclass(frozen=True, slots=True)
class JournalRecovery:
    """What one `recover` did."""

    enqueued: int
    duplicates: int
    """Already in the queue (a crash after the queue write); now committed."""
    failed: int
    """Still uncommitted (the queue is still failing)."""


def _payload(payload: BaseModel | Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    if isinstance(payload, BaseModel):
        dumped: dict[str, JsonValue] = payload.model_dump(mode="json")
        return dumped
    return dict(payload)


class ScanJournal:
    """The append-only journal file. Thread-safe."""

    def __init__(
        self,
        path: Path,
        cipher: PayloadCipher | None,
        compact_bytes: int,
        fd: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Use `ScanJournal.open`."""
        self._path = path
        self._cipher = cipher
        self._compact_bytes = compact_bytes
        self._fd = fd
        self._monotonic = monotonic
        self._clear_in_file = False
        self._lock = threading.Lock()
        self._pending: dict[UUID, JournalEntry] = {}
        self._corrupt = 0
        self._unencrypted = 0
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path | str,
        cipher: PayloadCipher | None = None,
        *,
        compact_bytes: int = DEFAULT_COMPACT_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Self:
        """Open (creating if needed) the journal and load its uncommitted entries.

        `cipher` None opens a clear journal (no data key; see the module docstring)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        journal = cls(path, cipher, compact_bytes, fd, monotonic)
        try:
            journal._load()
        except BaseException:
            os.close(fd)
            raise
        with journal._lock:
            journal._maybe_compact()  # committed plaintext from a previous run
        return journal

    def close(self) -> None:
        """Close the file. Safe to call twice."""
        with self._lock:
            if not self._closed:
                self._closed = True
                os.close(self._fd)

    # -- state -------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The journal file."""
        return self._path

    @property
    def backlog(self) -> int:
        """Uncommitted entries."""
        with self._lock:
            return len(self._pending)

    @property
    def corrupt_lines(self) -> int:
        """Lines skipped on load (torn, tampered or sealed with another key)."""
        with self._lock:
            return self._corrupt

    @property
    def unencrypted_writes(self) -> int:
        """Lines written in the clear since open."""
        with self._lock:
            return self._unencrypted

    def pending(self) -> list[JournalEntry]:
        """Uncommitted entries, oldest first."""
        with self._lock:
            return list(self._pending.values())

    # -- writing -----------------------------------------------------------

    def write_intent(
        self,
        record_id: UUID,
        item_type: str,
        occurred_at: datetime,
        payload: BaseModel | Mapping[str, JsonValue],
    ) -> None:
        """Durably record an item whose final form is not known yet. Raises `OSError`."""
        self._write(
            JournalEntry(
                record_id, item_type, occurred_at, _payload(payload), False, self._monotonic()
            )
        )

    def write_item(
        self,
        record_id: UUID,
        item_type: str,
        occurred_at: datetime,
        payload: BaseModel | Mapping[str, JsonValue],
    ) -> None:
        """Durably record a complete item the queue could not take. Raises `OSError`."""
        self._write(
            JournalEntry(
                record_id, item_type, occurred_at, _payload(payload), True, self._monotonic()
            )
        )

    def commit(self, record_id: UUID) -> None:
        """Mark an entry as safely in the queue. Not fsynced: a lost commit
        only makes `recover` find the item already queued."""
        line = {"v": JOURNAL_VERSION, "op": _Op.COMMIT.value, "id": str(record_id)}
        with self._lock:
            if self._pending.pop(record_id, None) is None:
                return
            self._append(json.dumps(line, separators=(",", ":")).encode(), sync=False)
            self._maybe_compact()

    def _write(self, entry: JournalEntry) -> None:
        body = {
            "v": JOURNAL_VERSION,
            "op": (_Op.ITEM if entry.final else _Op.INTENT).value,
            "id": str(entry.id),
            "type": entry.type,
            "occurred_at": entry.occurred_at.astimezone(UTC).isoformat(),
            "payload": entry.payload,
        }
        plain = json.dumps(body, separators=(",", ":")).encode()
        with self._lock:
            line = self._seal(plain)
            self._append(line, sync=True)
            current = self._pending.get(entry.id)
            if current is None or entry.final or not current.final:
                self._pending[entry.id] = entry

    def _seal(self, plain: bytes) -> bytes:
        if self._cipher is not None:
            try:
                nonce, sealed = self._cipher.encrypt(plain, JOURNAL_AD)
                return json.dumps(
                    {
                        "v": JOURNAL_VERSION,
                        "n": base64.b64encode(nonce).decode(),
                        "c": base64.b64encode(sealed).decode(),
                    },
                    separators=(",", ":"),
                ).encode()
            except Exception as exc:
                logger.error("scan journal sealing failed (%s); writing clear", type(exc).__name__)
        self._unencrypted += 1
        self._clear_in_file = True
        return plain

    def _append(self, line: bytes, *, sync: bool) -> None:
        if self._closed:
            raise OSError("scan journal is closed")
        data = line + b"\n"
        written = 0
        while written < len(data):
            written += os.write(self._fd, data[written:])
        if sync:
            os.fsync(self._fd)

    def _maybe_compact(self) -> None:
        if self._pending:
            return
        try:
            # Plaintext goes as soon as its records are safe in the queue.
            if self._clear_in_file or os.fstat(self._fd).st_size >= self._compact_bytes:
                os.ftruncate(self._fd, 0)
                os.fsync(self._fd)
                self._clear_in_file = False
        except OSError as exc:
            logger.warning("scan journal compaction failed: %s", type(exc).__name__)

    # -- reading -----------------------------------------------------------

    def _load(self) -> None:
        with self._path.open("rb") as handle:
            for raw in handle:
                if raw.strip():
                    self._load_line(raw)

    def _load_line(self, raw: bytes) -> None:
        try:
            line = json.loads(raw)
            if "c" not in line and line.get("op") != _Op.COMMIT.value:
                self._clear_in_file = True
            if "c" in line:
                if self._cipher is None:
                    raise ValueError("sealed line without a cipher")
                plain = self._cipher.decrypt(
                    base64.b64decode(line["n"]), base64.b64decode(line["c"]), JOURNAL_AD
                )
                line = json.loads(plain)
            op = _Op(line["op"])
            record_id = UUID(line["id"])
            if op is _Op.COMMIT:
                self._pending.pop(record_id, None)
                return
            entry = JournalEntry(
                id=record_id,
                type=str(line["type"]),
                occurred_at=datetime.fromisoformat(line["occurred_at"]),
                payload=dict(line["payload"]),
                final=op is _Op.ITEM,
            )
        except Exception as exc:
            self._corrupt += 1
            logger.warning("scan journal line skipped: %s", type(exc).__name__)
            return
        current = self._pending.get(record_id)
        if current is None or entry.final or not current.final:
            self._pending[record_id] = entry

    # -- recovery ----------------------------------------------------------

    def recover(self, queue: ItemQueue, *, min_age_s: float = 0.0) -> JournalRecovery:
        """Move every uncommitted entry at least `min_age_s` old to `queue`. Never raises.

        `min_age_s` > 0 while scans are being taken (periodic recovery), so the
        intent of a scan still being decided is left to the scan flow."""
        enqueued = duplicates = failed = 0
        cutoff = self._monotonic() - min_age_s
        for entry in self.pending():
            if min_age_s > 0 and entry.written_mono > cutoff:
                continue
            try:
                queue.enqueue(entry.type, entry.payload, entry.occurred_at, record_id=entry.id)
                enqueued += 1
            except ValueError as exc:
                if "already queued" not in str(exc):
                    failed += 1
                    logger.error("journal entry %s not enqueueable: %s", entry.id, exc)
                    continue
                duplicates += 1
            except Exception as exc:
                failed += 1
                logger.warning("journal recovery: queue failed (%s)", type(exc).__name__)
                continue
            try:
                self.commit(entry.id)
            except OSError as exc:
                logger.warning("journal commit failed: %s", type(exc).__name__)
        return JournalRecovery(enqueued, duplicates, failed)


class JournalFallbackWriter:
    """An `ItemQueue` that falls back to the journal when the queue refuses an item.

    Used for the UI's other records (session start and end, login audit), so
    a queue failure cannot lose the session that a scan refers to. Raises
    only if both fail (the original queue error). "Already queued" is not a
    failure to cover: it is raised as is."""

    def __init__(self, queue: ItemQueue, journal: Journal) -> None:
        self._queue = queue
        self._journal = journal

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        """Queue the item, else journal it; returns the queue's answer or the record id."""
        try:
            return self._queue.enqueue(item_type, payload, occurred_at, record_id=record_id)
        except ValueError as exc:
            if "already queued" in str(exc):
                raise
            error: Exception = exc
        except Exception as exc:
            error = exc
        rid = record_id or new_uuid7()
        try:
            self._journal.write_item(rid, item_type, occurred_at, payload)
        except Exception as journal_exc:
            logger.error("journal fallback failed too (%s)", type(journal_exc).__name__)
            raise error from journal_exc
        logger.warning("%s record journaled (queue: %s)", item_type, type(error).__name__)
        return rid


class PeriodicJournalRecovery:
    """Drains the journal into the queue every `interval_s` on a daemon thread."""

    def __init__(
        self,
        journal: Journal,
        queue: ItemQueue,
        *,
        interval_s: float = DEFAULT_RECOVERY_INTERVAL_S,
        min_age_s: float = DEFAULT_RECOVERY_MIN_AGE_S,
    ) -> None:
        self._journal = journal
        self._queue = queue
        self._interval = interval_s
        self._min_age = min_age_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> JournalRecovery | None:
        """One pass; None when there was nothing to do or it failed. Never raises."""
        try:
            if not self._journal.backlog:
                return None
            return self._journal.recover(self._queue, min_age_s=self._min_age)
        except Exception as exc:
            logger.error("journal recovery failed (%s)", type(exc).__name__)
            return None

    def start(self) -> None:
        """Start the thread."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="journal-recovery", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 1.0) -> None:
        """Stop the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.run_once()


class UnavailableQueue:
    """Stands in for a queue that could not be opened: every write fails, so the
    scan flow and `JournalFallbackWriter` put records in the journal."""

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        """Refuse."""
        raise OSError("local queue unavailable")


class UnavailableJournal:
    """Stands in for a journal that could not be opened: every write fails (the
    scan flow then relies on the queue alone and raises its health flag)."""

    backlog = 0
    unencrypted_writes = 0

    def write_intent(
        self,
        record_id: UUID,
        item_type: str,
        occurred_at: datetime,
        payload: BaseModel | Mapping[str, JsonValue],
    ) -> None:
        """Refuse."""
        raise OSError("scan journal unavailable")

    def write_item(
        self,
        record_id: UUID,
        item_type: str,
        occurred_at: datetime,
        payload: BaseModel | Mapping[str, JsonValue],
    ) -> None:
        """Refuse."""
        raise OSError("scan journal unavailable")

    def commit(self, record_id: UUID) -> None:
        """Nothing to commit."""

    def recover(self, queue: ItemQueue, *, min_age_s: float = 0.0) -> JournalRecovery:
        """Nothing to recover."""
        return JournalRecovery(0, 0, 0)
