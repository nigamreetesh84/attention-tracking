"""The encrypted local event queue (`api-contract.md` section 6).

SQLite in WAL mode; one row per batch item. Only routing metadata (id, seq,
type, schema, timestamps, status, attempts) is stored in the clear; the item's
`occurred_at` and payload are sealed with AES-256-GCM, bound to the row's id,
type, seq and schema. Server free-text reasons for dead letters are sealed too,
since they may echo payload values.

Several processes may open the same file (the service sends; the UI may
enqueue). Every write runs in `BEGIN IMMEDIATE`, so `seq` allocation and
claims are serialized; waiting writers block up to the busy timeout.

Evidence images are not in this queue; they are separate encrypted files
(task 2.2) uploaded after their event, so "metadata before images" holds by
construction.
"""

import json
import logging
import random
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from uuid import UUID

from ag_shared.api import MAX_BATCH_BYTES, MAX_BATCH_ITEMS, SCHEMA_REVISION, ItemResult, ItemStatus
from ag_shared.ids import Uuid7Generator, is_uuid7
from pydantic import BaseModel, JsonValue

from ag_workstation.clock import Clock
from ag_workstation.queue.backoff import MAX_BACKOFF_S, backoff_delay
from ag_workstation.queue.crypto import PayloadCipher, PayloadIntegrityError
from ag_workstation.queue.keys import KeyProvider

logger = logging.getLogger(__name__)

DB_VERSION: Final = 1
DEAD_LETTER_RETENTION: Final = timedelta(days=30)
MAX_DETAIL_CHARS: Final = 500
BATCH_ENVELOPE_RESERVE: Final = 4096
"""Bytes kept free in a batch for the envelope (`batch_id`, `sent_at`, brackets)."""

_SCHEMA = (
    """
    CREATE TABLE queue_meta (
        k TEXT PRIMARY KEY,
        v INTEGER NOT NULL
    )
    """,
    "INSERT INTO queue_meta (k, v) VALUES ('next_seq', 1)",
    """
    CREATE TABLE queue_record (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL UNIQUE,
        type TEXT NOT NULL,
        schema INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'dead')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL,
        nonce BLOB NOT NULL,
        payload BLOB NOT NULL
    )
    """,
    "CREATE INDEX ix_queue_record_status_seq ON queue_record (status, seq)",
    """
    CREATE TABLE dead_letter (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        schema INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        attempts INTEGER NOT NULL,
        dead_at INTEGER NOT NULL,
        reason TEXT NOT NULL,
        detail_nonce BLOB,
        detail BLOB,
        nonce BLOB NOT NULL,
        payload BLOB NOT NULL
    )
    """,
    "CREATE INDEX ix_dead_letter_dead_at ON dead_letter (dead_at)",
)


class DeadLetterReason(StrEnum):
    """Why a record left the queue without being accepted."""

    REJECTED = "rejected"
    """The server rejected it permanently (validation)."""
    INTEGRITY = "integrity"
    """The sealed payload failed authentication (tampered, swapped, wrong key)."""
    UNDECODABLE = "undecodable"
    """It authenticated but is not a valid sealed item (a bug, not an attack)."""


@dataclass(frozen=True, slots=True)
class QueuedRecord:
    """What `enqueue` assigned."""

    id: UUID
    seq: int


