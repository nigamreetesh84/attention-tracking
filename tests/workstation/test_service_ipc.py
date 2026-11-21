"""IPC over the real transport: round trip, 150 ms timeout, server loss, malformed input, events."""

import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState
from ag_workstation.clock import FakeClock, SystemClock
from ag_workstation.engine_api import (
    CameraState,
    EngineError,
    StationState,
)
from ag_workstation.fakes import FakeStationEngine
from ag_workstation.service import (
    CallStatus,
    EventPublisher,
    EventSubscriber,
    IpcClient,
    IpcServer,
    PipeTransport,
    default_address,
)
from ag_workstation.service import protocol as p
from ag_workstation.service.transport import MIN_AUTHKEY_BYTES, IpcListener

AUTHKEY = b"k" * 32


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    # pytest's tmp_path can exceed the ~104-byte AF_UNIX path limit.
    path = Path(tempfile.mkdtemp(prefix="agipc"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def transport(sock_dir: Path) -> PipeTransport:
    if sys.platform == "win32":
        # multiprocessing on Windows has named pipes, not AF_UNIX sockets.
        return PipeTransport(rf"\\.\pipe\agipc-{uuid.uuid4().hex}", AUTHKEY)
    return PipeTransport(str(sock_dir / "e.sock"), AUTHKEY)


@pytest.fixture
def engine() -> FakeStationEngine:
    cams = (CameraState(1, CameraStatus.ONLINE, AttentionState.ATTENTIVE, 0.8, True),)
    return FakeStationEngine(SystemClock(), cameras=cams)


@pytest.fixture
def server(transport: PipeTransport, engine: FakeStationEngine) -> Iterator[IpcServer]:
    srv = IpcServer(transport, engine)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(transport: PipeTransport) -> Iterator[IpcClient]:
    c = IpcClient(transport, FakeClock())
    yield c
    c.close()


def _wait(cond: Callable[[], bool], limit: float = 2.0) -> None:
    end = time.monotonic() + limit
    while not cond():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.005)


T = TypeVar("T")


def _timed(fn: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


# -- round trip ---------------------------------------------------------------


def test_round_trip_scan_and_current_state(
    server: IpcServer, client: IpcClient, engine: FakeStationEngine
) -> None:
    engine.set_state(AttentionState.DROWSY, confidence=0.7, signals={"perclos": 0.4, "x": None})
    res = client.query_scan_state()
    assert res.status is CallStatus.OK
    assert res.state.state is AttentionState.DROWSY
    assert res.state.confidence == 0.7
    assert dict(res.state.signals) == {"perclos": 0.4, "x": None}
    assert res.state.camera_states[0].usable
    assert res.state.engine_version == "fake-engine-1"
    assert client.current_state().state is AttentionState.DROWSY
    assert client.scan_state().state is AttentionState.DROWSY
    assert (engine.scan_queries, engine.current_queries) == (2, 1)
    assert client.ping() is CallStatus.OK


def test_round_trip_health(server: IpcServer, client: IpcClient) -> None:
    res = client.query_health()
    assert res.status is CallStatus.OK
    assert res.health.status is EngineStatus.RUNNING
    assert res.health.cameras[0].fps == 12.0
    assert client.health().monitoring_available


def test_round_trip_is_fast(server: IpcServer, client: IpcClient) -> None:
    client.ping()  # connect
    _, elapsed = _timed(client.query_scan_state)
    assert elapsed < 0.05


def test_engine_errors_map_to_typed_results(
    server: IpcServer, client: IpcClient, engine: FakeStationEngine
) -> None:
    engine.fail_next()
    engine.timeout_next()
    engine.script(EngineError("bad"), ZeroDivisionError())
    statuses = [client.query_scan_state().status for _ in range(4)]
    assert statuses == [
        CallStatus.UNAVAILABLE,
        CallStatus.TIMEOUT,
        CallStatus.ERROR,
        CallStatus.ERROR,
    ]
    assert client.query_scan_state().status is CallStatus.OK  # connection kept
    engine.go_down()
    res = client.query_current_state()
    assert res.status is CallStatus.UNAVAILABLE
    assert res.state.state is AttentionState.UNKNOWN
    assert client.query_health().health.status is EngineStatus.DOWN  # engine says so


# -- timeouts -------------------------------------------------------------------


def test_slow_engine_times_out_at_150ms(
    server: IpcServer, client: IpcClient, engine: FakeStationEngine
) -> None:
    client.ping()
    engine.hang_next(0.3)
    res, elapsed = _timed(client.query_scan_state)
    assert res.status is CallStatus.TIMEOUT
    assert res.state.state is AttentionState.UNKNOWN
    assert 0.14 <= elapsed < 0.25
    # The late answer to the timed-out call is discarded, not mistaken for a new one.
    engine.set_state(AttentionState.DISTRACTED)
    _wait(lambda: client.query_scan_state().status is CallStatus.OK)
    assert client.query_scan_state().state.state is AttentionState.DISTRACTED


def test_timeout_is_configurable(
    server: IpcServer, transport: PipeTransport, engine: FakeStationEngine
) -> None:
    client = IpcClient(transport, FakeClock(), timeout_s=0.05)
    try:
        client.ping()
        engine.hang_next(0.15)
        res, elapsed = _timed(client.query_scan_state)
        assert res.status is CallStatus.TIMEOUT
        assert elapsed < 0.12
    finally:
        client.close()
    with pytest.raises(ValueError):
        IpcClient(transport, FakeClock(), timeout_s=0)


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX socket behaviour (POSIX only)")
def test_frozen_server_handshake_times_out(sock_dir: Path) -> None:
    """A listener that never answers the auth challenge (frozen process)."""
    path = str(sock_dir / "frozen.sock")
    raw = socket.socket(getattr(socket, "AF_UNIX"), socket.SOCK_STREAM)  # noqa: B009 - POSIX only
    raw.bind(path)
    raw.listen(4)
    client = IpcClient(PipeTransport(path, AUTHKEY), FakeClock())
    try:
        res, elapsed = _timed(client.query_scan_state)
        assert res.status is CallStatus.TIMEOUT
        assert elapsed < 0.25
        health, elapsed = _timed(client.query_health)  # still connecting: times out again
        assert health.status is CallStatus.TIMEOUT
        assert health.health.status is EngineStatus.UNKNOWN
        assert elapsed < 0.25
    finally:
        client.close()
        raw.close()


def test_concurrent_callers_never_exceed_deadline(
    server: IpcServer, client: IpcClient, engine: FakeStationEngine
) -> None:
    client.ping()
    engine.hang_next(0.2)
    results: list[tuple[CallStatus, float]] = []

    def call() -> None:
        res, elapsed = _timed(client.query_scan_state)
        results.append((res.status, elapsed))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(elapsed < 0.25 for _, elapsed in results)
    assert CallStatus.TIMEOUT in {s for s, _ in results}


# -- server loss and recovery -----------------------------------------------------


def test_no_server_is_unavailable(client: IpcClient) -> None:
    res, elapsed = _timed(client.query_scan_state)
    assert res.status is CallStatus.UNAVAILABLE
    assert res.state.state is AttentionState.UNKNOWN
    assert elapsed < 0.25
    assert client.query_health().health.status is EngineStatus.DOWN


def test_server_crash_gives_unavailable_then_recovers(
    transport: PipeTransport, engine: FakeStationEngine, client: IpcClient
) -> None:
    srv = IpcServer(transport, engine)
    srv.start()
    srv.start()  # idempotent
    assert srv.running
    assert client.ping() is CallStatus.OK
    srv.stop()
    srv.stop()  # idempotent
    assert not srv.running
    res, elapsed = _timed(client.query_scan_state)
    assert res.status is CallStatus.UNAVAILABLE
    assert elapsed < 0.25
    assert client.query_scan_state().status is CallStatus.UNAVAILABLE
    again = IpcServer(transport, engine)
    again.start()
    try:
        assert client.query_scan_state().status is CallStatus.OK
    finally:
        again.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX socket behaviour (POSIX only)")
def test_stale_socket_file_is_replaced(transport: PipeTransport, engine: FakeStationEngine) -> None:
    stale = socket.socket(getattr(socket, "AF_UNIX"), socket.SOCK_STREAM)  # noqa: B009 - POSIX only
    stale.bind(transport.address)  # a crashed engine leaves its socket file behind
    stale.close()
    srv = IpcServer(transport, engine)
    srv.start()
    srv.stop()


def test_wrong_authkey_is_refused(
    server: IpcServer, transport: PipeTransport, engine: FakeStationEngine
) -> None:
    intruder = IpcClient(PipeTransport(transport.address, b"x" * 32), FakeClock())
    try:
        assert intruder.query_scan_state().status is CallStatus.UNAVAILABLE
    finally:
        intruder.close()
    assert engine.scan_queries == 0
    good = IpcClient(transport, FakeClock())  # the server keeps accepting
    try:
        assert good.ping() is CallStatus.OK
    finally:
        good.close()


def test_closed_client_is_unavailable(server: IpcServer, client: IpcClient) -> None:
    assert client.ping() is CallStatus.OK
    client.close()
    assert client.ping() is CallStatus.UNAVAILABLE


def test_client_limit(transport: PipeTransport, engine: FakeStationEngine) -> None:
    srv = IpcServer(transport, engine, max_clients=1)
    srv.start()
    first, second = IpcClient(transport, FakeClock()), IpcClient(transport, FakeClock())
    try:
        assert first.ping() is CallStatus.OK
        assert second.ping() is CallStatus.UNAVAILABLE
    finally:
        first.close()
        second.close()
        srv.stop()


# -- malformed messages -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'{"v": 1, "id": 1, "op": "format_disk"}',
        b'{"v": 2, "id": 1, "op": "ping"}',
        b'{"v": 1, "id": -5, "op": "ping"}',
        b'{"v": 1, "op": "ping"}',
    ],
)
def test_server_answers_malformed_requests_with_bad_request(
    server: IpcServer, transport: PipeTransport, raw: bytes
) -> None:
    conn = transport.connect()
    try:
        conn.send_bytes(raw)
        assert conn.poll(1.0)
        response = p.decode_response(conn.recv_bytes())
        assert (response.ok, response.id, response.error) == (False, None, p.ErrorCode.BAD_REQUEST)
        conn.send_bytes(p.encode(p.Request(id=9, op=p.Op.PING)))  # connection still usable
        assert conn.poll(1.0)
        assert p.decode_response(conn.recv_bytes()).id == 9
    finally:
        conn.close()
    assert server.bad_requests == 1


