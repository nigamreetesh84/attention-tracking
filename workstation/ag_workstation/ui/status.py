"""Non-blocking polling of the station state and engine health.

`StatusPoller.poll()` is called from the UI tick. It never calls the source
itself: it hands one query job to a `Runner` when the previous one has
finished and the interval has passed, and returns the latest finished
reading. A source that hangs keeps its job (and the runner thread) busy, the
reading goes stale, and the presenter shows "Monitoring unavailable"; the
tick itself stays fast.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from ag_workstation.clock import Clock
from ag_workstation.engine_api import EngineHealth, StationState
from ag_workstation.ui.runner import Runner

logger = logging.getLogger(__name__)


class StatusSource(Protocol):
    """Current state and health (`IpcClient` or an in-process engine)."""

    def current_state(self) -> StationState:
        """The current station state; may raise or hang."""
        ...

    def health(self) -> EngineHealth:
        """Engine health; may raise or hang."""
        ...


@dataclass(frozen=True, slots=True)
class StatusReading:
    """One finished poll. A field is None when its call raised."""

    state: StationState | None
    health: EngineHealth | None
    at: float
    """Monotonic seconds when the poll finished."""


class StatusPoller:
    """Polls a `StatusSource` on a runner; at most one poll in flight."""

    def __init__(
        self, source: StatusSource, runner: Runner, clock: Clock, *, interval_s: float = 0.5
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._source = source
        self._runner = runner
        self._clock = clock
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._latest: StatusReading | None = None
        self._in_flight = False
        self._next_at = clock.monotonic()

    @property
    def in_flight(self) -> bool:
        """A poll was started and has not finished."""
        with self._lock:
            return self._in_flight

    @property
    def latest(self) -> StatusReading | None:
        """The last finished reading."""
        with self._lock:
            return self._latest

    def poll(self) -> StatusReading | None:
        """Start a poll if one is due, and return the latest reading. Never blocks."""
        now = self._clock.monotonic()
        with self._lock:
            due = not self._in_flight and now >= self._next_at
            if due:
                self._in_flight = True
                self._next_at = now + self._interval_s
        if due:
            try:
                self._runner.submit(self._query)
            except Exception as exc:
                logger.error("status poll not started: %s", type(exc).__name__)
                with self._lock:
                    self._in_flight = False
        return self.latest

    def _query(self) -> None:
        state: StationState | None = None
        health: EngineHealth | None = None
        try:
            state = self._source.current_state()
            if not isinstance(state, StationState):
                state = None
        except Exception as exc:
            logger.warning("state poll failed: %s", type(exc).__name__)
        try:
            health = self._source.health()
            if not isinstance(health, EngineHealth):
                health = None
        except Exception as exc:
            logger.warning("health poll failed: %s", type(exc).__name__)
        reading = StatusReading(state, health, self._clock.monotonic())
        with self._lock:
            self._latest = reading
            self._in_flight = False
