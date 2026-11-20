"""Scan flow (PLAN 2.6): outcomes, fail-open engine handling, persistence order,
warned-scan events and evidence, escalation, unattributed scans."""

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import (
    ConfigBundle,
    EventType,
    ImagesWithheld,
    SessionMethod,
)
from ag_shared.health import CameraStatus
from ag_shared.ids import Uuid7Generator, new_uuid7
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.auth import CurrentSession
from ag_workstation.clock import FakeClock
from ag_workstation.consent import CaptureGate, CapturePermit, GatedEvidenceSink, GateReason
from ag_workstation.consent.decision import GateDecision
from ag_workstation.engine_api import (
    BurstKind,
    CameraState,
    EngineTimeoutError,
    EvidenceBurst,
    StationState,
)
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.queue import EventQueue, InMemoryKeyProvider, PayloadCipher
from ag_workstation.scan import (
    CAPTURE_FAILED,
    EscalationConfig,
    EscalationCounter,
    HealthFlag,
    QueryStatus,
    ScanFlow,
    ScanFlowConfig,
    ScanJournal,
    ScanMessageKey,
    ScanResult,
    ThreadingScheduler,
    text,
)
from ag_workstation.scan.flow import _clean_signals
from pydantic import BaseModel, JsonValue

KEY = bytes(range(32))
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
WORKER = UUID("0193a0b0-0000-7000-8000-00000000c001")
WARNED = [
    AttentionState.DISTRACTED,
    AttentionState.DROWSY,
    AttentionState.SLEEPING,
    AttentionState.ABSENT,
]


# -- fakes -------------------------------------------------------------------


def _session(
    *, consent: bool = True, started: datetime = T0 - timedelta(hours=1)
) -> CurrentSession:
    return CurrentSession(
        session_id=new_uuid7(),
        user_id=WORKER,
        employee_id="E1001",
        display_name="Worker E1001",
        has_consent=consent,
        preferred_language=None,
        ppe_profile_id=None,
        method=SessionMethod.EMPLOYEE_ID_PIN,
        station_assignment_id=None,
        started_at=started,
        valid_until=None,
    )


@dataclass
class FakeSessions:
    session: CurrentSession | None = None
    fail: bool = False

    def current(self) -> CurrentSession | None:
        if self.fail:
            raise RuntimeError("session store broken")
        return self.session


class NoBundle:
    def current_bundle(self) -> ConfigBundle | None:
        return None


@dataclass(frozen=True)
class Saved:
    image_ids: tuple[UUID, ...]
    images_withheld: ImagesWithheld = ImagesWithheld.NONE


@dataclass
class FakeStore:
    fail: bool = False
    saved: list[EvidenceBurst] = field(default_factory=list)

    def save_burst(
        self, burst: EvidenceBurst, event_id: UUID | None = None, *, permit: CapturePermit
    ) -> Saved:
        if self.fail:
            raise OSError("disk gone")
        permit.redeem(burst)
        self.saved.append(burst)
        return Saved(tuple(new_uuid7() for _ in burst.frames))


@dataclass
class _Timer:
    delay_s: float
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class ManualScheduler:
    def __init__(self) -> None:
        self.timers: list[_Timer] = []

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(delay_s, callback)
        self.timers.append(timer)
        return timer

    def fire_all(self) -> None:
        for timer in list(self.timers):
            if not timer.cancelled:
                timer.callback()


class SpyQueue:
    """Delegates to a real queue, records calls, can be told to fail."""

    def __init__(self, inner: EventQueue, log: list[str]) -> None:
        self.inner = inner
        self.log = log
        self.fail = False

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | Mapping[str, JsonValue],
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        self.log.append(f"queue.{item_type}")
        if self.fail:
            raise OSError("database is locked")
        return self.inner.enqueue(item_type, payload, occurred_at, record_id=record_id)


