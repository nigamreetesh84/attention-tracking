"""Windowed signal state: EAR baseline, PERCLOS, blink rate, yawn, nod, motion."""

import pytest
from ag_workstation.landmarks import Point
from ag_workstation.signals.params import ExtractionParams
from ag_workstation.signals.windows import (
    BlinkRateCounter,
    EarBaseline,
    MotionTracker,
    NodDetector,
    PerclosWindow,
    YawnDetector,
)

P = ExtractionParams()
GAP = 0.5


def _t(i: int) -> float:
    """Frame `i` at 10 fps; i / 10 keeps boundaries exact."""
    return i / 10


# --- EAR baseline ---------------------------------------------------------------


def test_baseline_normalizes_open_eyes_to_about_one() -> None:
    baseline = EarBaseline(P)
    ratios = [baseline.normalize(_t(i), 0.30) for i in range(100)]
    assert ratios[0] == pytest.approx(1.0)
    assert ratios[-1] == pytest.approx(1.0)
    assert baseline.value == pytest.approx(0.30)


def test_baseline_adapts_to_narrow_eyes_instead_of_calling_them_closed() -> None:
    baseline = EarBaseline(P)
    ratios = [baseline.normalize(_t(i), 0.18) for i in range(100)]
    assert min(ratios) > P.ear_closed_ratio


def test_blinks_during_warm_up_do_not_lower_the_baseline() -> None:
    baseline = EarBaseline(P)
    for i in range(80):
        baseline.normalize(_t(i), 0.08 if i % 10 == 0 else 0.30)
    assert baseline.value == pytest.approx(0.30)


def test_long_eye_closure_after_warm_up_keeps_the_baseline() -> None:
    baseline = EarBaseline(P)
    for i in range(100):
        baseline.normalize(_t(i), 0.30)
    ratios = [baseline.normalize(_t(100 + i), 0.09) for i in range(3000)]  # 5 minutes asleep
    assert baseline.value == pytest.approx(0.30)
    assert max(ratios) < P.ear_closed_ratio


def test_baseline_follows_slow_change_while_eyes_are_open() -> None:
    baseline = EarBaseline(P)
    for i in range(100):
        baseline.normalize(_t(i), 0.30)
    for i in range(3000):
        baseline.normalize(_t(100 + i), 0.26)  # tired or squinting, still open
    assert baseline.value == pytest.approx(0.26, abs=0.005)


def test_baseline_is_clamped() -> None:
    low = EarBaseline(P)
    assert low.normalize(0.0, 0.05) == pytest.approx(0.05 / P.ear_baseline_min)
    high = EarBaseline(P)
    assert high.normalize(0.0, 0.9) == pytest.approx(0.9 / P.ear_baseline_max)


def test_session_starting_with_closed_eyes_reads_closed_then_recovers() -> None:
    baseline = EarBaseline(P)
    closed = [baseline.normalize(_t(i), 0.09) for i in range(100)]
    assert max(closed) < P.ear_closed_ratio
    opened = [baseline.normalize(_t(100 + i), 0.30) for i in range(100)]
    assert min(opened) > P.ear_closed_ratio
    assert baseline.value == pytest.approx(0.30, abs=0.01)
    assert baseline.normalize(_t(200), 0.09) < P.ear_closed_ratio


def test_baseline_reset_starts_a_new_session() -> None:
    baseline = EarBaseline(P)
    for i in range(100):
        baseline.normalize(_t(i), 0.40)
    baseline.reset()
    assert baseline.normalize(_t(100), 0.20) == pytest.approx(1.0)


# --- PERCLOS --------------------------------------------------------------------


def _perclos() -> PerclosWindow:
    return PerclosWindow(min_valid_share=0.5, min_fill=0.5)


def test_perclos_is_the_share_of_closed_frames() -> None:
    window = _perclos()
    reading = None
    for i in range(301):
        reading = window.update(_t(i), i % 10 < 3, 30.0)
    assert reading is not None
    assert reading.valid
    assert reading.value == pytest.approx(0.3, abs=0.01)


