"""Time-ordered UUIDv7 ids for device-originated records (RFC 9562).

Device-originated records use a UUIDv7 as their server-side primary key
(`docs/schema.md`), which is what makes ingest idempotent.

Layout (RFC 9562, method 1 "fixed-length dedicated counter"): 48-bit Unix
milliseconds, version 7, a 12-bit counter in `rand_a`, the RFC variant and
62 random bits. Ids from one generator are strictly increasing, also within
one millisecond and when the wall clock is stepped backwards (the generator
then keeps counting on its last timestamp). The clock is injected so tests
and replay are deterministic.

Two clock styles are accepted: `now` (timezone-aware datetimes, normally
`Clock.now`, used by the Phase 2 workstation code) and `clock_ms` (integer
Unix milliseconds, used by the Phase 1 engine). Python 3.14 has
`uuid.uuid7`; this module exists because the project targets 3.11+.
"""

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import RFC_4122, UUID

_COUNTER_MAX = 0xFFF
_COUNTER_SEED_MASK = 0x3FF  # seed low so a burst in one millisecond has room to count up


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Uuid7Generator:
    """A thread-safe, monotonic UUIDv7 source.

    `now` returns timezone-aware time; `clock_ms` returns Unix milliseconds
    (give at most one). `randbytes` returns n random bytes (normally
    `os.urandom`).
    """

    def __init__(
        self,
        now: Callable[[], datetime] | None = None,
        randbytes: Callable[[int], bytes] = os.urandom,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if now is not None and clock_ms is not None:
            raise ValueError("give either now or clock_ms, not both")
        self._now = now if now is not None else (None if clock_ms is not None else _utc_now)
        self._clock_ms = clock_ms
        self._randbytes = randbytes
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def _current_ms(self) -> int:
        if self._clock_ms is not None:
            return self._clock_ms()
        assert self._now is not None  # noqa: S101 - set in __init__ when clock_ms is None
        current = self._now()
        if current.utcoffset() is None:
            raise ValueError("the clock must return timezone-aware time")
        return int(current.timestamp() * 1000)

    def __call__(self) -> UUID:
        """Return the next id."""
        rand = int.from_bytes(self._randbytes(10), "big")
        with self._lock:
            ms = self._current_ms()
            if ms > self._last_ms:
                self._last_ms = ms
                self._counter = (rand >> 64) & _COUNTER_SEED_MASK
            else:
                # Same millisecond, or the clock went back: keep order by counting.
                self._counter += 1
                if self._counter > _COUNTER_MAX:
                    self._last_ms += 1
                    self._counter = 0
            ms, counter = self._last_ms, self._counter
        rand_b = rand & ((1 << 62) - 1)
        value = ((ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (counter << 64) | (0b10 << 62)
        return UUID(int=value | rand_b)


uuid7 = Uuid7Generator()
"""The process-wide generator."""


def new_uuid7() -> UUID:
    """A UUIDv7 from the process-wide generator and the system clock.

    Logic that needs deterministic ids takes a `Uuid7Generator` instead.
    """
    return uuid7()


def is_uuid7(value: UUID) -> bool:
    """Whether `value` is an RFC 9562 version 7 UUID."""
    return value.version == 7 and value.variant == RFC_4122


def uuid7_ms(value: UUID) -> int:
    """The Unix milliseconds embedded in a UUIDv7."""
    if value.version != 7:
        raise ValueError("not a UUIDv7")
    return value.int >> 80


def uuid7_time(value: UUID) -> datetime:
    """The UTC millisecond timestamp embedded in a UUIDv7."""
    if not is_uuid7(value):
        raise ValueError("not a UUIDv7")
    ms = value.int >> 80
    return datetime.fromtimestamp(ms / 1000, tz=UTC).replace(microsecond=(ms % 1000) * 1000)
