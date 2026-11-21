"""Copies queued items into the worker's local record (PLAN 2.10).

`RecordMirror` wraps the queue a writer already uses (`ScanFlow`,
`SessionManager`, the engine child's `EngineEventRecorder`) and has the same
`enqueue` signature. The real queue write happens first and exactly as
before: its result is returned and its exceptions propagate, so the scan
path's behaviour (journal fallback, health flags) is unchanged. Only after a
successful write is the item offered to the mirror.

The local record is written on a daemon thread from a bounded buffer, so a
slow or locked record file never adds latency to a scan (CLAUDE.md rule 1).
A full buffer or a failing write loses that copy only (counted); the item is
safe in the queue, which is the record that matters. Mirrored types:
`session_start`, `session_end`, `scan` and `attention_event`; everything
else (buckets, health, alerts, disputes) is ignored.
"""

import logging
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from ag_shared.api import (
    AttentionEventPayload,
    EventType,
    ScanPayload,
    SessionEndPayload,
    SessionStartPayload,
)
from ag_shared.states import ScanOutcome
from pydantic import BaseModel, JsonValue

from ag_workstation.scan.journal import ItemQueue

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY: Final = 2048
_MIRRORED: Final = frozenset(
    {EventType.SESSION_START, EventType.SESSION_END, EventType.SCAN, EventType.ATTENTION_EVENT}
)


class RecordWriter(Protocol):
    """The write side of `PersonalRecordStore`."""

    def note_session_start(self, session_id: UUID, user_id: UUID, started_at: datetime) -> None:
        """A session began."""
        ...

    def note_session_end(self, session_id: UUID, ended_at: datetime) -> None:
        """A session ended."""
        ...

    def note_scan(
        self, session_id: UUID, user_id: UUID, scanned_at: datetime, outcome: ScanOutcome
    ) -> None:
        """Count one attributed scan."""
        ...

    def note_event(self, event_id: UUID, payload: AttentionEventPayload) -> bool:
        """Keep one attributed event."""
        ...


@dataclass(frozen=True, slots=True)
class _Copy:
    item_type: str
    payload: BaseModel | Mapping[str, JsonValue]
    occurred_at: datetime
    record_id: UUID | None


_STOP: Final = object()


def apply_copy(
    writer: RecordWriter,
    item_type: str,
    payload: BaseModel | Mapping[str, JsonValue],
    occurred_at: datetime,
    record_id: UUID | None,
) -> None:
    """Write one queued item to the record (raises on a bad item or store error)."""
    body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    if item_type == EventType.SESSION_START:
        if record_id is None:
            return
        start = SessionStartPayload.model_validate(body)
        writer.note_session_start(record_id, start.user_id, occurred_at)
    elif item_type == EventType.SESSION_END:
        end = SessionEndPayload.model_validate(body)
        writer.note_session_end(end.session_id, occurred_at)
    elif item_type == EventType.SCAN:
        scan = ScanPayload.model_validate(body)
        if scan.session_id is not None and scan.user_id is not None:
            writer.note_scan(scan.session_id, scan.user_id, occurred_at, scan.outcome)
    elif item_type == EventType.ATTENTION_EVENT and record_id is not None:
        writer.note_event(record_id, AttentionEventPayload.model_validate(body))


class RecordMirror:
    """An `ItemQueue` that also copies items into the local record. Thread-safe."""

    def __init__(
        self,
        inner: ItemQueue,
        writer: RecordWriter,
        *,
        capacity: int = DEFAULT_CAPACITY,
        start: bool = True,
    ) -> None:
        """`start=False` keeps the writer thread stopped (tests call `drain`)."""
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._inner = inner
        self._writer = writer
        self._buffer: queue.Queue[object] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._outstanding = 0
        self._closed = False
        self.dropped = 0
        """Copies lost to a full buffer or after close."""
        self.failed = 0
        """Copies the record store did not take."""
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._loop, name="ag-my-record", daemon=True)
            self._thread.start()

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        """Queue the item in the real queue (raising as it does), then copy it."""
        result = self._inner.enqueue(item_type, payload, occurred_at, record_id=record_id)
        if item_type in _MIRRORED:
            self._offer(_Copy(item_type, payload, occurred_at, record_id or _result_id(result)))
        return result

    def _offer(self, copy: _Copy) -> None:
        with self._lock:
            if self._closed:
                self.dropped += 1
                return
            try:
                self._buffer.put_nowait(copy)
            except queue.Full:
                self.dropped += 1
                logger.warning("my record buffer full; a copy was dropped")
                return
            self._outstanding += 1

    def drain(self) -> int:
        """Write every buffered copy on the calling thread; returns how many."""
        done = 0
        while True:
            try:
                item = self._buffer.get_nowait()
            except queue.Empty:
                return done
            if isinstance(item, _Copy):
                self._write(item)
                done += 1

    def flush(self, timeout_s: float = 2.0) -> bool:
        """Wait until every offered copy is written; False on timeout."""
        if self._thread is None:
            self.drain()
        with self._idle:
            return self._idle.wait_for(lambda: self._outstanding == 0, timeout_s)

    def _loop(self) -> None:
        while True:
            item = self._buffer.get()
            if item is _STOP:
                return
            if isinstance(item, _Copy):
                self._write(item)

    def _write(self, copy: _Copy) -> None:
        try:
            apply_copy(self._writer, copy.item_type, copy.payload, copy.occurred_at, copy.record_id)
        except Exception as exc:
            self.failed += 1
            logger.error("my record copy of %s failed (%s)", copy.item_type, type(exc).__name__)
        finally:
            with self._idle:
                self._outstanding -= 1
                self._idle.notify_all()

    def close(self, timeout_s: float = 2.0) -> None:
        """Write what is buffered (at most `timeout_s`), then stop. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.flush(timeout_s)
        if self._thread is not None:
            try:
                self._buffer.put(_STOP, timeout=timeout_s)
            except queue.Full:
                return
            self._thread.join(timeout_s)


def _result_id(result: object) -> UUID | None:
    value = getattr(result, "id", None)
    return value if isinstance(value, UUID) else None
