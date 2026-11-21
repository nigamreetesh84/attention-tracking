"""Dispute submission from the "My record" screen (PLAN 2.10; scope 7; D8).

A worker disputes one of their own attention events with a reason. The
dispute travels to the server as the contract's `dispute` batch item
(`ag_shared.api.DisputePayload`: `attention_event_id, user_id, message`).

Exactly once (double tap, retry, restart):

1. The dispute is first saved in the local record, keyed by the event: one
   dispute per event. A second submit for the same event finds the saved one
   and queues nothing new.
2. Its UUIDv7 is fixed when it is saved and is the queue record id, so every
   retry uses the same id: the queue refuses a second record with that id
   while one is waiting, and the server inserts by id (contract 5.1), so a
   resend after the first copy was delivered is a `duplicate`.
3. Only after the queue took it is it marked `queued`. A crash or queue
   failure in between leaves it `pending`; `recover()` (app start, and before
   every submit) queues pending disputes again with their saved id.

Only the worker's own events can be disputed: the event must be in the local
record with that worker's `user_id` (the server checks again; RBAC is
server-side).
"""

import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from ag_shared.api import DisputePayload, EventType
from ag_shared.ids import Uuid7Generator

from ag_workstation.clock import Clock
from ag_workstation.record.store import DisputeRecord, DisputeStatus, PersonalRecordStore
from ag_workstation.scan.journal import ItemQueue

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS: Final = 2000
"""`DisputePayload.message` limit."""
_ALREADY_QUEUED: Final = "already queued"
"""`EventQueue.enqueue` raises `ValueError("record ... is already queued")`."""


class DisputeOutcome(StrEnum):
    """What the screen tells the worker."""

    QUEUED = "queued"
    """New dispute, in the queue for the server."""
    SAVED = "saved"
    """New dispute, saved here; the queue did not take it yet (sent later)."""
    ALREADY_DISPUTED = "already_disputed"
    """The event already has a dispute; nothing new was queued."""
    NOT_FOUND = "not_found"
    """Not this worker's event, or no longer kept here."""
    FAILED = "failed"
    """Nothing was saved (the local record failed, or the message is empty)."""


@dataclass(frozen=True, slots=True)
class DisputeResult:
    """Outcome of `DisputeService.submit`."""

    outcome: DisputeOutcome
    dispute_id: UUID | None = None


class DisputeService:
    """Saves and queues disputes. Thread-safe; `submit` and `recover` never raise."""

    def __init__(
        self,
        store: PersonalRecordStore,
        queue: ItemQueue,
        clock: Clock,
        *,
        ids: Uuid7Generator | None = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._clock = clock
        self._ids = ids or Uuid7Generator(now=clock.now)
        self._lock = threading.Lock()

    def submit(self, user_id: UUID, attention_event_id: UUID, message: str) -> DisputeResult:
        """Dispute `attention_event_id` for `user_id` with `message`."""
        text = " ".join(str(message).split())[:MAX_MESSAGE_CHARS]
        if not text:
            return DisputeResult(DisputeOutcome.FAILED)
        with self._lock:
            try:
                return self._submit(user_id, attention_event_id, text)
            except Exception as exc:
                logger.error("dispute not saved (%s)", type(exc).__name__)
                return DisputeResult(DisputeOutcome.FAILED)

    def _submit(self, user_id: UUID, attention_event_id: UUID, text: str) -> DisputeResult:
        self._recover_locked()
        if self._store.event_for(user_id, attention_event_id) is None:
            return DisputeResult(DisputeOutcome.NOT_FOUND)
        record, new = self._store.reserve_dispute(
            dispute_id=self._ids(),
            attention_event_id=attention_event_id,
            user_id=user_id,
            message=text,
            created_at=self._clock.now(),
        )
        if not new:
            if record.status is DisputeStatus.PENDING:
                self._queue_one(record)
            return DisputeResult(DisputeOutcome.ALREADY_DISPUTED, record.id)
        if self._queue_one(record):
            return DisputeResult(DisputeOutcome.QUEUED, record.id)
        return DisputeResult(DisputeOutcome.SAVED, record.id)

    def _queue_one(self, record: DisputeRecord) -> bool:
        """Queue a saved dispute with its own id; True once it is in the queue."""
        payload = DisputePayload(
            attention_event_id=record.attention_event_id,
            user_id=record.user_id,
            message=record.message,
        )
        try:
            self._queue.enqueue(EventType.DISPUTE, payload, record.created_at, record_id=record.id)
        except ValueError as exc:
            if _ALREADY_QUEUED not in str(exc):
                logger.error("dispute not queued (%s)", type(exc).__name__)
                return False
            # A crash after the queue write and before the mark: it is in the queue.
        except Exception as exc:
            logger.error("dispute not queued yet (%s); kept locally", type(exc).__name__)
            return False
        try:
            self._store.mark_dispute_queued(record.id)
        except Exception as exc:
            # Harmless: the next recover finds it already queued.
            logger.warning("dispute queued but not marked (%s)", type(exc).__name__)
        return True

    def recover(self) -> int:
        """Queue every saved dispute that is not queued yet; returns how many were."""
        with self._lock:
            try:
                return self._recover_locked()
            except Exception as exc:
                logger.error("dispute recovery failed (%s)", type(exc).__name__)
                return 0

    def _recover_locked(self) -> int:
        return sum(1 for record in self._store.pending_disputes() if self._queue_one(record))
