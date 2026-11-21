"""IPC server: answers state and health queries and pushes events.

Runs next to the engine (in the engine child process), so a query is a
memory read. Each connection gets a handler thread; a connection that sends
`subscribe` becomes an event channel. Events are handed off through a bounded
queue so the engine thread that publishes never blocks on a slow client.
"""

import contextlib
import logging
import queue
import threading
from collections.abc import Callable
from typing import Final, Protocol

from ag_workstation.engine_api import (
    EngineError,
    EngineHealthSource,
    EngineTimeoutError,
    EngineUnavailableError,
    StateBucket,
    StateEpisode,
    StationState,
    StationStateQuery,
)
from ag_workstation.service import protocol as p
from ag_workstation.service.station import PushedSession, StationControl
from ag_workstation.service.transport import IpcConnection, IpcListener, IpcTransport

logger = logging.getLogger(__name__)

EVENT_BACKLOG: Final = 256
_POLL_S: Final = 0.05


class StateAndHealth(StationStateQuery, EngineHealthSource, Protocol):
    """What the server dispatches to."""


def _error_code(exc: BaseException) -> p.ErrorCode:
    if isinstance(exc, EngineUnavailableError):
        return p.ErrorCode.UNAVAILABLE
    if isinstance(exc, EngineTimeoutError):
        return p.ErrorCode.TIMEOUT
    if isinstance(exc, EngineError):
        return p.ErrorCode.ENGINE_ERROR
    return p.ErrorCode.INTERNAL