class SpyEngine(FakeStationEngine):
    def __init__(self, log: list[str], on_query: Callable[[], None], **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]  # forwarded test kwargs
        self.log = log
        self.on_query = on_query

    def scan_state(self) -> StationState:
        self.log.append("engine.scan_state")
        self.on_query()
        return super().scan_state()


@dataclass
class Rig:
    flow: ScanFlow
    engine: FakeStationEngine
    queue: EventQueue
    spy: SpyQueue
    journal: ScanJournal
    journal_path: Path
    sessions: FakeSessions
    store: FakeStore
    scheduler: ManualScheduler
    clock: FakeClock
    log: list[str]
    flags: list[HealthFlag]

    def items(self) -> list[tuple[str, UUID, dict[str, JsonValue]]]:
        return [(r.type, r.id, r.payload) for r in self.queue.claim_batch()]

    def of_type(self, kind: EventType) -> list[dict[str, JsonValue]]:
        return [p for t, _, p in self.items() if t == kind]


@pytest.fixture
def release() -> Iterator[threading.Event]:
    event = threading.Event()
    yield event
    event.set()  # unblock any engine thread a test left hanging


@pytest.fixture
def make(tmp_path: Path, release: threading.Event) -> Iterator[Callable[..., Rig]]:
    opened: list[EventQueue | ScanJournal] = []

    def build(
        *,
        session: CurrentSession | None = None,
        gate: bool = True,
        on_break: Callable[[], bool] | None = None,
        config: ScanFlowConfig | None = None,
        engine_cls: type[FakeStationEngine] = FakeStationEngine,
        on_query: Callable[[], None] | None = None,
        sink: bool = True,
    ) -> Rig:
        clock = FakeClock(start_utc=T0)
        log: list[str] = []
        flags: list[HealthFlag] = []
        queue = EventQueue.open(tmp_path / "queue.db", InMemoryKeyProvider(KEY), clock)
        journal_path = tmp_path / "scan.jnl"
        journal = ScanJournal.open(journal_path, PayloadCipher(InMemoryKeyProvider(KEY)))
        opened.extend([queue, journal])
        sessions = FakeSessions(session if session is not None else _session())
        capture_gate = CaptureGate(sessions=sessions, bundles=NoBundle(), clock=clock)
        store = FakeStore()
        holder: list[ScanFlow] = []
        gated = GatedEvidenceSink(
            capture_gate, store, on_outcome=lambda o: holder[0].on_capture_outcome(o)
        )
        kwargs: dict[str, object] = {
            "clock": clock,
            "sleep": release.wait,
            "sink": gated if sink else None,
        }
        if engine_cls is SpyEngine:
            engine: FakeStationEngine = SpyEngine(log, on_query or (lambda: None), **kwargs)
        else:
            engine = engine_cls(**kwargs)  # type: ignore[arg-type]  # forwarded test kwargs
        spy = SpyQueue(queue, log)
        scheduler = ManualScheduler()
        flow = ScanFlow(
            queue=spy,
            journal=journal,
            engine=engine,
            evidence=engine,
            sessions=sessions,
            clock=clock,
            gate=capture_gate if gate else None,
            on_break=on_break,
            config=config,
            ids=Uuid7Generator(now=clock.now),
            scheduler=scheduler,
            on_health_flag=flags.append,
        )
        holder.append(flow)
        return Rig(
            flow,
            engine,
            queue,
            spy,
            journal,
            journal_path,
            sessions,
            store,
            scheduler,
            clock,
            log,
            flags,
        )

    yield build
    for item in opened:
        item.close()


# -- outcomes ------------------------------------------------------------------


def test_attentive_scan(make: Callable[..., Rig]) -> None:
    rig = make()
    result = rig.flow.on_barcode("ITEM-1")
    assert (result.outcome, result.state, result.query_status) == (
        ScanOutcome.ATTENTIVE,
        AttentionState.ATTENTIVE,
        QueryStatus.OK,
    )
    assert not result.warn and not result.prompt_login and result.event_id is None
    assert result.message_key is ScanMessageKey.SCAN_OK
    assert result.persisted
    items = rig.items()
    assert len(items) == 1
    kind, record_id, payload = items[0]
    assert (kind, record_id) == (EventType.SCAN, result.scan_id)
    assert payload["barcode"] == "ITEM-1"
    assert payload["state_at_scan"] == "attentive"
    assert payload["outcome"] == "attentive"
    assert payload["confidence"] == 0.9
    assert payload["user_id"] == str(WORKER)
    assert rig.engine.burst_requests == []
    assert rig.journal.backlog == 0


