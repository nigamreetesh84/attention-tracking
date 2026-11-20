"""Repeated-warning escalation (scope 4.1 step 5: repeated warnings within X minutes).

Counts warned scans of one worker session in a sliding window on the
monotonic clock. When the count reaches the threshold the scan is marked
escalated and the window starts over, so a long run of warnings escalates
once per `threshold` warnings rather than on every scan. A different
session (or no session) starts from zero.
"""

import threading
from collections import deque
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EscalationConfig:
    """Default: 3 warned scans within 10 minutes."""

    threshold: int = 3
    window_s: float = 600.0

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be at least 1")
        if self.window_s <= 0:
            raise ValueError("window_s must be positive")


@dataclass(frozen=True, slots=True)
class EscalationCheck:
    """The counter's answer for one warned scan."""

    escalated: bool
    count: int
    """Warned scans of the session inside the window, this one included."""


class EscalationCounter:
    """Per-session sliding-window counter. Thread-safe."""

    def __init__(self, config: EscalationConfig | None = None) -> None:
        self._config = config or EscalationConfig()
        self._lock = threading.Lock()
        self._session: UUID | None = None
        self._times: deque[float] = deque()

    def observe_session(self, session_id: UUID | None) -> None:
        """Reset when the session changes (login, logout, replacement)."""
        with self._lock:
            if session_id != self._session:
                self._session = session_id
                self._times.clear()

    def record_warning(self, session_id: UUID, at: float) -> EscalationCheck:
        """Count a warned scan at monotonic time `at`."""
        with self._lock:
            if session_id != self._session:
                self._session = session_id
                self._times.clear()
            horizon = at - self._config.window_s
            while self._times and self._times[0] <= horizon:
                self._times.popleft()
            self._times.append(at)
            count = len(self._times)
            if count >= self._config.threshold:
                self._times.clear()
                return EscalationCheck(True, count)
            return EscalationCheck(False, count)

    def count(self) -> int:
        """Warned scans currently in the window (diagnostics)."""
        with self._lock:
            return len(self._times)