class IpcServer:
    """Serves one engine over a transport. `start`/`stop` are idempotent."""

    def __init__(
        self,
        transport: IpcTransport,
        engine: StateAndHealth,
        *,
        control: StationControl | None = None,
        max_clients: int = 16,
        event_backlog: int = EVENT_BACKLOG,
    ) -> None:
        self._transport = transport
        self._engine = engine
        self._control = control
        self._max_clients = max_clients
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._listener: IpcListener | None = None
        self._threads: list[threading.Thread] = []
        self._clients: set[IpcConnection] = set()
        self._subscribers: list[IpcConnection] = []
        self._events: queue.Queue[p.Event] = queue.Queue(event_backlog)
        self.dropped_events = 0
        self.bad_requests = 0

    # -- dispatch (pure, also used directly by tests) ---------------------

    def handle(self, data: bytes) -> bytes:
        """Answer one encoded request. Never raises."""
        try:
            request = p.decode_request(data)
        except p.ProtocolError:
            self.bad_requests += 1
            return p.encode(p.failed(None, p.ErrorCode.BAD_REQUEST))
        return p.encode(self._dispatch(request))

    def _dispatch(self, request: p.Request) -> p.Response:
        try:
            match request.op:
                case p.Op.SCAN_STATE:
                    return p.ok(request.id, p.StationStateMsg.of(self._engine.scan_state()))
                case p.Op.CURRENT_STATE:
                    return p.ok(request.id, p.StationStateMsg.of(self._engine.current_state()))
                case p.Op.HEALTH:
                    return p.ok(request.id, p.EngineHealthMsg.of(self._engine.health()))
                case p.Op.PING | p.Op.SUBSCRIBE:
                    return p.ok(request.id, {})
                case _:
                    return self._dispatch_control(request)
        except p.ProtocolError:
            self.bad_requests += 1
            return p.failed(request.id, p.ErrorCode.BAD_REQUEST)
        except Exception as exc:  # every engine failure becomes an error code
            code = _error_code(exc)
            if code is p.ErrorCode.INTERNAL:
                logger.warning("IPC %s failed: %s", request.op.value, type(exc).__name__)
            return p.failed(request.id, code)

    def _dispatch_control(self, request: p.Request) -> p.Response:
        control = self._control
        if control is None:
            return p.failed(request.id, p.ErrorCode.UNSUPPORTED)
        match request.op:
            case p.Op.SET_SESSION:
                args = p.parse_args(p.SetSessionArgs, request)
                s = args.session
                pushed = (
                    None
                    if s is None
                    else PushedSession(s.session_id, s.user_id, s.started_at, s.has_consent)
                )
                control.set_session(pushed, args.ttl_s)
                return p.ok(request.id, {})
            case p.Op.APPLY_CONFIG:
                config = p.parse_args(p.ApplyConfigArgs, request).config
                result = control.apply_config(config)
                return p.ok(
                    request.id,
                    p.ConfigResultMsg(
                        accepted=result.accepted,
                        config_version=result.config_version,
                        reason=None if result.reason is None else result.reason[:64],
                    ),
                )
            case p.Op.REQUEST_BURST:
                burst = p.parse_args(p.BurstRequestArgs, request)
                control.request_burst(burst.event_id, burst.kind, burst.trigger_ts)
                return p.ok(request.id, {})
            case p.Op.BURST_OUTCOME:
                event_id = p.parse_args(p.BurstOutcomeArgs, request).event_id
                status = control.burst_outcome(event_id)
                outcome = status.outcome
                if outcome is None:
                    return p.ok(request.id, p.BurstOutcomeMsg(status=status.status))
                return p.ok(
                    request.id,
                    p.BurstOutcomeMsg(
                        status=status.status,
                        image_ids=outcome.image_ids[:64],
                        images_withheld=outcome.images_withheld,
                        gate_reason=outcome.gate_reason,
                        frames=min(outcome.frames, 64),
                    ),
                )
        return p.failed(request.id, p.ErrorCode.BAD_REQUEST)

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the server is accepting connections."""
        return self._listener is not None and not self._stop.is_set()

    def start(self) -> None:
        """Bind and start the accept and event threads."""
        if self._listener is not None:
            return
        self._listener = self._transport.listen()
        for target, name in ((self._accept_loop, "ipc-accept"), (self._event_loop, "ipc-events")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 1.0) -> None:
        """Stop accepting, close every connection and join the threads."""
        if self._listener is None or self._stop.is_set():
            return
        self._stop.set()
        # accept() does not wake on close() everywhere; a throwaway connection
        # does. It runs on its own thread: if the accept loop has already
        # exited, nobody answers its handshake until the listener closes.
        threading.Thread(target=self._wake_accept, name="ipc-wake", daemon=True).start()
        self._threads[0].join(timeout)
        with contextlib.suppress(Exception):
            self._listener.close()
        with self._lock:
            conns = [*self._clients, *self._subscribers]
            self._clients.clear()
            self._subscribers.clear()
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.close()
        self._threads[1].join(timeout)

    def _wake_accept(self) -> None:
        with contextlib.suppress(Exception):
            self._transport.connect().close()

    # -- threads ------------------------------------------------------------

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                conn = listener.accept()
            except Exception as exc:  # bad handshakes must not end the loop
                if self._stop.is_set():
                    return
                logger.info("IPC connection refused: %s", type(exc).__name__)
                continue
            if self._stop.is_set():
                conn.close()
                return
            with self._lock:
                if len(self._clients) + len(self._subscribers) >= self._max_clients:
                    conn.close()
                    logger.warning("IPC connection refused: too many clients")
                    continue
                self._clients.add(conn)
            thread = threading.Thread(
                target=self._serve, args=(conn,), name="ipc-conn", daemon=True
            )
            thread.start()

    def _serve(self, conn: IpcConnection) -> None:
        try:
            while not self._stop.is_set():
                if not conn.poll(_POLL_S):
                    continue
                data = conn.recv_bytes(p.MAX_MESSAGE_BYTES)
                try:
                    request = p.decode_request(data)
                except p.ProtocolError:
                    self.bad_requests += 1
                    conn.send_bytes(p.encode(p.failed(None, p.ErrorCode.BAD_REQUEST)))
                    continue
                conn.send_bytes(p.encode(self._dispatch(request)))
                if request.op is p.Op.SUBSCRIBE:
                    with self._lock:
                        self._clients.discard(conn)
                        if not self._stop.is_set():
                            self._subscribers.append(conn)
                    return
        except (OSError, EOFError):
            pass  # client went away or sent an oversized frame
        except Exception as exc:  # one bad connection must not hurt others
            logger.warning("IPC connection failed: %s", type(exc).__name__)
        with self._lock:
            self._clients.discard(conn)
        with contextlib.suppress(Exception):
            conn.close()

    def _event_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._events.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            data = p.encode(event)
            with self._lock:
                subscribers = list(self._subscribers)
            for conn in subscribers:
                try:
                    conn.send_bytes(data)
                except Exception:  # drop the subscriber, keep the others
                    with self._lock:
                        if conn in self._subscribers:
                            self._subscribers.remove(conn)
                    with contextlib.suppress(Exception):
                        conn.close()

    # -- events -------------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        """Connected event subscribers."""
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: p.Event) -> None:
        """Queue an event for every subscriber. Never blocks; drops when full."""
        try:
            self._events.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1


class EventPublisher:
    """An `EngineEventListener` (plus state changes) that publishes over IPC.

    May be created before its server (the engine needs its listener first);
    events published before `bind` are dropped.
    """

    def __init__(self, server: IpcServer | None = None) -> None:
        self._server = server

    def bind(self, server: IpcServer) -> None:
        """Attach the server events go to."""
        self._server = server

    def _publish(self, build: Callable[[], p.Event]) -> None:
        server = self._server
        if server is None:
            return
        try:
            server.publish(build())
        except Exception as exc:  # called on an engine thread; never raise
            logger.warning("event not published: %s", type(exc).__name__)

    def state_changed(self, state: StationState) -> None:
        """The station state changed."""
        self._publish(lambda: p.state_event(state))

    def episode_opened(self, episode: StateEpisode) -> None:
        """An episode opened."""
        self._publish(lambda: p.episode_event(episode))

    def episode_closed(self, episode: StateEpisode) -> None:
        """An episode closed."""
        self._publish(lambda: p.episode_event(episode))

    def bucket_closed(self, bucket: StateBucket) -> None:
        """A minute bucket closed."""
        self._publish(lambda: p.bucket_event(bucket))
