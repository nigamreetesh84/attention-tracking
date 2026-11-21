"""Server clock offset (`api-contract.md` section 6, "Clock").

Every response carries `server_time`. One sample is
`server_time - midpoint(request sent, response received)` in device wall
time, so the network delay cancels to within half the round trip. Samples
with a round trip over `max_rtt_s` are ignored (too imprecise).

- `smoothed_ms`: an exponential average, reported in heartbeats and used to
  stamp records with corrected UTC (`CorrectedClock`). A jump larger than
  `reset_ms` (the device clock was stepped) restarts the average.
- `raw_ms`: the latest sample. Drift is raised while its magnitude exceeds
  2 s and cleared under 1.5 s (hysteresis, so a value hovering at the limit
  does not flap).
"""

import threading
from datetime import datetime, timedelta
from typing import Final

from ag_workstation.clock import Clock

DRIFT_LIMIT_MS: Final = 2000
DRIFT_CLEAR_MS: Final = 1500
DEFAULT_MAX_RTT_S: Final = 1.0
DEFAULT_ALPHA: Final = 0.2
DEFAULT_RESET_MS: Final = 5000


class ClockOffsetEstimator:
    """Smoothed server-minus-device offset. Thread-safe."""

    def __init__(
        self,
        *,
        alpha: float = DEFAULT_ALPHA,
        max_rtt_s: float = DEFAULT_MAX_RTT_S,
        reset_ms: int = DEFAULT_RESET_MS,
        initial_ms: int | None = None,
    ) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._max_rtt_s = max_rtt_s
        self._reset_ms = reset_ms
        self._lock = threading.Lock()
        self._smoothed: float | None = None if initial_ms is None else float(initial_ms)
        self._raw: int | None = None
        self._drift = False
        self._samples = 0

    def add_sample(self, server_time: datetime, sent_at: datetime, received_at: datetime) -> bool:
        """Feed one response; False if the sample was ignored."""
        rtt = (received_at - sent_at).total_seconds()
        if rtt < 0 or rtt > self._max_rtt_s:
            return False
        midpoint = sent_at + (received_at - sent_at) / 2
        sample = (server_time - midpoint) / timedelta(milliseconds=1)
        with self._lock:
            self._samples += 1
            self._raw = round(sample)
            if self._smoothed is None or abs(sample - self._smoothed) > self._reset_ms:
                self._smoothed = sample
            else:
                self._smoothed += self._alpha * (sample - self._smoothed)
            magnitude = abs(self._raw)
            if magnitude > DRIFT_LIMIT_MS:
                self._drift = True
            elif magnitude < DRIFT_CLEAR_MS:
                self._drift = False
        return True

    @property
    def smoothed_ms(self) -> int:
        """The smoothed offset; 0 before any sample."""
        with self._lock:
            return 0 if self._smoothed is None else round(self._smoothed)

    @property
    def samples(self) -> int:
        """How many samples were accepted (a new one means a fresh measurement)."""
        with self._lock:
            return self._samples

    @property
    def raw_ms(self) -> int | None:
        """The latest accepted sample."""
        with self._lock:
            return self._raw

    @property
    def drift(self) -> bool:
        """Whether the raw offset is beyond the 2 s limit."""
        with self._lock:
            return self._drift


class CorrectedClock:
    """A `Clock` whose `now()` is shifted by the smoothed server offset."""

    def __init__(self, base: Clock, offset: ClockOffsetEstimator) -> None:
        self._base = base
        self._offset = offset

    def monotonic(self) -> float:
        """The base clock's monotonic seconds (offsets never touch intervals)."""
        return self._base.monotonic()

    def now(self) -> datetime:
        """Base UTC plus the smoothed offset."""
        return self._base.now() + timedelta(milliseconds=self._offset.smoothed_ms)