def test_perclos_is_invalid_until_half_the_window_is_observed() -> None:
    window = _perclos()
    for i in range(150):
        assert not window.update(_t(i), True, 30.0).valid  # 0 .. 14.9 s
    assert window.update(_t(150), True, 30.0).valid  # 15.0 s observed
    assert window.update(_t(151), True, 30.0).value == 1.0


def test_perclos_ignores_invalid_frames_in_the_share() -> None:
    window = _perclos()
    reading = None
    for i in range(300):
        # 40% invalid; of the valid frames one half is closed.
        closed = None if i % 10 < 4 else i % 10 < 7
        reading = window.update(_t(i), closed, 30.0)
    assert reading is not None
    assert reading.valid
    assert reading.value == pytest.approx(0.5)


def test_perclos_needs_half_the_frames_valid() -> None:
    window = _perclos()
    exactly_half = None
    for i in range(300):
        exactly_half = window.update(_t(i), True if i % 2 == 0 else None, 30.0)
    assert exactly_half is not None
    assert exactly_half.valid

    window = _perclos()
    below_half = None
    for i in range(300):
        below_half = window.update(_t(i), True if i % 10 < 4 else None, 30.0)
    assert below_half is not None
    assert not below_half.valid


def test_perclos_with_no_valid_frame_is_invalid() -> None:
    window = _perclos()
    reading = None
    for i in range(300):
        reading = window.update(_t(i), None, 30.0)
    assert reading is not None
    assert not reading.valid
    assert reading.value == 0.0


def test_perclos_forgets_frames_older_than_the_window() -> None:
    window = _perclos()
    for i in range(100):
        window.update(_t(i), True, 30.0)  # closed 0 .. 9.9 s
    for i in range(100, 300):
        window.update(_t(i), False, 30.0)
    mid = window.update(_t(300), False, 30.0)
    for i in range(301, 400):
        window.update(_t(i), False, 30.0)
    late = window.update(_t(400), False, 30.0)
    assert mid.value > 0.0
    assert late.valid
    assert late.value == 0.0


def test_perclos_window_edge_is_inclusive() -> None:
    window = _perclos()
    window.update(0.0, True, 30.0)
    # The frame exactly one window old still counts; a later frame drops it.
    assert window.update(30.0, False, 30.0).value == pytest.approx(0.5)
    assert window.update(30.1, False, 30.0).value == 0.0


def test_perclos_window_length_follows_the_threshold() -> None:
    window = _perclos()
    for i in range(100):
        window.update(_t(i), True, 10.0)
    reading = None
    for i in range(100, 201):
        reading = window.update(_t(i), False, 10.0)
    assert reading is not None
    assert reading.valid
    assert reading.value == 0.0


def test_perclos_after_a_long_gap_starts_again() -> None:
    window = _perclos()
    for i in range(300):
        window.update(_t(i), True, 30.0)
    # Camera offline for two minutes: nothing in the window but the new frame.
    reading = window.update(150.0, False, 30.0)
    assert not reading.valid


def test_perclos_reset() -> None:
    window = _perclos()
    for i in range(300):
        window.update(_t(i), True, 30.0)
    window.reset()
    assert not window.update(30.0, True, 30.0).valid


# --- blink rate -----------------------------------------------------------------


def _blinks() -> BlinkRateCounter:
    return BlinkRateCounter(window_s=60.0, min_span_s=20.0, min_valid_share=0.5)


def test_blink_rate_counts_closed_to_open_transitions_per_minute() -> None:
    counter = _blinks()
    reading = None
    for i in range(601):  # 60 s, one 0.2 s blink every 4 s
        reading = counter.update(_t(i), i % 40 in (20, 21), GAP)
    assert reading is not None
    assert reading.valid
    assert reading.value == pytest.approx(15.0)


