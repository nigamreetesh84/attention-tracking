"""Engine child control over IPC (2.9-pre): session push, config, bursts and outcomes."""

import contextlib
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import ImagesWithheld
from ag_shared.config import EngineConfig
from ag_shared.health import EngineStatus
from ag_shared.states import AttentionState
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.consent import CaptureOutcome, GateReason
from ag_workstation.engine.adapter import FLAG_NOT_CONFIGURED
from ag_workstation.engine.fakes import T0, engine_config
from ag_workstation.engine_api import BurstKind, ConfigApplyResult
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.service import CallStatus, IpcClient, IpcServer, PipeTransport
from ag_workstation.service import protocol as p
from ag_workstation.service.child import engine_child_main
from ag_workstation.service.engine_factory import DEFAULT_ENGINE_FACTORY
from ag_workstation.service.remote import RemoteEvidence, SessionPusher
from ag_workstation.service.station import BurstStatus, PushedSession
from pydantic import JsonValue

AUTHKEY = b"c" * 32
SESSION = UUID("0193a0b0-0000-7000-8000-0000000c1001")
USER = UUID("0193a0b0-0000-7000-8000-0000000c1002")
EVENT = UUID("0193a0b0-0000-7000-8000-0000000c1003")


def _wait(cond: Callable[[], bool], limit: float = 5.0) -> None:
    end = time.monotonic() + limit
    while not cond():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.01)


class Control:
    """A `StationControl` that records calls."""

    def __init__(self) -> None:
        self.sessions: list[tuple[PushedSession | None, float]] = []
        self.configs: list[EngineConfig] = []
        self.bursts: list[tuple[UUID, BurstKind, float]] = []
        self.status = BurstStatus("pending")
        self.refuse = False

    def set_session(self, session: PushedSession | None, ttl_s: float) -> None:
        self.sessions.append((session, ttl_s))

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        self.configs.append(config)
        if self.refuse:
            return ConfigApplyResult(False, 1, "invalid config " + "x" * 200)
        return ConfigApplyResult(True, config.config_version)

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        self.bursts.append((event_id, kind, trigger_ts))

    def burst_outcome(self, event_id: UUID) -> BurstStatus:
        return self.status


class Session:
    session_id = SESSION
    user_id = USER
    started_at = T0
    has_consent = True
    employee_id = "EMP1"  # present on the UI's session, never sent
    display_name = "A Worker"


@pytest.fixture
def transport() -> Iterator[PipeTransport]:
    path = Path(tempfile.mkdtemp(prefix="agctl"))
    if sys.platform == "win32":
        yield PipeTransport(rf"\\.\pipe\agctl-{uuid.uuid4().hex}", AUTHKEY)
    else:
        yield PipeTransport(str(path / "c.sock"), AUTHKEY)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def control() -> Control:
    return Control()


@pytest.fixture
def server(transport: PipeTransport, control: Control) -> Iterator[IpcServer]:
    srv = IpcServer(transport, FakeStationEngine(SystemClock()), control=control)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(transport: PipeTransport) -> Iterator[IpcClient]:
    c = IpcClient(transport, FakeClock(), timeout_s=1.0)
    yield c
    c.close()


def test_session_push_carries_ids_and_consent_only(
    server: IpcServer, client: IpcClient, control: Control
) -> None:
    assert client.push_session(Session(), 5.0) is CallStatus.OK
    assert client.push_session(None, 5.0) is CallStatus.OK
    (pushed, ttl), (cleared, _) = control.sessions
    assert pushed == PushedSession(SESSION, USER, T0, True) and ttl == 5.0
    assert cleared is None
    # Only these fields go over the wire: no name, employee ID or PIN.
    assert set(p.SessionMsg.model_fields) == {"session_id", "user_id", "started_at", "has_consent"}
    assert client.push_session(Session(), 0.0) is CallStatus.ERROR


