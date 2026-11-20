"""The scan flow (PLAN 2.6; scope 4.1 and 4.4; detection-engine.md 5; risks A1-A3).

`ScanFlow.on_barcode` turns one barcode into one `scan` item, and for a
warned scan one `scan_warning` attention event, without ever refusing or
holding up the scan (CLAUDE.md rules 1-3):

1. The scan is written to the `ScanJournal` as an intent (a valid scan
   item with state Unknown, outcome `unmonitored`) and fsynced, before the
   engine or anything else is asked. See `journal.py` for why the record is
   not put in the queue yet: the server must get a single `scan` item with
   `state_at_scan` and `outcome`, and queue items cannot be updated.
2. The scan state: no worker session gives Unknown without asking the engine
   (A1, the scan is unattributed and the screen asks for login; A6, nobody
   to attribute a state to); a scheduled break gives On Break; otherwise the
   engine is asked with a 150 ms deadline (`BoundedStateQuery`) and every
   failure is Unknown.
3. Outcome from the state (`scan_outcome`, A2): Attentive -> `attentive`;
   Distracted, Drowsy, Sleeping, Absent -> `warned`; Unknown, On Break ->
   `unmonitored`. So `outcome` always matches `state_at_scan`.
4. The final scan item goes to the `EventQueue` with the scan's id and the
   journal intent is committed. If the queue write fails, the final item goes
   to the journal instead (still durable) and a health flag is raised;
   `recover_journal` moves it to the queue later.
5. Warned scan: a `scan_warning` event linked to the scan. The capture gate
   is asked first; closed means no burst is requested at all and the event is
   queued at once with `images_withheld`. Open means a burst is requested and
   the event is queued when the `GatedEvidenceSink` reports the outcome
   (`on_capture_outcome`) or after `capture_wait_s`, whichever is first. A
   failed request or a missing outcome still queues the event, with
   `images_withheld = disk_full` (the wire value for "could not be stored",
   as the sink uses it).
6. Repeated warnings of one session within a window escalate (`ScanResult.escalated`).

Steps 2-6 run after the scan is durable; the engine part is bounded by the
query timeout, so `on_barcode` returns in well under 200 ms even when the
engine hangs. `on_barcode` never raises.
"""

import logging
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from ag_shared.api import (
    AttentionEventKind,
    AttentionEventPayload,
    EventType,
    ImagesWithheld,
    ScanPayload,
)
from ag_shared.ids import Uuid7Generator
from ag_shared.states import AttentionState, ScanOutcome, scan_outcome
from pydantic import BaseModel

from ag_workstation.auth.sessions import CurrentSession
from ag_workstation.clock import Clock
from ag_workstation.consent.decision import GateDecision, GateReason
from ag_workstation.consent.gate import SessionSource
from ag_workstation.consent.sink import CaptureOutcome
from ag_workstation.engine_api import (
    SCAN_QUERY_TIMEOUT_S,
    BurstKind,
    EvidenceBurst,
    EvidenceRequester,
    StationState,
    StationStateQuery,
)
from ag_workstation.scan.escalation import EscalationConfig, EscalationCounter
from ag_workstation.scan.input import MAX_BARCODE_CHARS
from ag_workstation.scan.journal import ItemQueue, JournalRecovery, ScanJournal
from ag_workstation.scan.messages import ScanMessageKey
from ag_workstation.scan.query import BoundedStateQuery, QueryAnswer, QueryStatus

logger = logging.getLogger(__name__)

CAPTURE_FAILED: Final = ImagesWithheld.DISK_FULL
"""`images_withheld` when the burst could not be taken or stored in time."""
_MAX_SIGNAL_KEY: Final = 64


class HealthFlag(StrEnum):
    """Problems the scan flow reports to the service health (never to the scan)."""

    QUEUE_WRITE_FAILED = "queue_write_failed"
    """A record went to the emergency journal instead of the queue."""
    JOURNAL_WRITE_FAILED = "journal_write_failed"
    """The write-ahead intent or an emergency record could not be journaled."""
    RECORD_LOST = "record_lost"
    """Neither the queue nor the journal took a record."""