def test_blink_rate_is_invalid_before_the_minimum_span() -> None:
    counter = _blinks()
    for i in range(200):
        assert not counter.update(_t(i), False, GAP).valid
    assert counter.update(_t(200), False, GAP).valid


def test_blink_rate_scales_a_partial_window_to_a_minute() -> None:
    counter = _blinks()
    reading = None
    for i in range(301):  # 30 s with 5 blinks
        reading = counter.update(_t(i), i % 60 in (30, 31), GAP)
    assert reading is not None
    assert reading.value == pytest.approx(10.0)


def test_blink_rate_with_steady_eyes_is_zero() -> None:
    counter = _blinks()
    reading = None
    for i in range(300):
        reading = counter.update(_t(i), False, GAP)
    assert reading is not None
    assert reading.valid
    assert reading.value == 0.0


def test_a_blink_bridges_a_short_invalid_gap_but_not_a_long_one() -> None:
    short = _blinks()
    long = _blinks()
    for counter, gap_frames in ((short, 3), (long, 10)):
        reading = None
        for i in range(300):
            closed: bool | None = False
            if i == 100:
                closed = True
            elif 100 < i <= 100 + gap_frames:
                closed = None
            reading = counter.update(_t(i), closed, GAP)
        assert reading is not None
        assert reading.valid
        expected = 60.0 / 29.9 if counter is short else 0.0
        assert reading.value == pytest.approx(expected)


def test_blink_rate_is_invalid_when_most_frames_are_invalid() -> None:
    counter = _blinks()
    reading = None
    for i in range(300):
        reading = counter.update(_t(i), None if i % 10 < 6 else False, GAP)
    assert reading is not None
    assert not reading.valid


def test_blinks_leave_the_window() -> None:
    counter = _blinks()
    for i in range(100):
        counter.update(_t(i), i % 20 == 0, GAP)
    reading = None
    for i in range(100, 800):
        reading = counter.update(_t(i), False, GAP)
    assert reading is not None
    assert reading.value == 0.0


def test_blink_counter_reset() -> None:
    counter = _blinks()
    for i in range(300):
        counter.update(_t(i), i % 20 == 0, GAP)
    counter.reset()
    assert not counter.update(30.0, False, GAP).valid


# --- yawn -----------------------------------------------------------------------


def test_yawn_needs_more_than_the_minimum_duration() -> None:
    yawn = YawnDetector(min_s=1.5)
    results = [yawn.update(_t(i), True, GAP) for i in range(17)]
    assert results[15] is False  # exactly 1.5 s open
    assert results[16] is True  # 1.6 s


def test_a_short_mouth_opening_is_not_a_yawn() -> None:
    yawn = YawnDetector(min_s=1.5)
    for i in range(10):
        assert yawn.update(_t(i), True, GAP) is False
    assert yawn.update(_t(10), False, GAP) is False
    # The timer restarted: another 1.5 s is needed.
    results = [yawn.update(_t(20 + i), True, GAP) for i in range(17)]
    assert results[15] is False
    assert results[16] is True


def test_yawn_ends_when_the_mouth_closes() -> None:
    yawn = YawnDetector(min_s=1.5)
    for i in range(20):
        yawn.update(_t(i), True, GAP)
    assert yawn.update(_t(20), False, GAP) is False


def test_yawn_timer_survives_a_short_invalid_gap() -> None:
    yawn = YawnDetector(min_s=1.5)
    for i in range(10):
        yawn.update(_t(i), True, GAP)
    for i in range(10, 15):  # 0.5 s without a measurement: within tolerance
        assert yawn.update(_t(i), None, GAP) is None
    assert yawn.update(_t(15), True, GAP) is False
    assert yawn.update(_t(16), True, GAP) is True


def test_yawn_timer_resets_after_a_long_invalid_gap() -> None:
    yawn = YawnDetector(min_s=1.5)
    for i in range(10):
        yawn.update(_t(i), True, GAP)
    for i in range(10, 17):
        assert yawn.update(_t(i), None, GAP) is None
    assert yawn.update(_t(17), True, GAP) is False
    assert yawn.update(_t(20), True, GAP) is False
    assert yawn.update(_t(33), True, GAP) is True


