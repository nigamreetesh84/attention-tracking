"""IPC client: state and health queries with a hard deadline, and an event subscriber.

`IpcClient` never raises into its caller. Every call finishes within
`timeout_s` (150 ms by default, `SCAN_QUERY_TIMEOUT_S`), and a timeout, a
refused connection, a malformed answer or any other error comes back as a
typed `CallStatus` with an Unknown state (or an engine health of down /
unknown). It also satisfies `StationStateQuery`, so the scan flow can use it
in place of an in-process engine.

Connecting can block (a frozen server never finishes the auth handshake), so
it runs on a helper thread that the call waits on only until its deadline.
"""

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ag_shared.health import EngineStatus
from pydantic import JsonValue

from ag_workstation.clock import Clock
from ag_workstation.engine_api import SCAN_QUERY_TIMEOUT_S, EngineHealth, StationState
from ag_workstation.service import protocol as p
from ag_workstation.service.transport import IpcConnection, IpcTransport

logger = logging.getLogger(__name__)


class CallStatus(StrEnum):
    """How a call ended."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    """Nobody listening, connection lost, or the engine said it is not running."""
    TIMEOUT = "timeout"
    """No answer before the deadline (client side or engine side)."""
    ERROR = "error"
    """Malformed answer, engine error or a protocol problem."""


_ERROR_STATUS: Final = {
    p.ErrorCode.UNAVAILABLE: CallStatus.UNAVAILABLE,
    p.ErrorCode.TIMEOUT: CallStatus.TIMEOUT,
}


@dataclass(frozen=True, slots=True)
class CallResult:
    """A raw call outcome: `result` is set only when `status` is OK."""

    status: CallStatus
    result: dict[str, JsonValue] | None = None
    error: p.ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class StateResult:
    """A state query outcome; `state` is Unknown unless `status` is OK."""

    status: CallStatus
    state: StationState


@dataclass(frozen=True, slots=True)
class HealthResult:
    """A health query outcome; `health` is down/unknown unless `status` is OK."""

    status: CallStatus
    health: EngineHealth


class _UnavailableError(Exception):
    pass


class _TimeoutError(Exception):
    pass


class IpcClient:
    """Request/response client. Thread-safe; calls are serialized."""

    def __init__(
        self,
        transport: IpcTransport,
        clock: Clock,
        *,
        timeout_s: float = SCAN_QUERY_TIMEOUT_S,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._transport = transport
        self._clock = clock
        self.timeout_s = timeout_s
        self._timer = timer
        self._lock = threading.Lock()
        self._conn: IpcConnection | None = None
        self._connecting: Future[IpcConnection] | None = None
        self._next_id = 0
        self._closed = False

    # -- typed queries ------------------------------------------------------

    def query_scan_state(self) -> StateResult:
        """The scan state, or Unknown with the reason."""
        return self._state(p.Op.SCAN_STATE)

    def query_current_state(self) -> StateResult:
        """The current state, or Unknown with the reason."""
        return self._state(p.Op.CURRENT_STATE)

    def scan_state(self) -> StationState:
        """`StationStateQuery.scan_state`; Unknown on any failure, never raises."""
        return self.query_scan_state().state

    def current_state(self) -> StationState:
        """`StationStateQuery.current_state`; Unknown on any failure, never raises."""
        return self.query_current_state().state

    def query_health(self) -> HealthResult:
        """Engine health; DOWN when unreachable, UNKNOWN on timeout or error."""
        res = self.call(p.Op.HEALTH)
        if res.status is CallStatus.OK and res.result is not None:
            try:
                return HealthResult(
                    CallStatus.OK, p.EngineHealthMsg.model_validate(res.result).to_health()
                )
            except ValueError:
                res = CallResult(CallStatus.ERROR)
        status = EngineStatus.DOWN if res.status is CallStatus.UNAVAILABLE else EngineStatus.UNKNOWN
        return HealthResult(res.status, EngineHealth(status=status, last_error=res.status.value))

    def health(self) -> EngineHealth:
        """`EngineHealthSource.health`; never raises."""
        return self.query_health().health

    def ping(self) -> CallStatus:
        """Round trip without touching the engine."""
        return self.call(p.Op.PING).status

    def _state(self, op: p.Op) -> StateResult:
        res = self.call(op)
        if res.status is CallStatus.OK and res.result is not None:
            try:
                state = p.StationStateMsg.model_validate(res.result).to_state()
                return StateResult(CallStatus.OK, state)
            except ValueError:
                res = CallResult(CallStatus.ERROR)
        return StateResult(
            res.status, StationState.unknown(self._clock.now(), self._clock.monotonic())
        )

    # -- raw call -----------------------------------------------------------

    def call(self, op: p.Op) -> CallResult:
        """Send one request and wait for its answer until the deadline. Never raises."""
        deadline = self._timer() + self.timeout_s
        if not self._lock.acquire(timeout=self.timeout_s):
            return CallResult(CallStatus.TIMEOUT)
        try:
            return self._call(op, deadline)
        except _TimeoutError:
            return CallResult(CallStatus.TIMEOUT)
        except (_UnavailableError, OSError, EOFError):
            self._drop()
            return CallResult(CallStatus.UNAVAILABLE)
        except Exception as exc:  # any other failure must not reach the scan path
            logger.warning("IPC call %s failed: %s", op.value, type(exc).__name__)
            self._drop()
            return CallResult(CallStatus.ERROR)
        finally:
            self._lock.release()

    def _remaining(self, deadline: float) -> float:
        left = deadline - self._timer()
        if left <= 0:
            raise _TimeoutError
        return left

    def _call(self, op: p.Op, deadline: float) -> CallResult:
        if self._closed:
            raise _UnavailableError
        conn = self._connection(deadline)
        self._next_id = (self._next_id + 1) % 2**53
        request_id = self._next_id
        conn.send_bytes(p.encode(p.Request(id=request_id, op=op)))
        while True:
            if not conn.poll(self._remaining(deadline)):
                raise _TimeoutError
            try:
                response = p.decode_response(conn.recv_bytes(p.MAX_MESSAGE_BYTES))
            except p.ProtocolError:
                self._drop()
                return CallResult(CallStatus.ERROR)
            if response.id != request_id:
                continue  # a late answer to an earlier, timed-out call
            if response.ok:
                return CallResult(CallStatus.OK, response.result)
            code = response.error
            status = CallStatus.ERROR if code is None else _ERROR_STATUS.get(code, CallStatus.ERROR)
            return CallResult(status, error=code)

    def _connection(self, deadline: float) -> IpcConnection:
        if self._conn is not None:
            return self._conn
        if self._connecting is None:
            self._connecting = self._start_connect()
        future = self._connecting
        try:
            conn = future.result(timeout=self._remaining(deadline))
        except FutureTimeout:
            raise _TimeoutError from None  # still connecting; picked up by a later call
        except _TimeoutError:
            raise
        except Exception as exc:
            self._connecting = None
            raise _UnavailableError from exc
        self._connecting = None
        self._conn = conn
        return conn

    def _start_connect(self) -> "Future[IpcConnection]":
        # A daemon thread, not an executor: a connect stuck on a frozen server
        # must not keep the process from exiting.
        future: Future[IpcConnection] = Future()

        def work() -> None:
            try:
                future.set_result(self._transport.connect())
            except BaseException as exc:  # handed to the waiting call
                future.set_exception(exc)

        threading.Thread(target=work, name="ipc-connect", daemon=True).start()
        return future

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        """Close the connection; later calls answer UNAVAILABLE."""
        with self._lock:
            self._closed = True
            self._drop()
            pending, self._connecting = self._connecting, None
        if pending is not None:
            pending.add_done_callback(_close_result)


def _close_result(future: "Future[IpcConnection]") -> None:
    if future.cancelled() or future.exception() is not None:
        return
    with contextlib.suppress(Exception):
        future.result().close()


EventHandler = Callable[[p.EventKind, p.EventData], None]


class EventSubscriber:
    """Keeps an event channel open, reconnecting after failures, and calls `on_event`.

    `on_event` runs on the subscriber thread; its exceptions are logged and
    dropped. Malformed events are counted and skipped.
    """

    def __init__(
        self,
        transport: IpcTransport,
        on_event: EventHandler,
        *,
        retry_s: float = 0.5,
        poll_s: float = 0.05,
    ) -> None:
        self._transport = transport
        self._on_event = on_event
        self._retry_s = retry_s
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = threading.Event()
        self.malformed = 0
        self.connects = 0

    def start(self) -> None:
        """Start the subscriber thread."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ipc-events", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop and join."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            conn: IpcConnection | None = None
            try:
                conn = self._transport.connect()
                self._subscribe(conn)
                self.connects += 1
                self.connected.set()
                self._pump(conn)
            except Exception as exc:  # reconnect after any failure
                logger.debug("event channel down: %s", type(exc).__name__)
            finally:
                self.connected.clear()
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
            self._stop.wait(self._retry_s)

    def _subscribe(self, conn: IpcConnection) -> None:
        conn.send_bytes(p.encode(p.Request(id=0, op=p.Op.SUBSCRIBE)))
        if not conn.poll(max(self._retry_s, 1.0)):
            raise TimeoutError("no subscribe answer")
        if not p.decode_response(conn.recv_bytes(p.MAX_MESSAGE_BYTES)).ok:
            raise ConnectionError("subscribe refused")

    def _pump(self, conn: IpcConnection) -> None:
        while not self._stop.is_set():
            if not conn.poll(self._poll_s):
                continue
            data = conn.recv_bytes(p.MAX_MESSAGE_BYTES)
            try:
                event = p.decode_event(data)
                payload = p.parse_event_data(event)
            except p.ProtocolError:
                self.malformed += 1
                continue
            try:
                self._on_event(event.event, payload)
            except Exception as exc:  # a UI bug must not kill the channel
                logger.warning("event handler failed: %s", type(exc).__name__)