def test_oversized_request_drops_only_that_connection(
    server: IpcServer, transport: PipeTransport, client: IpcClient
) -> None:
    conn = transport.connect()
    try:
        try:
            conn.send_bytes(b"x" * (p.MAX_MESSAGE_BYTES + 1))
        except BrokenPipeError:
            # Windows named pipes: the server drops the connection while the
            # oversized message is still being written, so the write fails.
            pass
        else:
            assert conn.poll(1.0)
            with pytest.raises((EOFError, OSError)):
                conn.recv_bytes()
    finally:
        conn.close()
    assert client.ping() is CallStatus.OK


def test_handle_is_pure_dispatch(server: IpcServer) -> None:
    reply = p.decode_response(server.handle(p.encode(p.Request(id=3, op=p.Op.SCAN_STATE))))
    assert reply.ok and reply.id == 3
    assert reply.result is not None
    assert p.StationStateMsg.model_validate(reply.result).state is AttentionState.ATTENTIVE
    bad = p.decode_response(server.handle(b"{"))
    assert bad.error is p.ErrorCode.BAD_REQUEST


class ScriptedConnection:
    """A client-side connection that answers with scripted frames."""

    def __init__(self, replies: list[bytes | BaseException]) -> None:
        self.replies = replies
        self.sent: list[bytes] = []
        self.closed = False

    def send_bytes(self, buf: bytes) -> None:
        self.sent.append(buf)

    def poll(self, timeout: float | None = 0.0) -> bool:
        return bool(self.replies)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class ScriptedTransport:
    def __init__(self, conn: ScriptedConnection | BaseException) -> None:
        self.conn = conn

    def listen(self) -> IpcListener:
        raise NotImplementedError

    def connect(self) -> ScriptedConnection:
        if isinstance(self.conn, BaseException):
            raise self.conn
        return self.conn


