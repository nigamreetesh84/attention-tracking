"""In-memory sliding-window rate limits for `/enroll` and `/token` (contract 3.5).

Decision: in memory, per app process, not in the database. The limits are
abuse brakes, not accounting: a restart resetting them is harmless, and a
database table would add a write to every token request and needs a
schema change. With N app processes the effective limit is N times the
configured one (Compose runs one). Memory is bounded: past `max_keys`
the least recently used key is dropped, so a flood of new keys (spoofed
workstation ids) cannot grow the table and active clients stay limited.

Refused hits are not recorded, so a client hammering a closed window gets
in again once its earlier allowed hits age out.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

DEFAULT_MAX_KEYS: Final = 50_000


@dataclass(frozen=True)
class Limit:
    """At most `count` requests per `window_s` seconds."""

    count: int
    window_s: float

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("a limit allows at least one request")
        if self.window_s <= 0:
            raise ValueError("a limit window must be positive")


class SlidingWindowLimiter:
    """Thread-safe sliding-window counters keyed by string."""

    def __init__(self, monotonic: Callable[[], float], *, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._now = monotonic
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        """Keys currently tracked."""
        with self._lock:
            return len(self._hits)

    def hit(self, key: str, limit: Limit) -> float | None:
        """Record one request for `key`. None if allowed, else seconds until it would be."""
        now = self._now()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            else:
                self._hits.move_to_end(key)
            horizon = now - limit.window_s
            while hits and hits[0] <= horizon:
                hits.popleft()
            if len(hits) >= limit.count:
                return max(hits[0] + limit.window_s - now, 0.001)
            hits.append(now)
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
            return None
