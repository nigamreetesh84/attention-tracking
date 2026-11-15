"""Exponential reconnect backoff."""


class Backoff:
    """Delays of `initial_s * factor**n`, capped at `max_s`. Not thread-safe."""

    def __init__(self, initial_s: float = 0.5, factor: float = 2.0, max_s: float = 30.0) -> None:
        if initial_s <= 0:
            raise ValueError("initial_s must be positive")
        if factor < 1:
            raise ValueError("factor must be at least 1")
        if max_s < initial_s:
            raise ValueError("max_s must be at least initial_s")
        self._initial = initial_s
        self._factor = factor
        self._max = max_s
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """Delays handed out since the last reset."""
        return self._attempts

    def next_delay(self) -> float:
        """The delay before the next attempt; grows on every call."""
        delay = min(self._max, self._initial * self._factor**self._attempts)
        self._attempts += 1
        return delay

    def reset(self) -> None:
        """Start again from `initial_s` (after a success)."""
        self._attempts = 0
