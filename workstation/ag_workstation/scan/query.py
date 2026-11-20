"""The scan-state query with a hard deadline (detection-engine.md 5, engine_api rules).

`StationStateQuery.scan_state` may hang, raise anything or return garbage.
`BoundedStateQuery` runs it on a daemon thread and waits at most the
timeout; whatever goes wrong becomes `StationState.unknown` and a status. A
query that is still stuck keeps its thread; if `max_in_flight` such threads
exist, further scans do not start another one and are Unknown at once, so a
hung engine cannot pile up threads or slow scanning.
"""

import logging
import threading
from dataclasses import dataclass
from enum import StrEnum

from ag_workstation.clock import Clock
from ag_workstation.engine_api import (
    SCAN_QUERY_TIMEOUT_S,
    EngineTimeoutError,
    StationState,
    StationStateQuery,
)

logger = logging.getLogger(__name__)


class QueryStatus(StrEnum):
    """How the scan-state query ended."""

    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"
    """The engine raised (down, restarting, a bug) or answered garbage."""
    BUSY = "busy"
    """Earlier queries are still stuck; not asked."""
    SKIPPED = "skipped"
    """Not asked: no session or a scheduled break."""


@dataclass(frozen=True, slots=True)
class QueryAnswer:
    """The state to record and how it was obtained."""

    state: StationState
    status: QueryStatus


class _Call:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: StationState | None = None
        self.error: BaseException | None = None


class BoundedStateQuery:
    """Ask the engine for the scan state within `timeout_s`. Never raises."""

    def __init__(
        self,
        engine: StationStateQuery,
        clock: Clock,
        *,
        timeout_s: float = SCAN_QUERY_TIMEOUT_S,
        max_in_flight: int = 2,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be at least 1")
        self._engine = engine
        self._clock = clock
        self._timeout_s = timeout_s
        self._max_in_flight = max_in_flight
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """Queries whose thread has not returned yet."""
        with self._lock:
            return self._in_flight

    def unknown(self, status: QueryStatus) -> QueryAnswer:
        """The fail-open answer."""
        return QueryAnswer(StationState.unknown(self._clock.now(), self._clock.monotonic()), status)

    def ask(self) -> QueryAnswer:
        """The scan state, or Unknown with the reason."""
        with self._lock:
            if self._in_flight >= self._max_in_flight:
                busy = True
            else:
                busy = False
                self._in_flight += 1
        if busy:
            logger.warning("engine scan queries still stuck; scan state unknown")
            return self.unknown(QueryStatus.BUSY)
        call = _Call()
        try:
            thread = threading.Thread(
                target=self._run, args=(call,), name="ag-scan-query", daemon=True
            )
            thread.start()
        except Exception as exc:
            self._finish()
            logger.error("scan query thread failed to start: %s", type(exc).__name__)
            return self.unknown(QueryStatus.ERROR)
        if not call.done.wait(self._timeout_s):
            logger.warning("engine scan query timed out after %.0f ms", self._timeout_s * 1000)
            return self.unknown(QueryStatus.TIMEOUT)
        if call.error is not None:
            status = (
                QueryStatus.TIMEOUT
                if isinstance(call.error, EngineTimeoutError)
                else QueryStatus.ERROR
            )
            logger.warning("engine scan query failed: %s", type(call.error).__name__)
            return self.unknown(status)
        if not isinstance(call.result, StationState):
            logger.error("engine scan query returned %s", type(call.result).__name__)
            return self.unknown(QueryStatus.ERROR)
        return QueryAnswer(call.result, QueryStatus.OK)

    def _run(self, call: _Call) -> None:
        try:
            call.result = self._engine.scan_state()
        except BaseException as exc:
            # Anything from the engine is Unknown. This is a worker thread, so
            # swallowing SystemExit or KeyboardInterrupt here stops nothing.
            call.error = exc
        finally:
            self._finish()
            call.done.set()

    def _finish(self) -> None:
        with self._lock:
            self._in_flight -= 1
