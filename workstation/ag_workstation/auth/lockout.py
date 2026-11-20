"""Per-subject login lockout on this station (`api-contract.md` section 6).

5 failed attempts lock that employee ID on this station for 5 minutes.
Failures count within a window that starts at the first failure (15 min by
default), so occasional typos spread over days never add up to a lock; a
success or the end of a lock resets the count. State lives in
`AuthStateStore`, so a restart does not reset it. Times are UTC wall time
(the monotonic clock does not survive a restart); if the wall clock is set
back, a lock is clamped to at most `lock_duration` from now so it can never
become longer than the policy.
"""

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from ag_workstation.auth.state import AuthStateStore, LockoutRow
from ag_workstation.clock import Clock

MAX_FAILURES: Final = 5
LOCK_DURATION: Final = timedelta(minutes=5)
FAILURE_WINDOW: Final = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class LockStatus:
    """Whether a subject may try now."""

    locked_until: datetime | None

    @property
    def locked(self) -> bool:
        """True while locked."""
        return self.locked_until is not None


@dataclass(frozen=True, slots=True)
class FailureResult:
    """State after a counted failure."""

    locked_until: datetime | None
    """Set when this failure caused the lock."""
    attempts_remaining: int


class LoginThrottle:
    """Counts failures and locks subjects. Thread-safe."""

    def __init__(
        self,
        store: AuthStateStore,
        clock: Clock,
        *,
        max_failures: int = MAX_FAILURES,
        lock_duration: timedelta = LOCK_DURATION,
        failure_window: timedelta = FAILURE_WINDOW,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        self._store = store
        self._clock = clock
        self._max = max_failures
        self._duration = lock_duration
        self._window = failure_window
        self._lock = threading.Lock()

    def status(self, subject: str) -> LockStatus:
        """Whether `subject` is locked now; clears an expired lock."""
        with self._lock:
            now = self._clock.now()
            row = self._store.get_lockout(subject)
            if row is None or row.locked_until is None:
                return LockStatus(None)
            if now >= row.locked_until:
                self._store.clear_lockout(subject)
                return LockStatus(None)
            if row.locked_until - now > self._duration:
                # The wall clock went back: never lock longer than the policy.
                row = LockoutRow(row.failures, row.first_failure_at, now + self._duration)
                self._store.put_lockout(subject, row)
            return LockStatus(row.locked_until)

    def record_failure(self, subject: str) -> FailureResult:
        """Count one failure; locks on the `max_failures`-th within the window."""
        with self._lock:
            now = self._clock.now()
            row = self._store.get_lockout(subject)
            fresh = (
                row is None
                or row.locked_until is not None
                or not row.first_failure_at <= now < row.first_failure_at + self._window
            )
            failures = 1 if fresh or row is None else row.failures + 1
            first = now if fresh or row is None else row.first_failure_at
            if failures >= self._max:
                until = now + self._duration
                self._store.put_lockout(subject, LockoutRow(0, now, until))
                return FailureResult(until, 0)
            self._store.put_lockout(subject, LockoutRow(failures, first, None))
            return FailureResult(None, self._max - failures)

    def record_success(self, subject: str) -> None:
        """Reset `subject`'s failures."""
        with self._lock:
            self._store.clear_lockout(subject)

    def purge(self) -> int:
        """Drop rows that can no longer matter (housekeeping)."""
        with self._lock:
            now = self._clock.now()
            return self._store.purge_lockouts(now - self._window, now)