@dataclass(frozen=True, slots=True)
class ClaimedRecord:
    """A decrypted record claimed for sending."""

    id: UUID
    seq: int
    type: str
    schema: int
    created_at: datetime
    attempts: int
    occurred_at: datetime
    payload: dict[str, JsonValue]

    def to_item(self) -> dict[str, JsonValue]:
        """The batch item in wire form (`api-contract.md` 4.3)."""
        return {
            "type": self.type,
            "id": str(self.id),
            "seq": self.seq,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload,
        }

    def wire_size(self) -> int:
        """Bytes of the item's compact JSON."""
        return len(json.dumps(self.to_item(), separators=(",", ":")).encode())


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A dead-lettered record. `payload`/`detail` are None if they do not decrypt."""

    id: UUID
    seq: int
    type: str
    created_at: datetime
    dead_at: datetime
    attempts: int
    reason: DeadLetterReason
    detail: str | None
    payload: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class QueueStats:
    """Numbers for the heartbeat and health sample."""

    depth: int
    """Records not yet accepted (pending + sending)."""
    sending: int
    oldest_unsent_at: datetime | None
    dead_letter_count: int


@dataclass(frozen=True, slots=True)
class ApplySummary:
    """Outcome of `apply_results`."""

    done: int
    dead: int
    retry: int


def record_associated_data(record_id: UUID, record_type: str, seq: int, schema: int) -> bytes:
    """The AES-GCM associated data that binds a sealed payload to its row."""
    return "\x1f".join(("ag-queue/v1", str(record_id), record_type, str(seq), str(schema))).encode()


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class EventQueue:
    """The workstation's durable, encrypted event queue. Thread-safe."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        cipher: PayloadCipher,
        clock: Clock,
        rng: Callable[[], float],
    ) -> None:
        """Use `EventQueue.open`."""
        self._conn = conn
        self._cipher = cipher
        self._clock = clock
        self._rng = rng
        self._ids = Uuid7Generator(now=clock.now)
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path | str,
        keys: KeyProvider,
        clock: Clock,
        *,
        rng: Callable[[], float] | None = None,
        recover_sending: bool = True,
        busy_timeout_s: float = 5.0,
    ) -> Self:
        """Open (creating if needed) the queue at `path`.

        `recover_sending=True` returns records left `sending` by a crashed
        sender to `pending`; only the sending process should do this, so a
        process that only enqueues passes False.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            path, timeout=busy_timeout_s, isolation_level=None, check_same_thread=False
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_s * 1000)}")
            conn.execute("PRAGMA journal_mode = WAL")
            # FULL: a committed scan survives power loss, not only a process crash.
            conn.execute("PRAGMA synchronous = FULL")
            queue = cls(conn, PayloadCipher(keys), clock, rng or random.SystemRandom().random)
            queue._init_schema()
            if recover_sending:
                queue.recover_sending()
        except BaseException:
            conn.close()
            raise
        return queue

    def close(self) -> None:
        """Close the connection. Safe to call twice."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _init_schema(self) -> None:
        with self._tx() as conn:
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            if version > DB_VERSION:
                raise RuntimeError(
                    f"queue database version {version} is newer than this build ({DB_VERSION})"
                )
            if version == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {DB_VERSION}")

    def _now_ms(self) -> int:
        return _ms(self._clock.now())

    # -- writing ------------------------------------------------------------

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> QueuedRecord:
        """Seal and store one item; returns its id and allocated `seq`.

        `item_type` is an `EventType` value. `record_id` is the item's
        UUIDv7 when the caller already has one (e.g. the session id).
        """
        if occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if record_id is None:
            record_id = self._ids()
        elif not is_uuid7(record_id):
            raise ValueError("record_id must be a UUIDv7")
        body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        plaintext = json.dumps(
            {"occurred_at": occurred_at.astimezone(UTC).isoformat(), "payload": body},
            separators=(",", ":"),
        ).encode()
        record_type = str(item_type)
        with self._tx() as conn:
            (seq,) = conn.execute("SELECT v FROM queue_meta WHERE k = 'next_seq'").fetchone()
            conn.execute("UPDATE queue_meta SET v = v + 1 WHERE k = 'next_seq'")
            nonce, sealed = self._cipher.encrypt(
                plaintext, record_associated_data(record_id, record_type, seq, SCHEMA_REVISION)
            )
            now = self._now_ms()
            try:
                conn.execute(
                    "INSERT INTO queue_record (id, seq, type, schema, created_at, status, "
                    "attempts, next_attempt_at, nonce, payload) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                    (str(record_id), seq, record_type, SCHEMA_REVISION, now, now, nonce, sealed),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"record {record_id} is already queued") from exc
        return QueuedRecord(id=record_id, seq=seq)

    # -- sending ------------------------------------------------------------

    def claim_batch(
        self,
        max_items: int = MAX_BATCH_ITEMS,
        max_bytes: int = MAX_BATCH_BYTES - BATCH_ENVELOPE_RESERVE,
    ) -> list[ClaimedRecord]:
        """Mark the oldest due records `sending` and return them decrypted, by `seq`.

        Stops at `max_items` or when the next item would pass `max_bytes`
        (at least one item is returned if any is due). A record that fails
        its integrity check is dead-lettered and skipped, never raised.
        """
        if not 1 <= max_items <= MAX_BATCH_ITEMS:
            raise ValueError(f"max_items must be 1..{MAX_BATCH_ITEMS}")
        claimed: list[ClaimedRecord] = []
        size = 0
        with self._tx() as conn:
            now = self._now_ms()
            # A due time beyond the longest backoff can only come from a wall
            # clock that was stepped back; treat such records as due.
            horizon = now + int(MAX_BACKOFF_S * 1000)
            rows = conn.execute(
                "SELECT id, seq, type, schema, created_at, attempts, nonce, payload "
                "FROM queue_record WHERE status = 'pending' "
                "AND (next_attempt_at <= ? OR next_attempt_at > ?) ORDER BY seq",
                (now, horizon),
            )
            for rid, seq, rtype, schema, created, attempts, nonce, sealed in rows.fetchall():
                try:
                    occurred_at, payload = self._open(UUID(rid), rtype, seq, schema, nonce, sealed)
                except PayloadIntegrityError:
                    logger.error("queue record %s (seq %d) failed its integrity check", rid, seq)
                    self._move_to_dead(conn, rid, DeadLetterReason.INTEGRITY, None, now)
                    continue
                except ValueError:
                    logger.error("queue record %s (seq %d) is not a valid item", rid, seq)
                    self._move_to_dead(conn, rid, DeadLetterReason.UNDECODABLE, None, now)
                    continue
                record = ClaimedRecord(
                    id=UUID(rid),
                    seq=seq,
                    type=rtype,
                    schema=schema,
                    created_at=_dt(created),
                    attempts=attempts + 1,
                    occurred_at=occurred_at,
                    payload=payload,
                )
                item_size = record.wire_size()
                if claimed and size + item_size > max_bytes:
                    break
                claimed.append(record)
                size += item_size
                if len(claimed) == max_items:
                    break
            conn.executemany(
                "UPDATE queue_record SET status = 'sending', attempts = attempts + 1 WHERE id = ?",
                [(str(r.id),) for r in claimed],
            )
        return claimed

    def _open(
        self, rid: UUID, rtype: str, seq: int, schema: int, nonce: bytes, sealed: bytes
    ) -> tuple[datetime, dict[str, JsonValue]]:
        try:
            plaintext = self._cipher.decrypt(
                nonce, sealed, record_associated_data(rid, rtype, seq, schema)
            )
        except PayloadIntegrityError as exc:
            raise PayloadIntegrityError(
                f"queue record {rid} (seq {seq}) failed its integrity check"
            ) from exc
        try:
            body = json.loads(plaintext)
            occurred_at = datetime.fromisoformat(body["occurred_at"])
            payload = body["payload"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"queue record {rid} is not a valid sealed item") from exc
        if not isinstance(payload, dict) or occurred_at.utcoffset() is None:
            raise ValueError(f"queue record {rid} is not a valid sealed item")
        return occurred_at, payload

    def mark_done(self, ids: Iterable[UUID]) -> int:
        """Delete records the server accepted (or already had). Returns the count."""
        with self._tx() as conn:
            cur = conn.executemany(
                "DELETE FROM queue_record WHERE id = ?", [(str(i),) for i in ids]
            )
            return cur.rowcount

    def retry(self, ids: Iterable[UUID]) -> int:
        """Return `sending` records to `pending` after their backoff.

        Used for `retry_later` items, items missing from a response, and a
        batch the send loop gives up on after a transport failure.
        """
        count = 0
        with self._tx() as conn:
            now = self._clock.now()
            for rid in ids:
                row = conn.execute(
                    "SELECT attempts FROM queue_record WHERE id = ? AND status = 'sending'",
                    (str(rid),),
                ).fetchone()
                if row is None:
                    continue
                due = now + timedelta(seconds=backoff_delay(row[0], self._rng))
                conn.execute(
                    "UPDATE queue_record SET status = 'pending', next_attempt_at = ? WHERE id = ?",
                    (_ms(due), str(rid)),
                )
                count += 1
        return count

    def dead_letter(
        self, record_id: UUID, reason: DeadLetterReason, detail: str | None = None
    ) -> bool:
        """Move a record to the dead-letter table. False if it is not queued."""
        with self._tx() as conn:
            return self._move_to_dead(conn, str(record_id), reason, detail, self._now_ms())

    def _move_to_dead(
        self,
        conn: sqlite3.Connection,
        rid: str,
        reason: DeadLetterReason,
        detail: str | None,
        now: int,
    ) -> bool:
        row = conn.execute(
            "SELECT seq, type, schema FROM queue_record WHERE id = ?", (rid,)
        ).fetchone()
        if row is None:
            return False
        seq, rtype, schema = row
        detail_nonce: bytes | None = None
        sealed_detail: bytes | None = None
        if detail:
            detail_nonce, sealed_detail = self._cipher.encrypt(
                detail[:MAX_DETAIL_CHARS].encode(),
                record_associated_data(UUID(rid), rtype, seq, schema) + b"\x1fdetail",
            )
        conn.execute(
            "INSERT OR REPLACE INTO dead_letter (id, seq, type, schema, created_at, attempts, "
            "dead_at, reason, detail_nonce, detail, nonce, payload) "
            "SELECT id, seq, type, schema, created_at, attempts, ?, ?, ?, ?, nonce, payload "
            "FROM queue_record WHERE id = ?",
            (now, reason.value, detail_nonce, sealed_detail, rid),
        )
        conn.execute("DELETE FROM queue_record WHERE id = ?", (rid,))
        logger.warning("queue record %s (seq %d, %s) dead-lettered: %s", rid, seq, rtype, reason)
        return True

    def apply_results(
        self, claimed: Sequence[ClaimedRecord], results: Iterable[ItemResult]
    ) -> ApplySummary:
        """Apply a `BatchResult`'s per-item results to the records that were sent.

        accepted/duplicate: deleted. rejected: dead-lettered. retry_later, or
        no result for an item: retried with backoff. Results for ids that were
        not in `claimed` are ignored.
        """
        sent = {r.id for r in claimed}
        by_id = {r.id: r for r in results if r.id in sent}
        done = [i for i, r in by_id.items() if r.status.deletes_record]
        dead = [r for r in by_id.values() if r.status is ItemStatus.REJECTED]
        retry = [
            r.id
            for r in claimed
            if r.id not in by_id or by_id[r.id].status is ItemStatus.RETRY_LATER
        ]
        self.mark_done(done)
        for result in dead:
            self.dead_letter(result.id, DeadLetterReason.REJECTED, result.reason)
        self.retry(retry)
        return ApplySummary(done=len(done), dead=len(dead), retry=len(retry))

    def recover_sending(self) -> int:
        """Return every `sending` record to `pending`, due now (after a crash)."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE queue_record SET status = 'pending', next_attempt_at = ? "
                "WHERE status = 'sending'",
                (self._now_ms(),),
            )
            if cur.rowcount:
                logger.info("returned %d in-flight queue records to pending", cur.rowcount)
            return cur.rowcount

    # -- health and housekeeping -------------------------------------------

    def stats(self) -> QueueStats:
        """Queue depth, in-flight count, oldest unsent record and dead-letter count."""
        with self._lock:
            depth, sending, oldest = self._conn.execute(
                "SELECT count(*), coalesce(sum(status = 'sending'), 0), min(created_at) "
                "FROM queue_record"
            ).fetchone()
            (dead,) = self._conn.execute("SELECT count(*) FROM dead_letter").fetchone()
        return QueueStats(
            depth=depth,
            sending=sending,
            oldest_unsent_at=None if oldest is None else _dt(oldest),
            dead_letter_count=dead,
        )

    def next_attempt_at(self, record_id: UUID) -> datetime | None:
        """When a queued record is next due, or None if it is not queued."""
        with self._lock:
            row = self._conn.execute(
                "SELECT next_attempt_at FROM queue_record WHERE id = ?", (str(record_id),)
            ).fetchone()
        return None if row is None else _dt(row[0])

    def dead_letters(self) -> list[DeadLetter]:
        """All dead letters, oldest first, decrypted where possible (diagnostics)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, seq, type, schema, created_at, attempts, dead_at, reason, "
                "detail_nonce, detail, nonce, payload FROM dead_letter ORDER BY dead_at, seq"
            ).fetchall()
        letters = []
        for (
            rid,
            seq,
            rtype,
            schema,
            created,
            attempts,
            dead_at,
            reason,
            dn,
            dc,
            nonce,
            sealed,
        ) in rows:
            uid = UUID(rid)
            payload: dict[str, JsonValue] | None
            try:
                _, payload = self._open(uid, rtype, seq, schema, nonce, sealed)
            except (PayloadIntegrityError, ValueError):
                payload = None
            detail: str | None = None
            if dn is not None and dc is not None:
                try:
                    ad = record_associated_data(uid, rtype, seq, schema) + b"\x1fdetail"
                    detail = self._cipher.decrypt(dn, dc, ad).decode()
                except PayloadIntegrityError:
                    detail = None
            letters.append(
                DeadLetter(
                    id=uid,
                    seq=seq,
                    type=rtype,
                    created_at=_dt(created),
                    dead_at=_dt(dead_at),
                    attempts=attempts,
                    reason=DeadLetterReason(reason),
                    detail=detail,
                    payload=payload,
                )
            )
        return letters

    def read_dead_letter_payload(self, record_id: UUID) -> dict[str, JsonValue]:
        """Decrypt one dead letter's payload; raises `PayloadIntegrityError` if tampered."""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, type, schema, nonce, payload FROM dead_letter WHERE id = ?",
                (str(record_id),),
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        seq, rtype, schema, nonce, sealed = row
        return self._open(record_id, rtype, seq, schema, nonce, sealed)[1]

    def purge_dead_letters(self, retention: timedelta = DEAD_LETTER_RETENTION) -> int:
        """Delete dead letters older than `retention` (30 days by default)."""
        cutoff = _ms(self._clock.now() - retention)
        with self._tx() as conn:
            return conn.execute("DELETE FROM dead_letter WHERE dead_at < ?", (cutoff,)).rowcount

    def checkpoint(self) -> None:
        """Fold the WAL into the main file and truncate it."""
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
