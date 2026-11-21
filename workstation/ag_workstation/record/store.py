"""The worker's local record: what the "My record" screen reads (PLAN 2.10).

Why a separate local index: the encrypted queue deletes a record as soon as
the server accepts it, and the scan journal is truncated once everything is
queued, so neither can answer "what did I do in my last shifts" offline. This
small SQLite file is fed by `RecordMirror` (a copy of every session, scan and
attention-event item after it was queued) and by `DisputeService`.

Data minimization (scope 7, CLAUDE.md rule 6):

- No barcodes, signals, camera snapshots or image ids are kept; an event
  keeps its kind, state, times, confidence flag and only the *number* of
  images saved. Scans are not kept one by one: each session has counters.
- Rows older than `retention` (default 7 days) are deleted on open and on
  every read; a dispute that has not reached the queue yet is never deleted.
- Everything that identifies a worker is sealed with the queue's AES-256-GCM
  cipher, bound to the table and row id (associated data
  `ag-record/v1|<table>|<id>`), so a row moved to another id or table fails
  authentication. Only opaque ids, times and dispute status are in the clear.

Scoping (PLAN 2.10 "shows only the logged-in worker's data"): every read
takes a `user_id`, opens the session rows in the window and keeps only that
worker's sessions; events are read for those sessions and checked against
their own sealed `user_id` too. Nothing of another worker leaves this module.

Several processes write it (the UI mirrors scans and sessions, the engine
child mirrors episodes); every write runs in `BEGIN IMMEDIATE`.
"""

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from uuid import UUID

from ag_shared.api import AttentionEventKind, AttentionEventPayload, DisputeReasonCode
from ag_shared.states import AttentionState, ScanOutcome
from pydantic import JsonValue

from ag_workstation.clock import Clock
from ag_workstation.queue.crypto import PayloadCipher, PayloadIntegrityError
from ag_workstation.queue.keys import KeyProvider

logger = logging.getLogger(__name__)

DB_VERSION: Final = 1
DEFAULT_RETENTION: Final = timedelta(days=7)
RECORD_FILE: Final = "my_record.db"

_SCHEMA: Final = (
    """
    CREATE TABLE record_session (
        id TEXT PRIMARY KEY,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        nonce BLOB NOT NULL,
        payload BLOB NOT NULL
    )
    """,
    "CREATE INDEX ix_record_session_started ON record_session (started_at)",
    """
    CREATE TABLE record_event (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        occurred_at INTEGER NOT NULL,
        nonce BLOB NOT NULL,
        payload BLOB NOT NULL
    )
    """,
    "CREATE INDEX ix_record_event_session ON record_event (session_id)",
    "CREATE INDEX ix_record_event_occurred ON record_event (occurred_at)",
    """
    CREATE TABLE record_dispute (
        attention_event_id TEXT PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'queued')),
        nonce BLOB NOT NULL,
        payload BLOB NOT NULL
    )
    """,
)


