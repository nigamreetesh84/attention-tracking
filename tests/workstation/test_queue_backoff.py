"""Retry backoff schedule (contract section 6: exponential with jitter, 1 s .. 5 min)."""

import pytest
from ag_workstation.queue import MAX_BACKOFF_S, MIN_BACKOFF_S, backoff_delay


def test_bounds() -> None:
    assert MIN_BACKOFF_S == 1.0
    assert MAX_BACKOFF_S == 300.0


@pytest.mark.parametrize(
    ("attempts", "expected_max"),
    [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (9, 256.0), (10, 300.0), (50, 300.0), (10_000, 300.0)],
)
def test_without_jitter_is_exponential_and_capped(attempts: int, expected_max: float) -> None:
    assert backoff_delay(attempts, lambda: 1.0) == expected_max


def test_jitter_spans_upper_half_and_never_below_floor() -> None:
    assert backoff_delay(4, lambda: 0.0) == 4.0  # half of 8
    assert backoff_delay(4, lambda: 0.5) == 6.0
    assert backoff_delay(1, lambda: 0.0) == 1.0  # floor
    assert backoff_delay(50, lambda: 0.0) == 150.0
    for u in (0.0, 0.3, 0.999):

        def rng(u: float = u) -> float:
            return u

        for n in range(1, 40):
            assert MIN_BACKOFF_S <= backoff_delay(n, rng) <= MAX_BACKOFF_S


def test_zero_attempts_treated_as_first() -> None:
    assert backoff_delay(0, lambda: 1.0) == 1.0


def test_schedule_is_non_decreasing_without_jitter() -> None:
    delays = [backoff_delay(n, lambda: 1.0) for n in range(1, 15)]
    assert delays == sorted(delays)