class Cancellable(Protocol):
    """A scheduled callback."""

    def cancel(self) -> None:
        """Stop it if it has not run."""
        ...


class Scheduler(Protocol):
    """Runs a callback after a delay, on another thread."""

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> Cancellable:
        """Run `callback` once after `delay_s` seconds."""
        ...


class ThreadingScheduler:
    """`Scheduler` on daemon `threading.Timer`s."""

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> Cancellable:
        """Start a daemon timer."""
        timer = threading.Timer(delay_s, callback)
        timer.daemon = True
        timer.start()
        return timer


class CaptureGateCheck(Protocol):
    """The capture gate's decision API (`CaptureGate.decide`)."""

    def decide(self, burst: EvidenceBurst | None = None) -> GateDecision:
        """Whether frames may be written now."""
        ...


@dataclass(frozen=True, slots=True)
class ScanFlowConfig:
    """Scan flow settings."""

    query_timeout_s: float = SCAN_QUERY_TIMEOUT_S
    max_in_flight_queries: int = 2
    capture_wait_s: float = 5.0
    """Longest wait for the burst outcome before the event is queued without
    images. A scan burst needs about 1 s (2 post-frames at 2 fps)."""
    escalation: EscalationConfig = field(default_factory=EscalationConfig)

    def __post_init__(self) -> None:
        if self.capture_wait_s <= 0:
            raise ValueError("capture_wait_s must be positive")


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What the scan screen shows. The scan is accepted whatever this says."""

    scan_id: UUID
    barcode: str
    scanned_at: datetime
    outcome: ScanOutcome
    state: AttentionState
    confidence: float | None
    query_status: QueryStatus
    message_key: ScanMessageKey
    session_id: UUID | None = None
    user_id: UUID | None = None
    event_id: UUID | None = None
    """The `scan_warning` event, for a warned scan."""
    escalated: bool = False
    """Repeated warnings: tell the worker a supervisor will check in."""
    prompt_login: bool = False
    """No session (A1): the screen asks for login."""
    persisted: bool = True
    """False only if neither the queue nor the journal could take the scan."""

    @property
    def warn(self) -> bool:
        """Show the warning banner and play the sound."""
        return self.outcome is ScanOutcome.WARNED


@dataclass(frozen=True, slots=True)
class ScanFlowHealth:
    """Counters for the health report."""

    scans: int
    queue_write_failures: int
    journal_write_failures: int
    records_lost: int
    engine_query_failures: int
    journal_backlog: int
    pending_events: int


@dataclass
class _PendingEvent:
    event_id: UUID
    occurred_at: datetime
    fields: dict[str, object]
    timer: Cancellable | None = None


def _clean_signals(signals: Mapping[str, float | None]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key, value in signals.items():
        if not isinstance(key, str) or len(key) > _MAX_SIGNAL_KEY:
            continue
        number = float(value) if isinstance(value, int | float) else None
        out[key] = number if number is not None and math.isfinite(number) else None
    return out


class ScanFlow:
    """Records scans and warned-scan events. Thread-safe; `on_barcode` never raises."""

    def __init__(
        self,
        *,
        queue: ItemQueue,
        journal: ScanJournal,
        engine: StationStateQuery,
        evidence: EvidenceRequester,
        sessions: SessionSource,
        clock: Clock,
        gate: CaptureGateCheck | None = None,
        on_break: Callable[[], bool] | None = None,
        config: ScanFlowConfig | None = None,
        ids: Uuid7Generator | None = None,
        scheduler: Scheduler | None = None,
        on_health_flag: Callable[[HealthFlag], None] | None = None,
    ) -> None:
        """`engine` may be an in-process engine or an `IpcClient`. `on_break`
        reports a scheduled break from memory (bundle shifts); the engine's
        own On Break answer is honoured too. `gate` is the capture gate,
        asked before a burst is requested; the `GatedEvidenceSink` that
        receives the burst must call `on_capture_outcome`."""
        self._queue = queue
        self._journal = journal
        self._evidence = evidence
        self._sessions = sessions
        self._clock = clock
        self._gate = gate
        self._on_break = on_break
        self._config = config or ScanFlowConfig()
        self._ids = ids or Uuid7Generator(now=clock.now)
        self._scheduler = scheduler or ThreadingScheduler()
        self._on_health_flag = on_health_flag
        self._query = BoundedStateQuery(
            engine,
            clock,
            timeout_s=self._config.query_timeout_s,
            max_in_flight=self._config.max_in_flight_queries,
        )
        self._escalation = EscalationCounter(self._config.escalation)
        self._lock = threading.Lock()
        self._pending: dict[UUID, _PendingEvent] = {}
        self._scans = 0
        self._queue_failures = 0
        self._journal_failures = 0
        self._lost = 0
        self._engine_failures = 0

    # -- the scan path -------------------------------------------------------

    def on_barcode(self, barcode: str) -> ScanResult:
        """Record one scan and decide its outcome. Never raises, never rejects."""
        scanned_at = self._clock.now()
        scan_id = self._ids()
        code = str(barcode)
        if len(code) > MAX_BARCODE_CHARS:
            logger.warning("barcode of %d chars truncated to %d", len(code), MAX_BARCODE_CHARS)
            code = code[:MAX_BARCODE_CHARS]
        try:
            return self._scan(scan_id, scanned_at, code)
        except Exception as exc:
            # Unreachable by design (every step is guarded); the scan is still accepted.
            logger.exception("scan flow failed unexpectedly (%s)", type(exc).__name__)
            return ScanResult(
                scan_id=scan_id,
                barcode=code,
                scanned_at=scanned_at,
                outcome=ScanOutcome.UNMONITORED,
                state=AttentionState.UNKNOWN,
                confidence=None,
                query_status=QueryStatus.ERROR,
                message_key=ScanMessageKey.SCAN_UNMONITORED,
                persisted=False,
            )

    def _scan(self, scan_id: UUID, scanned_at: datetime, code: str) -> ScanResult:
        mono = self._clock.monotonic()
        session = self._current_session()
        session_id = session.session_id if session else None
        user_id = session.user_id if session else None
        self._escalation.observe_session(session_id)
        with self._lock:
            self._scans += 1

        # 1. Durable before anything else is asked.
        intent = ScanPayload(
            session_id=session_id,
            user_id=user_id,
            barcode=code,
            state_at_scan=AttentionState.UNKNOWN,
            outcome=ScanOutcome.UNMONITORED,
        )
        journaled = self._journal_write(self._journal.write_intent, scan_id, scanned_at, intent)

        # 2. The scan state.
        answer = self._scan_state(session)
        state = answer.state
        outcome = scan_outcome(state.state)

        # 4. The final scan item.
        final = ScanPayload(
            session_id=session_id,
            user_id=user_id,
            barcode=code,
            state_at_scan=state.state,
            confidence=state.confidence,
            outcome=outcome,
        )
        persisted = self._persist(EventType.SCAN, scan_id, scanned_at, final, journaled=journaled)

        # 5, 6. Warned scan: event, evidence, escalation.
        event_id: UUID | None = None
        escalated = False
        if outcome is ScanOutcome.WARNED and session is not None:
            escalated = self._escalation.record_warning(session.session_id, mono).escalated
            event_id = self._warn(scan_id, scanned_at, state, session)

        return ScanResult(
            scan_id=scan_id,
            barcode=code,
            scanned_at=scanned_at,
            outcome=outcome,
            state=state.state,
            confidence=state.confidence,
            query_status=answer.status,
            message_key=_message(outcome, state.state, session is None, escalated),
            session_id=session_id,
            user_id=user_id,
            event_id=event_id,
            escalated=escalated,
            prompt_login=session is None,
            persisted=persisted or journaled,
        )

    def _current_session(self) -> CurrentSession | None:
        try:
            return self._sessions.current()
        except Exception as exc:
            logger.error("session lookup failed (%s); scan is unattributed", type(exc).__name__)
            return None

    def _scan_state(self, session: CurrentSession | None) -> QueryAnswer:
        if session is None:
            return self._query.unknown(QueryStatus.SKIPPED)
        if self._scheduled_break():
            state = StationState(
                state=AttentionState.ON_BREAK,
                confidence=None,
                low_confidence=False,
                at=self._clock.now(),
                ts=self._clock.monotonic(),
                engine_version="",
                config_version=0,
            )
            return QueryAnswer(state, QueryStatus.SKIPPED)
        answer = self._query.ask()
        if answer.status is not QueryStatus.OK:
            with self._lock:
                self._engine_failures += 1
        return answer

    def _scheduled_break(self) -> bool:
        if self._on_break is None:
            return False
        try:
            return bool(self._on_break())
        except Exception as exc:
            # Unknown break status: ask the engine, which also knows the calendar.
            logger.warning("break check failed: %s", type(exc).__name__)
            return False

    # -- persistence -----------------------------------------------------------

    def _journal_write(
        self,
        write: Callable[[UUID, str, datetime, BaseModel], None],
        record_id: UUID,
        occurred_at: datetime,
        payload: BaseModel,
        item_type: str = EventType.SCAN,
    ) -> bool:
        try:
            write(record_id, item_type, occurred_at, payload)
            return True
        except Exception as exc:
            logger.error("scan journal write failed: %s", type(exc).__name__)
            with self._lock:
                self._journal_failures += 1
            self._flag(HealthFlag.JOURNAL_WRITE_FAILED)
            return False

    def _persist(
        self,
        item_type: EventType,
        record_id: UUID,
        occurred_at: datetime,
        payload: BaseModel,
        *,
        journaled: bool = False,
    ) -> bool:
        """Queue the item; fall back to the journal. True if either took it."""
        try:
            self._queue.enqueue(item_type, payload, occurred_at, record_id=record_id)
        except Exception as exc:
            logger.error("queue write failed (%s); using the scan journal", type(exc).__name__)
            with self._lock:
                self._queue_failures += 1
            self._flag(HealthFlag.QUEUE_WRITE_FAILED)
            if self._journal_write(
                self._journal.write_item, record_id, occurred_at, payload, item_type
            ):
                return True
            with self._lock:
                self._lost += 1
            self._flag(HealthFlag.RECORD_LOST)
            return False
        if journaled:
            try:
                self._journal.commit(record_id)
            except Exception as exc:
                # Harmless: recovery finds the item already queued.
                logger.warning("scan journal commit failed: %s", type(exc).__name__)
        return True

    def _flag(self, flag: HealthFlag) -> None:
        if self._on_health_flag is None:
            return
        try:
            self._on_health_flag(flag)
        except Exception as exc:
            logger.error("health flag listener failed: %s", type(exc).__name__)

    # -- warned scans ------------------------------------------------------------

    def _warn(
        self, scan_id: UUID, scanned_at: datetime, state: StationState, session: CurrentSession
    ) -> UUID:
        event_id = self._ids()
        fields: dict[str, object] = {
            "kind": AttentionEventKind.SCAN_WARNING,
            "state": state.state,
            "confidence": state.confidence,
            "low_confidence": state.low_confidence,
            "started_at": scanned_at,
            "scan_id": scan_id,
            "session_id": session.session_id,
            "user_id": session.user_id,
            "signals": _clean_signals(state.signals),
            "camera_states": tuple(c.to_snapshot() for c in state.camera_states),
            "engine_version": state.engine_version[:64],
            "config_version": state.config_version,
        }
        pending = _PendingEvent(event_id, scanned_at, fields)
        decision = self._gate_decision()
        if decision is not None and not decision.open:
            # Closed gate: no frames are even requested (data minimization).
            self._emit(pending, (), decision.images_withheld)
            return event_id
        with self._lock:
            self._pending[event_id] = pending
        try:
            pending.timer = self._scheduler.schedule(
                self._config.capture_wait_s, lambda: self._expire(event_id)
            )
        except Exception as exc:
            logger.error("capture wait timer failed (%s)", type(exc).__name__)
        try:
            self._evidence.request_burst(event_id, BurstKind.SCAN_WARNING, state.ts)
        except Exception as exc:
            logger.error("evidence burst request failed: %s", type(exc).__name__)
            self._finish(event_id, (), CAPTURE_FAILED)
        return event_id

    def _gate_decision(self) -> GateDecision | None:
        if self._gate is None:
            return None
        try:
            return self._gate.decide()
        except Exception as exc:
            logger.error("capture gate failed (%s); images withheld", type(exc).__name__)
            return GateDecision(GateReason.ERROR, None)

    def on_capture_outcome(self, outcome: CaptureOutcome) -> None:
        """`GatedEvidenceSink(on_outcome=...)`: queue the waiting event with its images."""
        self.claim_capture_outcome(outcome)

    def claim_capture_outcome(self, outcome: CaptureOutcome) -> bool:
        """Like `on_capture_outcome`; False for a burst that is not a waiting
        scan warning (an episode burst, or one that came after `capture_wait_s`),
        so a combined listener can pass it on."""
        return self._finish(outcome.event_id, outcome.image_ids, outcome.images_withheld)

    def _expire(self, event_id: UUID) -> None:
        if self._finish(event_id, (), CAPTURE_FAILED, cancel=False):
            logger.warning(
                "no capture outcome for event %s in time; queued without images", event_id
            )

    def _finish(
        self,
        event_id: UUID,
        image_ids: tuple[UUID, ...],
        withheld: ImagesWithheld,
        *,
        cancel: bool = True,
    ) -> bool:
        with self._lock:
            pending = self._pending.pop(event_id, None)
        if pending is None:
            return False
        if cancel and pending.timer is not None:
            try:
                pending.timer.cancel()
            except Exception as exc:
                logger.warning("capture wait timer cancel failed: %s", type(exc).__name__)
        self._emit(pending, image_ids, withheld)
        return True

    def _emit(
        self, pending: _PendingEvent, image_ids: tuple[UUID, ...], withheld: ImagesWithheld
    ) -> None:
        payload = self._event_payload(pending, image_ids, withheld)
        if payload is None:
            with self._lock:
                self._lost += 1
            self._flag(HealthFlag.RECORD_LOST)
            return
        self._persist(EventType.ATTENTION_EVENT, pending.event_id, pending.occurred_at, payload)

    def _event_payload(
        self, pending: _PendingEvent, image_ids: tuple[UUID, ...], withheld: ImagesWithheld
    ) -> AttentionEventPayload | None:
        data: dict[str, object] = {
            **pending.fields,
            "images_withheld": withheld,
            "image_ids": image_ids,
        }
        try:
            return AttentionEventPayload.model_validate(data)
        except Exception as exc:
            logger.error("scan warning payload invalid (%s); dropping details", type(exc).__name__)
        try:
            return AttentionEventPayload.model_validate(
                {**data, "signals": {}, "camera_states": (), "engine_version": ""}
            )
        except Exception as exc:
            logger.error("scan warning payload not built: %s", type(exc).__name__)
            return None

    # -- service hooks -------------------------------------------------------------

    def recover_journal(self) -> JournalRecovery:
        """Move journaled records to the queue (service start, housekeeping)."""
        return self._journal.recover(self._queue)

    def health(self) -> ScanFlowHealth:
        """Counters since start."""
        backlog = self._journal.backlog
        with self._lock:
            return ScanFlowHealth(
                scans=self._scans,
                queue_write_failures=self._queue_failures,
                journal_write_failures=self._journal_failures,
                records_lost=self._lost,
                engine_query_failures=self._engine_failures,
                journal_backlog=backlog,
                pending_events=len(self._pending),
            )

    def close(self) -> None:
        """Queue every event still waiting for its images (shutdown)."""
        with self._lock:
            waiting = list(self._pending)
        for event_id in waiting:
            self._finish(event_id, (), CAPTURE_FAILED)


def _message(
    outcome: ScanOutcome, state: AttentionState, no_session: bool, escalated: bool
) -> ScanMessageKey:
    if no_session:
        return ScanMessageKey.SCAN_LOGIN_REQUIRED
    if outcome is ScanOutcome.ATTENTIVE:
        return ScanMessageKey.SCAN_OK
    if outcome is ScanOutcome.WARNED:
        return ScanMessageKey.SCAN_WARNED_REPEATED if escalated else ScanMessageKey.SCAN_WARNED
    if state is AttentionState.ON_BREAK:
        return ScanMessageKey.SCAN_ON_BREAK
    return ScanMessageKey.SCAN_UNMONITORED