class DisputeStatus(StrEnum):
    """Where a dispute is on this station."""

    PENDING = "pending"
    """Saved here, not yet in the queue (a queue write failed or the app stopped)."""
    QUEUED = "queued"
    """In the queue (or already sent) for the server."""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One session (shift at this station) of one worker, with counters."""

    session_id: UUID
    user_id: UUID
    started_at: datetime
    ended_at: datetime | None
    scans: int
    warned: int
    unmonitored: int
    events: int


@dataclass(frozen=True, slots=True)
class RecordEvent:
    """One attention event of one worker, without images or signals."""

    id: UUID
    session_id: UUID
    user_id: UUID
    kind: AttentionEventKind
    state: AttentionState
    started_at: datetime
    ended_at: datetime | None
    low_confidence: bool
    images_saved: int
    dispute: DisputeStatus | None


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    """Everything the screen shows for one worker."""

    user_id: UUID
    sessions: tuple[SessionSummary, ...]
    """Newest first."""
    events: tuple[RecordEvent, ...]
    """Newest first; only events of `sessions`."""
    unreadable: int = 0
    """Rows skipped because they did not authenticate (other key, tampering)."""


@dataclass(frozen=True, slots=True)
class DisputeRecord:
    """A dispute as saved here."""

    id: UUID
    attention_event_id: UUID
    user_id: UUID
    message: str
    created_at: datetime
    status: DisputeStatus
    reason: DisputeReasonCode | None = None
    """Optional contract reason code (schema revision 2); rows saved before it have none."""


def _reason_code(value: object) -> DisputeReasonCode | None:
    if value is None:
        return None
    try:
        return DisputeReasonCode(str(value))
    except ValueError:
        return DisputeReasonCode.OTHER


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _ad(table: str, row_id: str) -> bytes:
    return "\x1f".join(("ag-record/v1", table, row_id)).encode()


class PersonalRecordStore:
    """Local per-worker counters, events and disputes. Thread-safe."""

    def __init__(
        self, conn: sqlite3.Connection, cipher: PayloadCipher, clock: Clock, retention: timedelta
    ) -> None:
        """Use `PersonalRecordStore.open`."""
        self._conn = conn
        self._cipher = cipher
        self._clock = clock
        self._retention = retention
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path | str,
        keys: KeyProvider,
        clock: Clock,
        *,
        retention: timedelta = DEFAULT_RETENTION,
        busy_timeout_s: float = 2.0,
    ) -> Self:
        """Open (creating if needed) the store at `path` and purge old rows."""
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            path, timeout=busy_timeout_s, isolation_level=None, check_same_thread=False
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_s * 1000)}")
            conn.execute("PRAGMA journal_mode = WAL")
            store = cls(conn, PayloadCipher(keys), clock, retention)
            store._init_schema()
            store.purge()
        except BaseException:
            conn.close()
            raise
        return store

    @property
    def retention(self) -> timedelta:
        """How long rows are kept."""
        return self._retention

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
                raise RuntimeError(f"record database version {version} is newer than this build")
            if version == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {DB_VERSION}")

    # -- sealing --------------------------------------------------------------

    def _seal(self, table: str, row_id: str, data: Mapping[str, JsonValue]) -> tuple[bytes, bytes]:
        plaintext = json.dumps(dict(data), separators=(",", ":")).encode()
        return self._cipher.encrypt(plaintext, _ad(table, row_id))

    def _open(self, table: str, row_id: str, nonce: bytes, sealed: bytes) -> dict[str, JsonValue]:
        """Raises `PayloadIntegrityError` or `ValueError` for a bad row."""
        data = json.loads(self._cipher.decrypt(nonce, sealed, _ad(table, row_id)))
        if not isinstance(data, dict):
            raise ValueError("sealed row is not an object")
        return data

    # -- writing (RecordMirror) --------------------------------------------------

    def note_session_start(self, session_id: UUID, user_id: UUID, started_at: datetime) -> None:
        """A session began. Idempotent: an existing row keeps its counters."""
        sid = str(session_id)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT nonce, payload FROM record_session WHERE id = ?", (sid,)
            ).fetchone()
            if row is not None:
                return
            nonce, sealed = self._seal(
                "session", sid, {"user_id": str(user_id), "scans": 0, "warned": 0, "unmonitored": 0}
            )
            conn.execute(
                "INSERT INTO record_session (id, started_at, ended_at, nonce, payload) "
                "VALUES (?, ?, NULL, ?, ?)",
                (sid, _ms(started_at), nonce, sealed),
            )

    def note_session_end(self, session_id: UUID, ended_at: datetime) -> None:
        """A session ended (no-op for an unknown session)."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE record_session SET ended_at = ? WHERE id = ?",
                (_ms(ended_at), str(session_id)),
            )

    def note_scan(
        self, session_id: UUID, user_id: UUID, scanned_at: datetime, outcome: ScanOutcome
    ) -> None:
        """Count one attributed scan in its session's counters."""
        sid = str(session_id)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT nonce, payload FROM record_session WHERE id = ?", (sid,)
            ).fetchone()
            if row is None:
                # The session start was not mirrored (store failure): start here.
                data: dict[str, JsonValue] = {
                    "user_id": str(user_id),
                    "scans": 0,
                    "warned": 0,
                    "unmonitored": 0,
                }
                started: int | None = _ms(scanned_at)
            else:
                data = self._open("session", sid, row[0], row[1])
                started = None
                if data.get("user_id") != str(user_id):
                    # Never mix two workers' counts; the scan is still in the queue.
                    logger.error("scan of another user in a session; not counted locally")
                    return
            data["scans"] = _int(data.get("scans")) + 1
            if outcome is ScanOutcome.WARNED:
                data["warned"] = _int(data.get("warned")) + 1
            elif outcome is ScanOutcome.UNMONITORED:
                data["unmonitored"] = _int(data.get("unmonitored")) + 1
            nonce, sealed = self._seal("session", sid, data)
            if started is None:
                conn.execute(
                    "UPDATE record_session SET nonce = ?, payload = ? WHERE id = ?",
                    (nonce, sealed, sid),
                )
            else:
                conn.execute(
                    "INSERT INTO record_session (id, started_at, ended_at, nonce, payload) "
                    "VALUES (?, ?, NULL, ?, ?)",
                    (sid, started, nonce, sealed),
                )

    def note_event(self, event_id: UUID, payload: AttentionEventPayload) -> bool:
        """Keep the minimal view of an attributed event; False if it has no worker
        or was already kept."""
        if payload.user_id is None or payload.session_id is None:
            return False
        eid = str(event_id)
        data: dict[str, JsonValue] = {
            "user_id": str(payload.user_id),
            "kind": payload.kind.value,
            "state": payload.state.value,
            "started_at": payload.started_at.isoformat(),
            "ended_at": payload.ended_at.isoformat() if payload.ended_at else None,
            "low_confidence": payload.low_confidence,
            "images_saved": len(payload.image_ids),
        }
        nonce, sealed = self._seal("event", eid, data)
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO record_event (id, session_id, occurred_at, nonce, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (eid, str(payload.session_id), _ms(payload.started_at), nonce, sealed),
            )
            return cursor.rowcount == 1

    # -- reading -------------------------------------------------------------------

    def snapshot(
        self, user_id: UUID, *, max_sessions: int = 20, max_events: int = 500
    ) -> RecordSnapshot:
        """`user_id`'s sessions and events within the retention window, newest first."""
        self.purge()
        cutoff = _ms(self._clock.now() - self._retention)
        owner = str(user_id)
        unreadable = 0
        with self._lock:
            session_rows = self._conn.execute(
                "SELECT id, started_at, ended_at, nonce, payload FROM record_session "
                "WHERE started_at >= ? ORDER BY started_at DESC",
                (cutoff,),
            ).fetchall()
        mine: list[tuple[str, int, int | None, dict[str, JsonValue]]] = []
        for sid, started, ended, nonce, sealed in session_rows:
            try:
                data = self._open("session", sid, nonce, sealed)
            except (PayloadIntegrityError, ValueError):
                unreadable += 1
                continue
            if data.get("user_id") == owner:
                mine.append((sid, started, ended, data))
            if len(mine) >= max_sessions:
                break
        events, bad = self._events(owner, [m[0] for m in mine], max_events)
        unreadable += bad
        per_session: dict[UUID, int] = {}
        for event in events:
            per_session[event.session_id] = per_session.get(event.session_id, 0) + 1
        sessions = tuple(
            SessionSummary(
                session_id=UUID(sid),
                user_id=user_id,
                started_at=_dt(started),
                ended_at=_dt(ended) if ended is not None else None,
                scans=_int(data.get("scans")),
                warned=_int(data.get("warned")),
                unmonitored=_int(data.get("unmonitored")),
                events=per_session.get(UUID(sid), 0),
            )
            for sid, started, ended, data in mine
        )
        if unreadable:
            logger.warning("my record: %d rows did not authenticate and were skipped", unreadable)
        return RecordSnapshot(user_id, sessions, events, unreadable)

    def _events(
        self, owner: str, session_ids: Sequence[str], limit: int
    ) -> tuple[tuple[RecordEvent, ...], int]:
        if not session_ids:
            return (), 0
        with self._lock:
            # json_each keeps the statement constant (no SQL built from values).
            rows = self._conn.execute(
                "SELECT e.id, e.session_id, e.nonce, e.payload, d.status "
                "FROM record_event e LEFT JOIN record_dispute d ON d.attention_event_id = e.id "
                "WHERE e.session_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY e.occurred_at DESC LIMIT ?",
                (json.dumps(list(session_ids)), limit),
            ).fetchall()
        out: list[RecordEvent] = []
        bad = 0
        for eid, sid, nonce, sealed, status in rows:
            try:
                event = self._event(eid, sid, nonce, sealed, status)
            except (PayloadIntegrityError, ValueError, KeyError, TypeError):
                bad += 1
                continue
            if str(event.user_id) == owner:
                out.append(event)
        return tuple(out), bad

    def _event(
        self, eid: str, sid: str, nonce: bytes, sealed: bytes, status: str | None
    ) -> RecordEvent:
        data = self._open("event", eid, nonce, sealed)
        ended = data.get("ended_at")
        return RecordEvent(
            id=UUID(eid),
            session_id=UUID(sid),
            user_id=UUID(str(data["user_id"])),
            kind=AttentionEventKind(str(data["kind"])),
            state=AttentionState(str(data["state"])),
            started_at=datetime.fromisoformat(str(data["started_at"])),
            ended_at=datetime.fromisoformat(ended) if isinstance(ended, str) else None,
            low_confidence=bool(data.get("low_confidence")),
            images_saved=_int(data.get("images_saved")),
            dispute=DisputeStatus(status) if status is not None else None,
        )

    def event_for(self, user_id: UUID, event_id: UUID) -> RecordEvent | None:
        """The event if it is `user_id`'s and still kept here, else None."""
        eid = str(event_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT e.id, e.session_id, e.nonce, e.payload, d.status "
                "FROM record_event e LEFT JOIN record_dispute d ON d.attention_event_id = e.id "
                "WHERE e.id = ?",
                (eid,),
            ).fetchone()
        if row is None:
            return None
        try:
            event = self._event(*row)
        except (PayloadIntegrityError, ValueError, KeyError, TypeError):
            return None
        return event if event.user_id == user_id else None

    # -- disputes (DisputeService) ---------------------------------------------------

    def reserve_dispute(
        self,
        *,
        dispute_id: UUID,
        attention_event_id: UUID,
        user_id: UUID,
        message: str,
        created_at: datetime,
        reason: DisputeReasonCode | None = None,
    ) -> tuple[DisputeRecord, bool]:
        """Save a dispute for the event unless one exists; returns the saved
        dispute (the existing one if there was one) and whether it is new."""
        eid = str(attention_event_id)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT id, created_at, status, nonce, payload FROM record_dispute "
                "WHERE attention_event_id = ?",
                (eid,),
            ).fetchone()
            if existing is not None:
                return self._dispute(eid, *existing), False
            did = str(dispute_id)
            content: dict[str, JsonValue] = {
                "user_id": str(user_id),
                "message": message,
                "event": eid,
            }
            if reason is not None:
                content["reason"] = reason.value
            nonce, sealed = self._seal("dispute", did, content)
            conn.execute(
                "INSERT INTO record_dispute "
                "(attention_event_id, id, created_at, status, nonce, payload) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (eid, did, _ms(created_at), nonce, sealed),
            )
        return (
            DisputeRecord(
                id=dispute_id,
                attention_event_id=attention_event_id,
                user_id=user_id,
                message=message,
                created_at=_dt(_ms(created_at)),
                status=DisputeStatus.PENDING,
                reason=reason,
            ),
            True,
        )

    def _dispute(
        self, eid: str, did: str, created: int, status: str, nonce: bytes, sealed: bytes
    ) -> DisputeRecord:
        data = self._open("dispute", did, nonce, sealed)
        if data.get("event") != eid:
            raise PayloadIntegrityError("dispute row does not match its event")
        return DisputeRecord(
            id=UUID(did),
            attention_event_id=UUID(eid),
            user_id=UUID(str(data["user_id"])),
            message=str(data["message"]),
            created_at=_dt(created),
            status=DisputeStatus(status),
            reason=_reason_code(data.get("reason")),
        )

    def mark_dispute_queued(self, dispute_id: UUID) -> None:
        """The dispute is in the queue."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE record_dispute SET status = 'queued' WHERE id = ?", (str(dispute_id),)
            )

    def pending_disputes(self) -> list[DisputeRecord]:
        """Disputes saved here but not yet queued (oldest first); bad rows are skipped."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT attention_event_id, id, created_at, status, nonce, payload "
                "FROM record_dispute WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        out: list[DisputeRecord] = []
        for row in rows:
            try:
                out.append(self._dispute(*row))
            except (PayloadIntegrityError, ValueError, KeyError):
                logger.error("a saved dispute did not authenticate; skipped")
        return out

    # -- retention -------------------------------------------------------------------

    def purge(self) -> int:
        """Delete rows older than the retention; returns how many were deleted.
        Disputes not yet queued are kept until they are."""
        cutoff = _ms(self._clock.now() - self._retention)
        with self._tx() as conn:
            deleted = conn.execute(
                "DELETE FROM record_session WHERE started_at < ?", (cutoff,)
            ).rowcount
            deleted += conn.execute(
                "DELETE FROM record_event WHERE occurred_at < ?", (cutoff,)
            ).rowcount
            deleted += conn.execute(
                "DELETE FROM record_dispute WHERE status = 'queued' AND created_at < ? "
                "AND attention_event_id NOT IN (SELECT id FROM record_event)",
                (cutoff,),
            ).rowcount
        return deleted


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