def _state_reply(request_id: int, **overrides: object) -> bytes:
    state = StationState(
        AttentionState.ATTENTIVE, 0.9, False, datetime(2026, 1, 1, tzinfo=UTC), 1.0, "e", 1
    )
    result = p.StationStateMsg.of(state).model_dump(mode="json") | overrides
    return p.encode(p.Response(id=request_id, ok=True, result=result))


@pytest.mark.parametrize(
    ("replies", "status"),
    [
        ([b"garbage"], CallStatus.ERROR),
        ([b'{"v": 1, "id": 1, "ok": true}'], CallStatus.ERROR),  # ok without result
        ([b'{"v": 1, "id": 1, "ok": false}'], CallStatus.ERROR),  # failed without code
        ([_state_reply(1, state="levitating")], CallStatus.ERROR),
        ([_state_reply(1, at="2026-01-01T05:30:00+05:30")], CallStatus.ERROR),  # not UTC
        ([_state_reply(1, confidence=1.5)], CallStatus.ERROR),
        ([_state_reply(7), _state_reply(1)], CallStatus.OK),  # stale id skipped
        ([p.encode(p.failed(1, p.ErrorCode.BAD_REQUEST))], CallStatus.ERROR),
        ([p.encode(p.failed(1, p.ErrorCode.INTERNAL))], CallStatus.ERROR),
        ([p.encode(p.failed(1, p.ErrorCode.TIMEOUT))], CallStatus.TIMEOUT),
        ([EOFError()], CallStatus.UNAVAILABLE),
        ([RuntimeError("weird")], CallStatus.ERROR),
        ([], CallStatus.TIMEOUT),
    ],
)
def test_client_turns_bad_answers_into_typed_results(
    replies: list[bytes | BaseException], status: CallStatus
) -> None:
    conn = ScriptedConnection(list(replies))
    client = IpcClient(ScriptedTransport(conn), FakeClock(), timeout_s=0.05)
    res = client.query_scan_state()
    assert res.status is status
    if status is not CallStatus.OK:
        assert res.state.state is AttentionState.UNKNOWN
    assert p.decode_request(conn.sent[0]).op is p.Op.SCAN_STATE


