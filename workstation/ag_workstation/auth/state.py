"""Small persistent login state: lockouts, handled commands, the open session.

A separate SQLite file next to the queue. It holds no PINs, hashes or other
secrets: lockout rows are keyed by the case-folded employee ID that was
typed, command rows by command or assignment id, and the open-session row by
session and user id. Persisting it means a restart neither resets a lockout
nor re-runs an already handled `assign_worker` command, and a session left
open by a crash is closed on the next start.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from uuid import UUID

DB_VERSION: Final = 1

_SCHEMA = (
    """
    CREATE TABLE login_lockout (
        subject TEXT PRIMARY KEY,
        failures INTEGER NOT NULL,
        first_failure_at INTEGER NOT NULL,
        locked_until INTEGER
    )
    """,
    """
    CREATE TABLE handled_key (
        key TEXT PRIMARY KEY,
        handled_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE open_session (
        only_row INTEGER PRIMARY KEY CHECK (only_row = 1),
        session_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        started_at INTEGER NOT NULL
    )
    """,
)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass(frozen=True, slots=True)
class LockoutRow:
    """Failure count and lock for one subject."""

    failures: int
    first_failure_at: datetime
    locked_until: datetime | None


@dataclass(frozen=True, slots=True)
class OpenSessionRow:
    """The session that was open when the row was written."""

    session_id: UUID
    user_id: UUID
    started_at: datetime


class AuthStateStore:
    """The login state database. Thread-safe."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Use `AuthStateStore.open` (or `in_memory` in tests)."""
        self._conn = conn
        self._lock = threading.RLock()
        self._closed = False
        self._init_schema()

    @classmethod
    def open(cls, path: Path | str, *, busy_timeout_s: float = 5.0) -> Self:
        """Open (creating if needed) the database at `path`."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            path, timeout=busy_timeout_s, isolation_level=None, check_same_thread=False
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_s * 1000)}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            return cls(conn)
        except BaseException:
            conn.close()
            raise

    @classmethod
    def in_memory(cls) -> Self:
        """A non-persistent store."""
        return cls(sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False))

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
                    f"login state database version {version} is newer than this build"
                )
            if version == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {DB_VERSION}")

    # -- lockouts -----------------------------------------------------------

    def get_lockout(self, subject: str) -> LockoutRow | None:
        """The lockout row for `subject`, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT failures, first_failure_at, locked_until FROM login_lockout "
                "WHERE subject = ?",
                (subject,),
            ).fetchone()
        if row is None:
            return None
        failures, first, locked = row
        return LockoutRow(failures, _dt(first), None if locked is None else _dt(locked))

    def put_lockout(self, subject: str, row: LockoutRow) -> None:
        """Insert or replace the row for `subject`."""
        locked = None if row.locked_until is None else _ms(row.locked_until)
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO login_lockout "
                "(subject, failures, first_failure_at, locked_until) VALUES (?, ?, ?, ?)",
                (subject, row.failures, _ms(row.first_failure_at), locked),
            )

    def clear_lockout(self, subject: str) -> None:
        """Forget `subject`'s failures and lock."""
        with self._tx() as conn:
            conn.execute("DELETE FROM login_lockout WHERE subject = ?", (subject,))

    def purge_lockouts(self, window_started_before: datetime, now: datetime) -> int:
        """Drop unlocked rows whose window started before `window_started_before`
        and rows whose lock ended by `now`."""
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM login_lockout WHERE (locked_until IS NULL AND first_failure_at < ?) "
                "OR locked_until <= ?",
                (_ms(window_started_before), _ms(now)),
            )
            return cur.rowcount

    # -- handled commands ---------------------------------------------------

    def mark_handled(self, key: str, at: datetime) -> bool:
        """Record `key`; False if it was already recorded."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO handled_key (key, handled_at) VALUES (?, ?)",
                (key, _ms(at)),
            )
            return cur.rowcount == 1

    def is_handled(self, key: str) -> bool:
        """Whether `key` was recorded."""
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM handled_key WHERE key = ?", (key,)).fetchone()
        return row is not None

    def purge_handled(self, older_than: datetime) -> int:
        """Drop keys recorded before `older_than`."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM handled_key WHERE handled_at < ?", (_ms(older_than),))
            return cur.rowcount

    # -- open session -------------------------------------------------------

    def set_open_session(self, row: OpenSessionRow | None) -> None:
        """Record the open session, or clear it."""
        with self._tx() as conn:
            conn.execute("DELETE FROM open_session")
            if row is not None:
                conn.execute(
                    "INSERT INTO open_session (only_row, session_id, user_id, started_at) "
                    "VALUES (1, ?, ?, ?)",
                    (str(row.session_id), str(row.user_id), _ms(row.started_at)),
                )

    def get_open_session(self) -> OpenSessionRow | None:
        """The recorded open session, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, user_id, started_at FROM open_session"
            ).fetchone()
        if row is None:
            return None
        return OpenSessionRow(UUID(row[0]), UUID(row[1]), _dt(row[2]))