@pytest.mark.parametrize("state", WARNED)
def test_warned_scan_creates_event_with_evidence(
    make: Callable[..., Rig], state: AttentionState
) -> None:
    rig = make()
    cams = (CameraState(1, CameraStatus.ONLINE, state, 0.8, True),)
    rig.engine.set_state(
        state, confidence=0.7, low_confidence=True, signals={"perclos": 0.4}, cameras=cams
    )
    result = rig.flow.on_barcode("ITEM-2")
    assert result.outcome is ScanOutcome.WARNED and result.warn
    assert result.message_key is ScanMessageKey.SCAN_WARNED
    assert result.event_id is not None
    (req,) = rig.engine.burst_requests
    assert (req.event_id, req.kind) == (result.event_id, BurstKind.SCAN_WARNING)
    (scan, event) = rig.items()
    assert (scan[0], scan[1]) == (EventType.SCAN, result.scan_id)
    assert (scan[2]["state_at_scan"], scan[2]["outcome"]) == (state.value, "warned")
    assert (event[0], event[1]) == (EventType.ATTENTION_EVENT, result.event_id)
    assert event[2]["scan_id"] == str(result.scan_id)
    assert event[2]["state"] == state.value


def test_warned_event_payload(make: Callable[..., Rig]) -> None:
    rig = make()
    cams = (CameraState(1, CameraStatus.ONLINE, AttentionState.DROWSY, 0.8, True),)
    rig.engine.set_state(
        AttentionState.DROWSY,
        confidence=0.7,
        low_confidence=True,
        signals={"perclos": 0.4},
        cameras=cams,
    )
    result = rig.flow.on_barcode("ITEM-2")
    items = rig.items()
    assert [t for t, _, _ in items] == [EventType.SCAN, EventType.ATTENTION_EVENT]
    _, event_id, event = items[1]
    assert event_id == result.event_id
    assert event["kind"] == "scan_warning"
    assert event["scan_id"] == str(result.scan_id)
    assert event["session_id"] == str(result.session_id)
    assert event["user_id"] == str(WORKER)
    assert event["state"] == "drowsy"
    assert event["confidence"] == 0.7
    assert event["low_confidence"] is True
    assert event["signals"] == {"perclos": 0.4}
    assert event["images_withheld"] == "none"
    assert isinstance(event["image_ids"], list) and len(event["image_ids"]) == 5
    assert event["started_at"].startswith("2026-11-20T08:00:00")  # type: ignore[union-attr]
    assert event["ended_at"] is None
    assert len(rig.store.saved) == 1
    assert rig.flow.health().pending_events == 0


@pytest.mark.parametrize("state", [AttentionState.UNKNOWN, AttentionState.ON_BREAK])
def test_unknown_and_engine_break_are_unmonitored(
    make: Callable[..., Rig], state: AttentionState
) -> None:
    rig = make()
    rig.engine.set_state(state, confidence=None)
    result = rig.flow.on_barcode("ITEM-3")
    assert result.outcome is ScanOutcome.UNMONITORED and not result.warn
    expected = (
        ScanMessageKey.SCAN_ON_BREAK
        if state is AttentionState.ON_BREAK
        else ScanMessageKey.SCAN_UNMONITORED
    )
    assert result.message_key is expected
    assert rig.engine.burst_requests == []
    (scan,) = rig.of_type(EventType.SCAN)
    assert (scan["state_at_scan"], scan["outcome"]) == (state.value, "unmonitored")