def test_client_bad_health_answer_is_error() -> None:
    reply = p.encode(p.Response(id=1, ok=True, result={"status": "running", "restarts": -1}))
    client = IpcClient(ScriptedTransport(ScriptedConnection([reply])), FakeClock())
    res = client.query_health()
    assert res.status is CallStatus.ERROR
    assert res.health.status is EngineStatus.UNKNOWN


def test_client_connect_failure_is_unavailable() -> None:
    client = IpcClient(ScriptedTransport(PermissionError()), FakeClock())
    assert client.ping() is CallStatus.UNAVAILABLE


def test_client_send_failure_is_unavailable_and_reconnects() -> None:
    conn = ScriptedConnection([])

    def broken(buf: bytes) -> None:
        raise BrokenPipeError

    conn.send_bytes = broken  # type: ignore[method-assign]  # inject a fault
    client = IpcClient(ScriptedTransport(conn), FakeClock())
    assert client.ping() is CallStatus.UNAVAILABLE
    assert conn.closed


# -- events ---------------------------------------------------------------------------


def test_events_reach_subscriber(transport: PipeTransport) -> None:
    publisher = EventPublisher()
    publisher.state_changed(_any_state())  # before bind: dropped quietly
    engine = FakeStationEngine(SystemClock(), listener=publisher)
    srv = IpcServer(transport, engine)
    publisher.bind(srv)
    srv.start()
    got: list[tuple[p.EventKind, p.EventData]] = []
    sub = EventSubscriber(transport, lambda kind, data: got.append((kind, data)), retry_s=0.02)
    sub.start()
    try:
        _wait(lambda: srv.subscriber_count == 1)
        publisher.state_changed(_any_state())
        episode = engine.open_episode(AttentionState.SLEEPING)
        engine.close_episode()
        engine.close_bucket(
            datetime(2026, 1, 1, 0, 1, tzinfo=UTC), {AttentionState.ATTENTIVE: 60.0}
        )
        _wait(lambda: len(got) == 4)
    finally:
        sub.stop()
        srv.stop()
    kinds = [k for k, _ in got]
    assert kinds == [
        p.EventKind.STATE_CHANGED,
        p.EventKind.EPISODE_OPENED,
        p.EventKind.EPISODE_CLOSED,
        p.EventKind.BUCKET_CLOSED,
    ]
    opened = got[1][1]
    assert isinstance(opened, p.EpisodeMsg)
    assert opened.id == str(episode.id)
    assert isinstance(got[2][1], p.EpisodeMsg)
    assert got[2][1].ended_at is not None


def test_subscriber_skips_malformed_events_and_survives_handler_errors(
    server: IpcServer, transport: PipeTransport
) -> None:
    got: list[p.EventKind] = []

    def handler(kind: p.EventKind, data: p.EventData) -> None:
        got.append(kind)
        if len(got) == 1:
            raise RuntimeError("UI bug")

    sub = EventSubscriber(transport, handler, retry_s=0.02)
    sub.start()
    try:
        _wait(lambda: server.subscriber_count == 1)
        server.publish(p.Event(event=p.EventKind.STATE_CHANGED, data={"state": "nope"}))
        server.publish(p.state_event(_any_state()))
        server.publish(p.state_event(_any_state()))
        _wait(lambda: len(got) == 2)
    finally:
        sub.stop()
    assert sub.malformed == 1