def test_apply_config_round_trip(server: IpcServer, client: IpcClient, control: Control) -> None:
    config = engine_config(config_version=12)
    result = client.apply_config(config)
    assert (result.accepted, result.config_version) == (True, 12)
    assert control.configs == [config]
    control.refuse = True
    refused = client.apply_config(config)
    assert not refused.accepted and refused.reason is not None and len(refused.reason) <= 64


def test_burst_request_and_outcome(server: IpcServer, client: IpcClient, control: Control) -> None:
    assert client.send_burst_request(EVENT, BurstKind.SCAN_WARNING, 12.5) is CallStatus.OK
    assert control.bursts == [(EVENT, BurstKind.SCAN_WARNING, 12.5)]
    assert client.burst_outcome(EVENT) == BurstStatus("pending")
    image = UUID(int=9)
    control.status = BurstStatus(
        "done", CaptureOutcome(EVENT, (image,), ImagesWithheld.NONE, GateReason.OPEN, 6)
    )
    answer = client.burst_outcome(EVENT)
    assert answer is not None and answer.outcome == control.status.outcome
    control.status = BurstStatus("unknown")
    assert client.burst_outcome(EVENT) == BurstStatus("unknown")


def test_control_ops_without_a_control_handler_are_unsupported(
    transport: PipeTransport, client: IpcClient
) -> None:
    srv = IpcServer(transport, FakeStationEngine(SystemClock()))
    srv.start()
    try:
        res = client.call(p.Op.REQUEST_BURST, {"event_id": str(EVENT), "kind": "scan_warning"})
        assert (res.status, res.error) == (CallStatus.ERROR, p.ErrorCode.UNSUPPORTED)
        assert client.burst_outcome(EVENT) is None
        result = client.apply_config(engine_config(config_version=3))
        assert (result.accepted, result.reason) == (False, "unsupported")
        assert client.scan_state().state is AttentionState.ATTENTIVE  # state ops unaffected
    finally:
        srv.stop()


def test_malformed_control_arguments_are_bad_requests(server: IpcServer, control: Control) -> None:
    cases: list[tuple[p.Op, dict[str, JsonValue] | None]] = [
        (p.Op.SET_SESSION, {"session": None, "ttl_s": 999}),
        (p.Op.SET_SESSION, {"session": {"session_id": "x"}, "ttl_s": 5}),
        (p.Op.APPLY_CONFIG, {"config": {"cameras": []}}),
        (p.Op.REQUEST_BURST, {"event_id": str(EVENT), "kind": "selfie", "trigger_ts": 1.0}),
        (p.Op.REQUEST_BURST, None),
        (p.Op.BURST_OUTCOME, {"event_id": "not-a-uuid"}),
    ]
    for op, args in cases:
        raw = server.handle(p.encode(p.Request(id=3, op=op, args=args)))
        response = p.decode_response(raw)
        assert (response.ok, response.error) == (False, p.ErrorCode.BAD_REQUEST), op
    assert control.sessions == [] and control.configs == [] and control.bursts == []


def test_control_handler_errors_never_escape(server: IpcServer, control: Control) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("bug")

    control.request_burst = boom  # type: ignore[method-assign,assignment]  # inject a failure
    raw = server.handle(
        p.encode(
            p.Request(
                id=4,
                op=p.Op.REQUEST_BURST,
                args={"event_id": str(EVENT), "kind": "scan_warning", "trigger_ts": 1.0},
            )
        )
    )
    assert p.decode_response(raw).error is p.ErrorCode.INTERNAL


def test_control_calls_never_raise_without_a_server(client: IpcClient) -> None:
    assert client.push_session(None, 5.0) is CallStatus.UNAVAILABLE
    assert client.send_burst_request(EVENT, BurstKind.SCAN_WARNING, 1.0) is CallStatus.UNAVAILABLE
    assert client.burst_outcome(EVENT) is None
    result = client.apply_config(engine_config(config_version=2))
    assert (result.accepted, result.config_version, result.reason) == (False, 2, "unavailable")