def test_yawn_reset() -> None:
    yawn = YawnDetector(min_s=1.5)
    for i in range(20):
        yawn.update(_t(i), True, GAP)
    yawn.reset()
    assert yawn.update(_t(20), True, GAP) is False


# --- nod ------------------------------------------------------------------------


def _nod() -> NodDetector:
    return NodDetector(drop_deg=15.0, recover_deg=5.0, max_s=3.0, ref_window_s=4.0)


def _feed(detector: NodDetector, pitches: list[float | None], start: int = 0) -> list[bool | None]:
    return [detector.update(_t(start + i), pitch, GAP) for i, pitch in enumerate(pitches)]


def test_a_drop_and_recovery_is_one_nod() -> None:
    drop = [0.0 - 2.0 * i for i in range(11)]  # 0 .. -20 over 1 s
    out = _feed(_nod(), [0.0] * 20 + drop + [-20.0] * 5 + [-10.0, -2.0] + [0.0] * 20)
    assert out.count(True) == 1
    assert out.index(True) == 20 + 11 + 5 + 1  # the frame back within 5 degrees


def test_a_slow_drop_still_counts() -> None:
    drop = [0.0 - 0.8 * i for i in range(26)]  # 20 degrees over 2.5 s
    out = _feed(_nod(), [0.0] * 20 + drop + [0.0] * 10)
    assert out.count(True) == 1


def test_a_small_dip_is_not_a_nod() -> None:
    out = _feed(_nod(), [0.0] * 20 + [-10.0] * 10 + [0.0] * 20)
    assert True not in out


def test_staying_down_is_not_a_nod() -> None:
    # Looking down at the work for 5 s and then up again: head drop, not a nod.
    out = _feed(_nod(), [0.0] * 20 + [-20.0] * 50 + [0.0] * 20)
    assert True not in out


def test_recovery_just_inside_the_time_limit_counts() -> None:
    inside = _feed(_nod(), [0.0] * 20 + [-20.0] * 30 + [0.0] * 5)
    assert inside.count(True) == 1  # recovered 3.0 s after the drop
    outside = _feed(_nod(), [0.0] * 20 + [-20.0] * 31 + [0.0] * 5)
    assert True not in outside  # 3.1 s


def test_partial_recovery_is_not_enough() -> None:
    out = _feed(_nod(), [0.0] * 20 + [-20.0] * 5 + [-8.0] * 40)
    assert True not in out


def test_repeated_nods_are_each_reported() -> None:
    one = [0.0] * 15 + [-20.0] * 8
    out = _feed(_nod(), one * 3 + [0.0] * 15)
    assert out.count(True) == 3


def test_nod_reference_follows_a_new_working_posture() -> None:
    # Settles at -20 (looking at the bench); a nod is then measured from there.
    out = _feed(_nod(), [0.0] * 10 + [-20.0] * 80 + [-40.0] * 8 + [-20.0] * 10)
    assert out.count(True) == 1


def test_nod_without_pitch_is_invalid_and_a_long_gap_resets() -> None:
    detector = _nod()
    _feed(detector, [0.0] * 20 + [-20.0] * 3)
    gap = _feed(detector, [None] * 10, start=23)
    assert gap == [None] * 10
    # The drop was forgotten: coming back upright is not a nod.
    out = _feed(detector, [0.0] * 10, start=33)
    assert True not in out


def test_nod_survives_a_short_invalid_gap() -> None:
    detector = _nod()
    _feed(detector, [0.0] * 20 + [-20.0] * 3)
    _feed(detector, [None] * 3, start=23)
    out = _feed(detector, [0.0] * 3, start=26)
    assert out.count(True) == 1