def test_subscriber_reconnects_after_server_restart(
    transport: PipeTransport, engine: FakeStationEngine
) -> None:
    srv = IpcServer(transport, engine)
    srv.start()
    got: list[p.EventKind] = []
    sub = EventSubscriber(transport, lambda kind, _: got.append(kind), retry_s=0.02)
    sub.start()
    try:
        _wait(lambda: srv.subscriber_count == 1)
        srv.stop()
        _wait(lambda: not sub.connected.is_set())
        srv = IpcServer(transport, engine)
        srv.start()
        _wait(lambda: srv.subscriber_count == 1)
        srv.publish(p.state_event(_any_state()))
        _wait(lambda: got == [p.EventKind.STATE_CHANGED])
        assert sub.connects == 2
    finally:
        sub.stop()
        srv.stop()


def test_publish_drops_when_backlog_full(
    transport: PipeTransport, engine: FakeStationEngine
) -> None:
    srv = IpcServer(transport, engine, event_backlog=1)  # not started: nobody drains
    srv.publish(p.state_event(_any_state()))
    srv.publish(p.state_event(_any_state()))
    assert srv.dropped_events == 1


def test_publisher_never_raises() -> None:
    class Exploding(IpcServer):
        def publish(self, event: p.Event) -> None:
            raise RuntimeError("full")

    pub = EventPublisher(Exploding(PipeTransport("x", AUTHKEY), FakeStationEngine()))
    pub.state_changed(_any_state())


def test_dead_subscriber_is_dropped(server: IpcServer, transport: PipeTransport) -> None:
    conn = transport.connect()
    conn.send_bytes(p.encode(p.Request(id=1, op=p.Op.SUBSCRIBE)))
    assert conn.poll(1.0)
    assert p.decode_response(conn.recv_bytes()).ok
    _wait(lambda: server.subscriber_count == 1)
    conn.close()
    deadline = time.monotonic() + 2.0
    while server.subscriber_count and time.monotonic() < deadline:
        server.publish(p.state_event(_any_state()))
        time.sleep(0.01)
    assert server.subscriber_count == 0


# -- transport ------------------------------------------------------------------------


def test_transport_rules() -> None:
    with pytest.raises(ValueError):
        PipeTransport("x", b"short")
    t = PipeTransport("\\\\.\\pipe\\AttentionGuard-engine", b"k" * MIN_AUTHKEY_BYTES)
    assert t.family == "AF_PIPE"
    assert "k" * 16 not in repr(t)
    if sys.platform == "win32":
        assert default_address("ui") == "\\\\.\\pipe\\AttentionGuard-ui"
    else:
        assert default_address("ui").endswith("attentionguard-ui.sock")


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX socket behaviour (POSIX only)")
def test_transport_does_not_unlink_regular_files(sock_dir: Path) -> None:
    path = sock_dir / "notasocket"
    path.write_text("keep")
    with pytest.raises(OSError):
        PipeTransport(str(path), AUTHKEY).listen()
    assert path.read_text() == "keep"


def _any_state() -> StationState:
    return StationState(
        AttentionState.ATTENTIVE, 0.9, False, datetime(2026, 1, 1, tzinfo=UTC), 1.0, "e", 1
    )


# -- 2.R H1: a half-open connection must not block the others --------------------


def test_half_open_connection_does_not_block_new_clients(
    transport: PipeTransport, engine: FakeStationEngine
) -> None:
    """A local process that connects and never answers the auth challenge used to
    hold the single accept thread forever, so every later client timed out."""
    from multiprocessing.connection import Client

    server = IpcServer(transport, engine, handshake_timeout_s=0.3)
    server.start()
    silent = Client(transport.address, family=transport.family)  # no authkey: never answers
    try:
        client = IpcClient(transport, FakeClock(), timeout_s=1.0)
        try:
            res, elapsed = _timed(client.query_scan_state)
            assert res.status is CallStatus.OK
            assert elapsed < 0.9
        finally:
            client.close()
        # The silent connection is dropped once its handshake time is up.
        assert silent.poll(2.0)
        silent.recv_bytes()  # the challenge
        deadline = time.monotonic() + 2.0
        with pytest.raises((EOFError, OSError)):
            while time.monotonic() < deadline:
                if silent.poll(0.05):
                    silent.recv_bytes()
            raise AssertionError("server kept the half-open connection")
    finally:
        silent.close()
        server.stop()


def test_wrong_key_still_refused_with_deferred_handshake(
    transport: PipeTransport, server: IpcServer
) -> None:
    other = PipeTransport(transport.address, b"z" * 32)
    client = IpcClient(other, FakeClock(), timeout_s=0.5)
    try:
        assert client.ping() is not CallStatus.OK
    finally:
        client.close()
    good = IpcClient(transport, FakeClock(), timeout_s=0.5)
    try:
        assert good.ping() is CallStatus.OK
    finally:
        good.close()