def test_oversized_request_is_refused_before_sending(server: IpcServer, client: IpcClient) -> None:
    res = client.call(p.Op.PING, {"blob": "x" * (p.MAX_MESSAGE_BYTES + 1)})
    assert (res.status, res.error) == (CallStatus.ERROR, p.ErrorCode.BAD_REQUEST)
    assert client.ping() is CallStatus.OK  # the connection is still fine


def test_control_client_does_not_hold_up_the_state_client(
    transport: PipeTransport, control: Control
) -> None:
    gate = threading.Event()

    def slow_config(config: EngineConfig) -> ConfigApplyResult:
        gate.wait(2.0)
        return ConfigApplyResult(True, config.config_version)

    control.apply_config = slow_config  # type: ignore[method-assign]  # slow child
    srv = IpcServer(transport, FakeStationEngine(SystemClock()), control=control)
    srv.start()
    state_client = IpcClient(transport, FakeClock())
    control_client = IpcClient(transport, FakeClock(), timeout_s=1.0)
    try:
        assert state_client.ping() is CallStatus.OK
        worker = threading.Thread(
            target=control_client.apply_config, args=(engine_config(),), daemon=True
        )
        worker.start()
        time.sleep(0.05)
        start = time.monotonic()
        assert state_client.query_scan_state().status is CallStatus.OK
        assert time.monotonic() - start < 0.15
        gate.set()
        worker.join(3.0)
    finally:
        gate.set()
        state_client.close()
        control_client.close()
        srv.stop()


# -- UI-side adapters --------------------------------------------------------------------------


class StubControl:
    def __init__(self) -> None:
        self.requests: list[UUID] = []
        self.answers: dict[UUID, list[BurstStatus | None]] = {}
        self.send_status = CallStatus.OK

    def send_burst_request(
        self, event_id: UUID, kind: BurstKind, trigger_ts: float, *, timeout_s: float | None = None
    ) -> CallStatus:
        self.requests.append(event_id)
        return self.send_status

    def burst_outcome(
        self, event_id: UUID, *, timeout_s: float | None = None
    ) -> BurstStatus | None:
        queue = self.answers.get(event_id, [])
        return queue.pop(0) if queue else BurstStatus("pending")


def test_remote_evidence_delivers_the_outcome() -> None:
    clock = FakeClock()
    stub = StubControl()
    outcome = CaptureOutcome(EVENT, (UUID(int=1),), ImagesWithheld.NONE, GateReason.OPEN, 6)
    stub.answers[EVENT] = [BurstStatus("pending"), None, BurstStatus("done", outcome)]
    got: list[CaptureOutcome] = []
    remote = RemoteEvidence(stub, monotonic=clock.monotonic, on_outcome=got.append)
    start = time.monotonic()
    remote.request_burst(EVENT, BurstKind.SCAN_WARNING, 1.0)  # scan path: returns at once
    assert time.monotonic() - start < 0.01
    in_flight: list[object] = []
    for _ in range(3):
        remote.step(in_flight)  # type: ignore[arg-type]  # internal list
    assert got == [outcome] and in_flight == []
    assert stub.requests == [EVENT]


def test_remote_evidence_gives_up_and_counts() -> None:
    clock = FakeClock()
    stub = StubControl()
    remote = RemoteEvidence(stub, monotonic=clock.monotonic, wait_s=1.0, max_queued=1)
    remote.request_burst(EVENT, BurstKind.SCAN_WARNING, 1.0)
    remote.request_burst(UUID(int=2), BurstKind.SCAN_WARNING, 1.0)  # queue full
    assert remote.dropped == 1
    in_flight: list[object] = []
    remote.step(in_flight)  # type: ignore[arg-type]  # internal list
    clock.advance(2.0)
    remote.step(in_flight)  # type: ignore[arg-type]  # internal list
    assert remote.expired == 1 and in_flight == []
    stub.send_status = CallStatus.UNAVAILABLE
    remote.request_burst(EVENT, BurstKind.SCAN_WARNING, 1.0)
    remote.step(in_flight)  # type: ignore[arg-type]  # internal list
    assert remote.failed == 1
    stub.send_status = CallStatus.OK
    stub.answers[EVENT] = [BurstStatus("unknown")]
    remote.request_burst(EVENT, BurstKind.SCAN_WARNING, 1.0)
    remote.step(in_flight)  # type: ignore[arg-type]  # internal list
    assert remote.expired == 2


