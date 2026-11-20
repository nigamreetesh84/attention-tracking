"""Client-generated UUIDv7 ids (RFC 9562).

Device-originated records use a UUIDv7 as their server-side primary key
(`docs/schema.md`), which is what makes ingest idempotent. Python 3.11 has no
`uuid.uuid7`, so it is built here: 48-bit Unix milliseconds, version 7,
variant 10, and random bits. Ids from one generator are strictly increasing
even within the same millisecond.
"""

import os
import threading
import time
from collections.abc import Callable
from uuid import UUID

_MAX_COUNTER = (1 << 12) - 1


class Uuid7Generator:
    """Monotonic UUIDv7 source. Thread-safe."""

    def __init__(
        self,
        clock_ms: Callable[[], int] | None = None,
        randbytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        self._rand = randbytes
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def __call__(self) -> UUID:
        """A new UUIDv7."""
        with self._lock:
            ms = self._clock_ms()
            if ms <= self._last_ms:
                # Same millisecond, or the clock went back: keep order with a counter.
                ms = self._last_ms
                self._counter += 1
                if self._counter > _MAX_COUNTER:
                    ms += 1
                    self._counter = 0
            else:
                self._counter = int.from_bytes(self._rand(2), "big") & 0x3FF
            self._last_ms = ms
            rand_b = int.from_bytes(self._rand(8), "big") & ((1 << 62) - 1)
        value = (ms & ((1 << 48) - 1)) << 80
        value |= 0x7 << 76
        value |= (self._counter & _MAX_COUNTER) << 64
        value |= 0b10 << 62
        value |= rand_b
        return UUID(int=value)


uuid7 = Uuid7Generator()
"""The process-wide generator."""


def uuid7_ms(value: UUID) -> int:
    """The Unix milliseconds embedded in a UUIDv7."""
    if value.version != 7:
        raise ValueError("not a UUIDv7")
    return value.int >> 80
