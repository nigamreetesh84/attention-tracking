"""Retry backoff for the send loop (`api-contract.md` section 6)."""

from collections.abc import Callable
from typing import Final

MIN_BACKOFF_S: Final = 1.0
MAX_BACKOFF_S: Final = 300.0


def backoff_delay(attempts: int, rng: Callable[[], float]) -> float:
    """Seconds to wait after the `attempts`-th failed attempt.

    Exponential (1, 2, 4, ... s) capped at 5 min, with "equal jitter": the
    delay is drawn from the upper half of the step, so stations that went
    offline together spread out but never retry sooner than half the step.
    `rng` returns a float in [0, 1]. The result is clamped to [1 s, 5 min].
    """
    exponent = min(max(attempts, 1) - 1, 16)  # 2**16 s is far past the cap
    step = min(MAX_BACKOFF_S, MIN_BACKOFF_S * 2.0**exponent)
    delay = step / 2 + (step / 2) * min(max(rng(), 0.0), 1.0)
    return min(MAX_BACKOFF_S, max(MIN_BACKOFF_S, delay))