def test_remote_evidence_thread_and_failing_handler() -> None:
    stub = StubControl()
    outcome = CaptureOutcome(EVENT, (), ImagesWithheld.NO_WORKER, GateReason.NO_SESSION, 6)
    stub.answers[EVENT] = [BurstStatus("done", outcome)]

    def bad(o: CaptureOutcome) -> None:
        raise RuntimeError("bug")

    remote = RemoteEvidence(stub, monotonic=time.monotonic, poll_s=0.01)
    remote.bind(bad)
    remote.start()
    try:
        remote.request_burst(EVENT, BurstKind.SCAN_WARNING, 1.0)
        _wait(lambda: stub.requests == [EVENT] and not stub.answers[EVENT])
    finally:
        remote.close()
    assert remote.delivered == 0


def test_session_pusher_pushes_now_and_periodically() -> None:
    pushed: list[object] = []
    status = [CallStatus.OK]

    class Client:
        def push_session(
            self, session: object, ttl_s: float, *, timeout_s: float | None = None
        ) -> CallStatus:
            pushed.append(session)
            return status[0]

    class Sessions:
        current_session: object = None

        def current(self) -> object:
            return self.current_session

    sessions = Sessions()
    pusher = SessionPusher(Client(), sessions, interval_s=60.0)  # type: ignore[arg-type]  # stubs
    pusher.start()
    try:
        _wait(lambda: len(pushed) == 1)
        sessions.current_session = Session()
        pusher.push_now()
        _wait(lambda: len(pushed) == 2)
        assert isinstance(pushed[1], Session)
    finally:
        pusher.close()
    status[0] = CallStatus.UNAVAILABLE
    assert pusher.push() is CallStatus.UNAVAILABLE and pusher.failures == 1


# -- the real engine child, in process ---------------------------------------------------------


class _Counter:
    value = 0


def test_real_child_serves_state_and_control(
    transport: PipeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("AG_ENGINE_CONFIG", "AG_DATA_DIR", "AG_DATA_KEY", "AG_MODEL_DIR"):
        monkeypatch.delenv(name, raising=False)
    counter = _Counter()
    stop = threading.Event()

    def sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise SystemExit  # ends the child's beat loop

    thread = threading.Thread(
        target=lambda: _run_child(counter, transport, sleep), name="child", daemon=True
    )
    thread.start()
    client = IpcClient(transport, FakeClock(), timeout_s=1.0)
    try:
        _wait(lambda: client.ping() is CallStatus.OK)
        state = client.query_scan_state()
        assert state.status is CallStatus.OK and state.state.state is AttentionState.UNKNOWN
        health = client.query_health().health
        assert (health.status, health.last_error) == (EngineStatus.DEGRADED, FLAG_NOT_CONFIGURED)
        assert client.push_session(Session(), 5.0) is CallStatus.OK
        assert client.send_burst_request(EVENT, BurstKind.SCAN_WARNING, 1.0) is CallStatus.OK
        assert client.burst_outcome(EVENT) == BurstStatus("pending")  # no engine: never built
    finally:
        client.close()
        stop.set()
        thread.join(5.0)
    assert counter.value >= 1


def _run_child(counter: _Counter, transport: PipeTransport, sleep: Callable[[float], None]) -> None:
    with contextlib.suppress(SystemExit):
        engine_child_main(counter, DEFAULT_ENGINE_FACTORY, transport, 0.05, sleep=sleep)
