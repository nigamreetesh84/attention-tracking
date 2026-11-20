"""Condition timers, edge counters and share windows."""

from ag_workstation.engine.timers import ConditionTimer, EdgeCounter, ShareWindow

GAP = 0.5


def test_timer_measures_between_first_and_last_true_frame() -> None:
    timer = ConditionTimer()
    for i in range(41):
        timer.update(i / 10, True, GAP)
    assert timer.duration == 4.0
    assert not timer.exceeds(4.0)
    timer.update(4.1, True, GAP)
    assert timer.exceeds(4.0)


def test_timer_bridges_a_short_gap_of_false_or_unmeasured_frames() -> None:
    timer = ConditionTimer()
    for i in range(20):
        timer.update(i / 10, True, GAP)
    timer.update(2.0, None, GAP)
    timer.update(2.1, False, GAP)
    timer.update(2.2, False, GAP)
    assert timer.update(2.3, True, GAP) == 2.3


def test_timer_resets_after_a_gap_longer_than_the_tolerance() -> None:
    timer = ConditionTimer()
    for i in range(20):
        timer.update(i / 10, True, GAP)
    for i in range(20, 26):
        timer.update(i / 10, False, GAP)
    assert timer.duration == 0.0
    assert timer.update(2.6, True, GAP) == 0.0
    assert abs(timer.update(2.7, True, GAP) - 0.1) < 1e-9


def test_timer_restarts_when_true_returns_after_a_silent_gap() -> None:
    timer = ConditionTimer()
    timer.update(0.0, True, GAP)
    timer.update(1.0, True, GAP)  # no frames in between: the gap is too long
    assert timer.duration == 0.0


def test_timer_zero_gap_tolerance_resets_on_any_interruption() -> None:
    timer = ConditionTimer()
    timer.update(0.0, True, 0.0)
    timer.update(0.1, True, 0.0)
    timer.update(0.2, False, 0.0)
    assert timer.duration == 0.0


def test_edge_counter_counts_rising_edges_in_the_window() -> None:
    counter = EdgeCounter()
    values = [False, True, True, False, True, None, True, False]
    counts = [counter.update(float(i), v, 100.0) for i, v in enumerate(values)]
    # An unmeasured frame does not end the event in progress.
    assert counts == [0, 1, 1, 1, 2, 2, 2, 2]


def test_edge_counter_drops_old_events() -> None:
    counter = EdgeCounter()
    counter.update(0.0, True, 10.0)
    counter.update(1.0, False, 10.0)
    assert counter.update(10.0, False, 10.0) == 1
    assert counter.update(10.1, False, 10.0) == 0
    assert counter.count == 0
    counter.reset()
    assert counter.update(11.0, True, 10.0) == 1


def test_share_window() -> None:
    window = ShareWindow()
    assert window.valid_share() == 0.0
    for i, value in enumerate([True, None, None, False]):
        window.update(i * 0.5, value, 2.0)
    assert len(window) == 4
    assert window.valid_share() == 0.5
    window.update(5.0, True, 2.0)
    assert len(window) == 1
    window.reset()
    assert len(window) == 0