def test_scheduled_break_skips_engine(make: Callable[..., Rig]) -> None:
    rig = make(on_break=lambda: True)
    rig.engine.set_state(AttentionState.SLEEPING)
    result = rig.flow.on_barcode("ITEM-4")
    assert (result.state, result.outcome, result.query_status) == (
        AttentionState.ON_BREAK,
        ScanOutcome.UNMONITORED,
        QueryStatus.SKIPPED,
    )
    assert result.message_key is ScanMessageKey.SCAN_ON_BREAK
    assert rig.engine.scan_queries == 0
    assert rig.engine.burst_requests == []


def test_failing_break_check_falls_back_to_engine(make: Callable[..., Rig]) -> None:
    def broken() -> bool:
        raise ValueError("bad timezone")

    rig = make(on_break=broken)
    assert rig.flow.on_barcode("X").outcome is ScanOutcome.ATTENTIVE


# -- engine failures -------------------------------------------------------------


def test_engine_hang_is_bounded(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.engine.hang_next(5.0)
    started = time.perf_counter()
    result = rig.flow.on_barcode("SLOW")
    elapsed = time.perf_counter() - started
    assert elapsed < 0.25  # 150 ms query deadline plus two fsyncs; budget ~200 ms
    assert (result.state, result.outcome, result.query_status) == (
        AttentionState.UNKNOWN,
        ScanOutcome.UNMONITORED,
        QueryStatus.TIMEOUT,
    )
    assert result.message_key is ScanMessageKey.SCAN_UNMONITORED
    (scan,) = rig.of_type(EventType.SCAN)
    assert scan["state_at_scan"] == "unknown"
    assert rig.flow.health().engine_query_failures == 1


def test_stuck_queries_do_not_pile_up(make: Callable[..., Rig]) -> None:
    rig = make(config=ScanFlowConfig(query_timeout_s=0.02, max_in_flight_queries=1))
    rig.engine.hang_next(5.0)
    assert rig.flow.on_barcode("A").query_status is QueryStatus.TIMEOUT
    started = time.perf_counter()
    result = rig.flow.on_barcode("B")
    assert time.perf_counter() - started < 0.05
    assert result.query_status is QueryStatus.BUSY
    assert result.outcome is ScanOutcome.UNMONITORED
    assert rig.engine.scan_queries == 1


@pytest.mark.parametrize(
    ("setup", "status"),
    [
        (lambda e: e.timeout_next(), QueryStatus.TIMEOUT),
        (lambda e: e.fail_next(RuntimeError("engine bug")), QueryStatus.ERROR),
        (lambda e: e.fail_next(), QueryStatus.ERROR),
        (lambda e: e.go_down(), QueryStatus.ERROR),
    ],
    ids=["engine-timeout", "exception", "unavailable", "down"],
)
def test_engine_failure_still_accepts_scan(
    make: Callable[..., Rig], setup: Callable[[FakeStationEngine], None], status: QueryStatus
) -> None:
    rig = make()
    rig.engine.set_state(AttentionState.SLEEPING)
    setup(rig.engine)
    result = rig.flow.on_barcode("ITEM-5")
    assert (result.outcome, result.query_status) == (ScanOutcome.UNMONITORED, status)
    assert result.persisted
    (scan,) = rig.of_type(EventType.SCAN)
    assert (scan["state_at_scan"], scan["outcome"]) == ("unknown", "unmonitored")


def test_engine_raising_base_exception_is_unknown(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.engine.fail_next(KeyboardInterrupt())
    assert rig.flow.on_barcode("X").query_status is QueryStatus.ERROR


def test_engine_answering_garbage_is_unknown(
    make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make()
    monkeypatch.setattr(rig.engine, "scan_state", lambda: "not a state")
    result = rig.flow.on_barcode("X")
    assert (result.state, result.query_status) == (AttentionState.UNKNOWN, QueryStatus.ERROR)


def test_engine_timeout_error_is_reported_as_timeout(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.engine.fail_next(EngineTimeoutError("late"))
    assert rig.flow.on_barcode("X").query_status is QueryStatus.TIMEOUT


# -- sessions --------------------------------------------------------------------


def test_no_session_is_unattributed_and_asks_login(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.sessions.session = None
    rig.engine.set_state(AttentionState.SLEEPING)
    result = rig.flow.on_barcode("ITEM-6")
    assert result.outcome is ScanOutcome.UNMONITORED and not result.warn
    assert result.prompt_login
    assert result.message_key is ScanMessageKey.SCAN_LOGIN_REQUIRED
    assert (result.user_id, result.session_id) == (None, None)
    assert result.query_status is QueryStatus.SKIPPED
    assert rig.engine.scan_queries == 0
    assert rig.engine.burst_requests == []
    (scan,) = rig.of_type(EventType.SCAN)
    assert (scan["user_id"], scan["session_id"], scan["state_at_scan"]) == (None, None, "unknown")


def test_session_lookup_failure_is_unattributed(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.sessions.fail = True
    result = rig.flow.on_barcode("X")
    assert result.prompt_login and result.outcome is ScanOutcome.UNMONITORED


# -- persistence order and fallbacks -------------------------------------------


def test_scan_is_durable_before_engine_is_asked(make: Callable[..., Rig], tmp_path: Path) -> None:
    seen_on_disk: list[int] = []

    def check_disk() -> None:
        # A fresh reader of the file sees the intent before the engine answers.
        reader = ScanJournal.open(tmp_path / "scan.jnl", PayloadCipher(InMemoryKeyProvider(KEY)))
        seen_on_disk.append(reader.backlog)
        reader.close()

    rig = make(engine_cls=SpyEngine, on_query=check_disk)
    rig.engine.set_state(AttentionState.DISTRACTED)
    original = rig.journal.write_intent

    def spy_intent(*args: object, **kw: object) -> None:
        rig.log.append("journal.intent")
        original(*args, **kw)  # type: ignore[arg-type]  # pass-through spy

    rig.journal.write_intent = spy_intent  # type: ignore[method-assign]  # spy
    result = rig.flow.on_barcode("ORDER")
    assert rig.log == [
        "journal.intent",
        "engine.scan_state",
        "queue.scan",
        "queue.attention_event",
    ]
    assert seen_on_disk == [1]
    assert rig.journal.backlog == 0
    assert result.outcome is ScanOutcome.WARNED


def test_queue_failure_falls_back_to_journal(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.engine.set_state(AttentionState.SLEEPING)
    rig.spy.fail = True
    result = rig.flow.on_barcode("ITEM-7")
    assert result.persisted and result.outcome is ScanOutcome.WARNED
    assert HealthFlag.QUEUE_WRITE_FAILED in rig.flags
    health = rig.flow.health()
    assert health.queue_write_failures == 2  # the scan and its event
    assert health.journal_backlog == 2
    assert rig.items() == []
    rig.spy.fail = False
    recovery = rig.flow.recover_journal()
    assert (recovery.enqueued, recovery.failed) == (2, 0)
    items = rig.items()
    assert [(t, i) for t, i, _ in items] == [
        (EventType.SCAN, result.scan_id),
        (EventType.ATTENTION_EVENT, result.event_id),
    ]
    assert items[0][2]["state_at_scan"] == "sleeping"  # the final scan, not the intent
    assert rig.flow.health().journal_backlog == 0


def test_queue_and_journal_failure_never_raise(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.spy.fail = True
    rig.journal.close()
    result = rig.flow.on_barcode("ITEM-8")
    assert result.outcome is ScanOutcome.ATTENTIVE
    assert not result.persisted
    assert HealthFlag.JOURNAL_WRITE_FAILED in rig.flags
    assert HealthFlag.RECORD_LOST in rig.flags
    assert rig.flow.health().records_lost == 1


def test_journal_failure_alone_still_queues(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.journal.close()
    result = rig.flow.on_barcode("ITEM-9")
    assert result.persisted
    assert len(rig.of_type(EventType.SCAN)) == 1
    assert rig.flow.health().journal_write_failures == 1


def test_failing_health_listener_is_ignored(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.spy.fail = True

    def boom(_: HealthFlag) -> None:
        raise RuntimeError("listener bug")

    rig.flow._on_health_flag = boom
    assert rig.flow.on_barcode("X").persisted


def test_unexpected_failure_still_returns_result(
    make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make()

    def boom(*_: object) -> ScanResult:
        raise RuntimeError("bug")

    monkeypatch.setattr(rig.flow, "_scan", boom)
    result = rig.flow.on_barcode("X")
    assert result.outcome is ScanOutcome.UNMONITORED and not result.persisted


def test_long_barcode_is_truncated_to_wire_limit(make: Callable[..., Rig]) -> None:
    rig = make()
    result = rig.flow.on_barcode("7" * 5000)
    assert len(result.barcode) == 4096
    (scan,) = rig.of_type(EventType.SCAN)
    assert scan["barcode"] == "7" * 4096


# -- evidence ------------------------------------------------------------------


def test_no_consent_withholds_images_without_requesting_a_burst(make: Callable[..., Rig]) -> None:
    rig = make(session=_session(consent=False))
    rig.engine.set_state(AttentionState.DISTRACTED)
    result = rig.flow.on_barcode("ITEM-10")
    assert result.warn
    assert rig.engine.burst_requests == []
    assert rig.store.saved == []
    (event,) = rig.of_type(EventType.ATTENTION_EVENT)
    assert (event["images_withheld"], event["image_ids"]) == ("no_consent", [])


def test_gate_closing_after_request_withholds_at_the_sink(make: Callable[..., Rig]) -> None:
    rig = make(gate=False, session=_session(consent=False))
    rig.engine.set_state(AttentionState.DISTRACTED)
    rig.flow.on_barcode("ITEM-11")
    assert len(rig.engine.burst_requests) == 1
    assert rig.store.saved == []
    (event,) = rig.of_type(EventType.ATTENTION_EVENT)
    assert event["images_withheld"] == "no_consent"


class _RaisingGate:
    def decide(self, burst: EvidenceBurst | None = None) -> GateDecision:
        raise RuntimeError("gate bug")


def test_failing_gate_withholds(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.flow._gate = _RaisingGate()
    rig.engine.set_state(AttentionState.DISTRACTED)
    rig.flow.on_barcode("X")
    assert rig.engine.burst_requests == []
    (event,) = rig.of_type(EventType.ATTENTION_EVENT)
    assert event["images_withheld"] == GateDecision(GateReason.ERROR, None).images_withheld.value


def test_event_enqueued_when_burst_request_raises(
    make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make()
    rig.engine.set_state(AttentionState.SLEEPING)

    def boom(*_: object) -> None:
        raise RuntimeError("ring buffer bug")

    monkeypatch.setattr(rig.engine, "request_burst", boom)
    result = rig.flow.on_barcode("ITEM-12")
    (event,) = rig.of_type(EventType.ATTENTION_EVENT)
    assert event["scan_id"] == str(result.scan_id)
    assert (event["images_withheld"], event["image_ids"]) == (CAPTURE_FAILED.value, [])
    assert all(t.cancelled for t in rig.scheduler.timers)


def test_event_enqueued_when_store_raises(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.store.fail = True
    rig.engine.set_state(AttentionState.SLEEPING)
    rig.flow.on_barcode("ITEM-13")
    (event,) = rig.of_type(EventType.ATTENTION_EVENT)
    assert (event["images_withheld"], event["image_ids"]) == ("disk_full", [])


def test_missing_capture_outcome_times_out(make: Callable[..., Rig]) -> None:
    rig = make(sink=False)  # the engine never delivers the burst
    rig.engine.set_state(AttentionState.ABSENT)
    result = rig.flow.on_barcode("ITEM-14")
    assert rig.of_type(EventType.ATTENTION_EVENT) == []
    assert rig.flow.health().pending_events == 1
    (timer,) = rig.scheduler.timers
    assert timer.delay_s == ScanFlowConfig().capture_wait_s
    rig.scheduler.fire_all()
    (event,) = rig.of_type(EventType.ATTENTION_EVENT)
    assert event["scan_id"] == str(result.scan_id)
    assert event["images_withheld"] == CAPTURE_FAILED.value
    # A late outcome is not ours any more and is not queued twice.
    from ag_workstation.consent import CaptureOutcome

    assert result.event_id is not None
    late = CaptureOutcome(result.event_id, (new_uuid7(),), ImagesWithheld.NONE, GateReason.OPEN, 5)
    assert not rig.flow.claim_capture_outcome(late)
    rig.scheduler.fire_all()
    assert rig.of_type(EventType.ATTENTION_EVENT) == []


def test_close_flushes_waiting_events(make: Callable[..., Rig]) -> None:
    rig = make(sink=False)
    rig.engine.set_state(AttentionState.ABSENT)
    rig.flow.on_barcode("ITEM-15")
    rig.flow.close()
    assert len(rig.of_type(EventType.ATTENTION_EVENT)) == 1
    assert all(t.cancelled for t in rig.scheduler.timers)


def test_scheduler_and_timer_failures_are_contained(make: Callable[..., Rig]) -> None:
    rig = make()

    class BadTimer:
        def cancel(self) -> None:
            raise RuntimeError("cancel bug")

    class BadScheduler:
        calls = 0

        def schedule(self, delay_s: float, callback: Callable[[], None]) -> BadTimer:
            BadScheduler.calls += 1
            if BadScheduler.calls == 1:
                raise RuntimeError("no threads")
            return BadTimer()

    rig.flow._scheduler = BadScheduler()
    rig.engine.set_state(AttentionState.DROWSY)
    rig.flow.on_barcode("A")
    rig.flow.on_barcode("B")
    assert len(rig.of_type(EventType.ATTENTION_EVENT)) == 2


def test_episode_outcomes_are_not_ours(make: Callable[..., Rig]) -> None:
    from ag_workstation.consent import CaptureOutcome

    rig = make()
    other = CaptureOutcome(new_uuid7(), (), ImagesWithheld.NONE, GateReason.OPEN, 1)
    assert not rig.flow.claim_capture_outcome(other)


def test_event_with_bad_details_is_still_queued(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.engine.set_state(AttentionState.DROWSY)
    rig.engine._setting.signals = {"x": 1.0}
    rig.flow.on_barcode("A")
    pending_fields: dict[str, object] = {"kind": "nonsense"}  # payload that cannot validate at all
    from ag_workstation.scan.flow import _PendingEvent

    rig.flow._emit(_PendingEvent(new_uuid7(), T0, pending_fields), (), ImagesWithheld.NONE)
    assert rig.flow.health().records_lost == 1
    assert HealthFlag.RECORD_LOST in rig.flags


def test_event_with_invalid_signal_snapshot_drops_details(make: Callable[..., Rig]) -> None:
    from ag_workstation.scan.flow import _PendingEvent

    rig = make()
    fields: dict[str, object] = {
        "kind": "scan_warning",
        "state": "drowsy",
        "started_at": T0,
        "scan_id": new_uuid7(),
        "signals": "not a mapping",
    }
    rig.flow._emit(_PendingEvent(new_uuid7(), T0, fields), (), ImagesWithheld.NONE)
    (event,) = rig.of_type(EventType.ATTENTION_EVENT)
    assert event["signals"] == {}


def test_signal_sanitizing() -> None:
    long_key = "k" * 65
    cleaned = _clean_signals(
        {"ok": 0.5, "nan": float("nan"), "inf": float("inf"), "none": None, long_key: 1.0, "int": 3}
    )
    assert cleaned == {"ok": 0.5, "nan": None, "inf": None, "none": None, "int": 3.0}


# -- escalation ------------------------------------------------------------------


def test_escalation_after_threshold_within_window(make: Callable[..., Rig]) -> None:
    rig = make(config=ScanFlowConfig(escalation=EscalationConfig(threshold=3, window_s=600)))
    rig.engine.set_state(AttentionState.DISTRACTED)
    first = rig.flow.on_barcode("1")
    rig.clock.advance(60)
    rig.engine.set_state(AttentionState.ATTENTIVE)
    assert not rig.flow.on_barcode("ok").escalated  # attentive scans do not count
    rig.engine.set_state(AttentionState.DROWSY)
    second = rig.flow.on_barcode("2")
    rig.clock.advance(60)
    third = rig.flow.on_barcode("3")
    assert [first.escalated, second.escalated, third.escalated] == [False, False, True]
    assert third.message_key is ScanMessageKey.SCAN_WARNED_REPEATED
    fourth = rig.flow.on_barcode("4")
    assert not fourth.escalated  # the window starts over after escalating


def test_no_escalation_when_warnings_are_spread_out(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.engine.set_state(AttentionState.DISTRACTED)
    results = []
    for _ in range(5):
        results.append(rig.flow.on_barcode("x").escalated)
        rig.clock.advance(301)
    assert results == [False] * 5


def test_escalation_resets_on_session_change(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.engine.set_state(AttentionState.SLEEPING)
    rig.flow.on_barcode("1")
    rig.flow.on_barcode("2")
    rig.sessions.session = _session()
    assert not rig.flow.on_barcode("3").escalated
    assert not rig.flow.on_barcode("4").escalated
    assert rig.flow.on_barcode("5").escalated


def test_escalation_resets_on_logout(make: Callable[..., Rig]) -> None:
    rig = make()
    session = rig.sessions.session
    rig.engine.set_state(AttentionState.SLEEPING)
    rig.flow.on_barcode("1")
    rig.flow.on_barcode("2")
    rig.sessions.session = None
    rig.flow.on_barcode("unattributed")
    rig.sessions.session = session
    assert not rig.flow.on_barcode("3").escalated


def test_escalation_counter_unit() -> None:
    counter = EscalationCounter(EscalationConfig(threshold=2, window_s=10))
    s1, s2 = new_uuid7(), new_uuid7()
    assert counter.record_warning(s1, 0.0).count == 1
    assert counter.record_warning(s2, 1.0).count == 1  # other session: fresh
    assert counter.record_warning(s2, 20.0).count == 1  # first fell out of the window
    check = counter.record_warning(s2, 21.0)
    assert check.escalated and check.count == 2
    assert counter.count() == 0
    with pytest.raises(ValueError):
        EscalationConfig(threshold=0)
    with pytest.raises(ValueError):
        EscalationConfig(window_s=0)


# -- misc ------------------------------------------------------------------------


def test_every_message_key_has_text() -> None:
    for key in ScanMessageKey:
        assert text(key)


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        ScanFlowConfig(capture_wait_s=0)


def test_threading_scheduler_runs_and_cancels() -> None:
    ran = threading.Event()
    ThreadingScheduler().schedule(0.001, ran.set)
    assert ran.wait(1.0)
    never = threading.Event()
    ThreadingScheduler().schedule(0.05, never.set).cancel()
    assert not never.wait(0.1)


def test_bounded_query_validation() -> None:
    from ag_workstation.scan import BoundedStateQuery

    engine = FakeStationEngine()
    with pytest.raises(ValueError):
        BoundedStateQuery(engine, FakeClock(), timeout_s=0)
    with pytest.raises(ValueError):
        BoundedStateQuery(engine, FakeClock(), max_in_flight=0)
    assert BoundedStateQuery(engine, FakeClock()).in_flight == 0


def test_bounded_query_thread_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from ag_workstation.scan import BoundedStateQuery

    query = BoundedStateQuery(FakeStationEngine(), FakeClock())

    def no_threads(*_: object, **__: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)
    answer = query.ask()
    assert answer.status is QueryStatus.ERROR
    assert query.in_flight == 0