def test_nod_reset() -> None:
    detector = _nod()
    _feed(detector, [0.0] * 20 + [-20.0] * 3)
    detector.reset()
    assert True not in _feed(detector, [0.0] * 5, start=23)


# --- motion ---------------------------------------------------------------------


def _motion() -> MotionTracker:
    return MotionTracker(window_s=2.0, max_gap_s=1.0, min_fill=0.5)


def _pts(x: float, scale: float = 0.2) -> dict[str, tuple[Point, float]]:
    return {"a": (Point(x, 0.5), scale), "b": (Point(x, 0.6), scale)}


def test_still_points_have_zero_motion() -> None:
    tracker = _motion()
    reading = None
    for i in range(30):
        reading = tracker.update(_t(i), _pts(0.5))
    assert reading is not None
    assert reading.valid
    assert reading.value == 0.0


def test_motion_is_speed_in_body_scales_per_second() -> None:
    tracker = _motion()
    reading = None
    for i in range(30):
        # 0.01 per frame at 10 fps = 0.1 per second; scale 0.2 -> 0.5 scales/s.
        reading = tracker.update(_t(i), _pts(0.3 + 0.01 * i))
    assert reading is not None
    assert reading.valid
    assert reading.value == pytest.approx(0.5)


def test_motion_is_invalid_until_the_window_is_half_full() -> None:
    tracker = _motion()
    readings = [tracker.update(_t(i), _pts(0.5)) for i in range(12)]
    assert not readings[0].valid  # nothing to compare with
    assert not readings[10].valid  # samples span 0.9 s
    assert readings[11].valid  # 1.0 s


def test_motion_decays_once_movement_stops() -> None:
    tracker = _motion()
    for i in range(20):
        tracker.update(_t(i), _pts(0.3 + 0.01 * i))
    reading = None
    for i in range(20, 45):
        reading = tracker.update(_t(i), _pts(0.5))
    assert reading is not None
    assert reading.value == 0.0


def test_motion_without_points_becomes_invalid_and_does_not_span_the_gap() -> None:
    tracker = _motion()
    for i in range(20):
        tracker.update(_t(i), _pts(0.5))
    assert tracker.update(_t(20), None).valid  # recent samples still count
    for i in range(21, 40):
        tracker.update(_t(i), None)
    assert not tracker.update(_t(40), None).valid
    # Reappearing somewhere else is not a movement measurement.
    back = tracker.update(_t(41), _pts(0.9))
    assert not back.valid
    reading = None
    for i in range(42, 60):
        reading = tracker.update(_t(i), _pts(0.9))
    assert reading is not None
    assert reading.valid
    assert reading.value == 0.0


def test_motion_only_compares_points_present_in_both_frames() -> None:
    tracker = _motion()
    tracker.update(0.0, {"a": (Point(0.5, 0.5), 0.2)})
    reading = None
    for i in range(1, 20):
        reading = tracker.update(
            _t(i), {"a": (Point(0.5, 0.5), 0.2), "b": (Point(0.1 * i, 0), 0.2)}
        )
    assert reading is not None
    assert reading.valid
    assert reading.value > 0.0  # "b" moves once it has a previous position
    only_new = _motion()
    only_new.update(0.0, {"a": (Point(0.5, 0.5), 0.2)})
    assert not only_new.update(0.1, {"c": (Point(0.5, 0.5), 0.2)}).valid


def test_motion_ignores_points_without_a_scale_and_repeated_timestamps() -> None:
    tracker = _motion()
    reading = None
    for i in range(30):
        reading = tracker.update(_t(i), {"a": (Point(0.01 * i, 0.5), 0.0)})
    assert reading is not None
    assert not reading.valid
    same_ts = _motion()
    same_ts.update(1.0, _pts(0.1))
    assert not same_ts.update(1.0, _pts(0.9)).valid


def test_motion_reset() -> None:
    tracker = _motion()
    for i in range(30):
        tracker.update(_t(i), _pts(0.5))
    tracker.reset()
    assert not tracker.update(3.0, _pts(0.5)).valid
