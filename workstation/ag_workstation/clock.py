"""Injected clocks.

Logic on the workstation never reads the system time directly; it takes a
`Clock` so tests and replay can drive time deterministically. `monotonic()`
orders events and measures intervals; `now()` is the UTC wall time stamped on
records.
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """A source of monotonic seconds and UTC wall time."""

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards."""
        ...

    def now(self) -> datetime:
        """Current wall time, timezone-aware UTC."""
        ...


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        """`time.monotonic()`."""
        return time.monotonic()

    def now(self) -> datetime:
        """`datetime.now(UTC)`."""
        return datetime.now(UTC)


class FakeClock:
    """A clock that only moves when told to. Thread-safe."""

    def __init__(
        self,
        start_monotonic: float = 0.0,
        start_utc: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    ) -> None:
        if start_utc.utcoffset() != timedelta(0):
            raise ValueError("start_utc must be timezone-aware UTC")
        self._lock = threading.Lock()
        self._mono = start_monotonic
        self._utc = start_utc

    def monotonic(self) -> float:
        """Current fake monotonic seconds."""
        with self._lock:
            return self._mono

    def now(self) -> datetime:
        """Current fake UTC time."""
        with self._lock:
            return self._utc

    def advance(self, seconds: float) -> None:
        """Move both timelines forward by `seconds`."""
        if seconds < 0:
            raise ValueError("a clock cannot go backwards")
        with self._lock:
            self._mono += seconds
            self._utc += timedelta(seconds=seconds)
