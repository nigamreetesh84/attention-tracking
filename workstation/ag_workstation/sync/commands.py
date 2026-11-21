"""Heartbeat commands that the UI process must carry out.

The sync client runs in the service parent, but worker sessions live in the
UI process (PLAN 2.9-pre). Session commands (`assign_worker`,
`end_session`) therefore go through a small durable inbox, an SQLite file in
the data directory that both processes open:

1. the sync client `put`s each new command (idempotent by command id);
2. the UI's `CommandPump` hands pending commands to the `SessionManager`
   (itself idempotent by command id) and marks them handled;
3. the sync client acknowledges handled commands in the next heartbeat and
   marks them acked once the server has received that heartbeat.

A command is acknowledged only after the UI handled it, so a station whose
UI is not running keeps receiving it until it expires on the server.
Rows hold command ids, user, session and assignment ids, never PINs.
"""

import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from ag_shared.api import AssignWorkerCommand, Command, EndSessionCommand
from pydantic import TypeAdapter, ValidationError

from ag_workstation.clock import Clock

logger = logging.getLogger(__name__)

INBOX_FILE: Final = "commands.db"
RETENTION: Final = timedelta(days=7)
OUTCOME_ERROR: Final = "error"

_COMMAND: TypeAdapter[Command] = TypeAdapter(Command)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS command (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    handled_at INTEGER,
    outcome TEXT,
    acked_at INTEGER
)
"""


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class CommandInbox:
    """The durable inbox shared by the service parent and the UI. Thread-safe."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        """Use `CommandInbox.open`."""
        self._conn = conn
        self._clock = clock
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path, clock: Clock) -> "CommandInbox":
        """Open (creating if needed) the inbox at `path`."""
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(_SCHEMA)
        except BaseException:
            conn.close()
            raise
        return cls(conn, clock)

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    def put(self, command: AssignWorkerCommand | EndSessionCommand) -> bool:
        """Store a command for the UI; False if it was already there."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO command (id, type, body, received_at) VALUES (?, ?, ?, ?)",
                (
                    str(command.id),
                    command.type,
                    command.model_dump_json(),
                    _ms(self._clock.now()),
                ),
            )
            return cur.rowcount == 1

    def pending(self) -> list[Command]:
        """Commands not yet handled, oldest first. Unreadable rows are marked handled."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, body FROM command WHERE handled_at IS NULL ORDER BY received_at, id"
            ).fetchall()
        commands: list[Command] = []
        for rid, body in rows:
            try:
                commands.append(_COMMAND.validate_json(body))
            except ValidationError:
                logger.error("command %s in the inbox is unreadable; dropped", rid)
                self.mark_handled(UUID(rid), OUTCOME_ERROR)
        return commands

    def mark_handled(self, command_id: UUID, outcome: str) -> None:
        """Record that the UI carried out a command (any outcome is final)."""
        with self._lock:
            self._conn.execute(
                "UPDATE command SET handled_at = ?, outcome = ? "
                "WHERE id = ? AND handled_at IS NULL",
                (_ms(self._clock.now()), outcome, str(command_id)),
            )

    def handled_unacked(self) -> list[UUID]:
        """Handled commands the server has not yet seen acknowledged."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM command WHERE handled_at IS NOT NULL AND acked_at IS NULL "
                "ORDER BY handled_at, id"
            ).fetchall()
        return [UUID(r[0]) for r in rows]

    def mark_acked(self, ids: Iterable[UUID]) -> None:
        """The server received a heartbeat acknowledging these ids."""
        now = _ms(self._clock.now())
        with self._lock:
            self._conn.executemany(
                "UPDATE command SET acked_at = ? WHERE id = ? AND acked_at IS NULL",
                [(now, str(i)) for i in ids],
            )

    def outcome(self, command_id: UUID) -> str | None:
        """The recorded outcome, or None if unknown or not handled."""
        with self._lock:
            row = self._conn.execute(
                "SELECT outcome FROM command WHERE id = ?", (str(command_id),)
            ).fetchone()
        return None if row is None else row[0]

    def purge(self, retention: timedelta = RETENTION) -> int:
        """Drop acknowledged commands older than `retention`."""
        cutoff = _ms(self._clock.now() - retention)
        with self._lock:
            return self._conn.execute(
                "DELETE FROM command WHERE acked_at IS NOT NULL AND received_at < ?", (cutoff,)
            ).rowcount


class SessionCommandHandler(Protocol):
    """What the pump drives (`SessionManager` satisfies it)."""

    def handle_assign_worker(self, command: AssignWorkerCommand) -> object:
        """Start a supervisor-assigned session."""
        ...

    def handle_end_session(self, command: EndSessionCommand) -> object:
        """End the named session."""
        ...


class CommandPump:
    """UI side: hands pending inbox commands to the session manager."""

    def __init__(self, inbox: CommandInbox, handler: SessionCommandHandler) -> None:
        self._inbox = inbox
        self._handler = handler

    def poll(self) -> int:
        """Handle every pending command; returns how many. Never raises."""
        try:
            pending = self._inbox.pending()
        except sqlite3.Error as exc:
            logger.warning("command inbox unreadable (%s)", type(exc).__name__)
            return 0
        done = 0
        for command in pending:
            try:
                if isinstance(command, AssignWorkerCommand):
                    outcome = self._handler.handle_assign_worker(command)
                elif isinstance(command, EndSessionCommand):
                    outcome = self._handler.handle_end_session(command)
                else:
                    outcome = "unsupported"
                result = str(getattr(outcome, "value", outcome))
            except Exception as exc:
                # Final, so a broken command is not retried forever; the
                # supervisor sees no session and can assign again.
                logger.error("command %s failed (%s)", command.id, type(exc).__name__)
                result = OUTCOME_ERROR
            try:
                self._inbox.mark_handled(command.id, result)
            except sqlite3.Error as exc:
                logger.warning("command %s not marked (%s)", command.id, type(exc).__name__)
                continue
            done += 1
        return done
