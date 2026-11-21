"""Metric functions on hand-built timelines."""

import pytest
from ag_shared.states import AttentionState
from ag_workstation.harness.metrics import (
    SHIFT_SECONDS,
    RecordingResult,
    Segment,
    StateMetrics,
    aggregate,
    confusion,
    episodes,
    evaluate_recording,
    fairness_slices,
    false_alarms,
    meets_targets,
    state_metrics,
    timeline_from_samples,
)

A = AttentionState


def seg(start: float, end: float, state: AttentionState) -> Segment:
    return Segment(start, end, state)


TRUTH = [
    seg(0, 60, A.ATTENTIVE),
    seg(60, 80, A.SLEEPING),
    seg(80, 200, A.ATTENTIVE),
    seg(200, 230, A.SLEEPING),
    seg(230, 300, A.ATTENTIVE),
]


def test_timeline_from_samples_merges_equal_neighbours() -> None:
    samples = [(0.0, A.ATTENTIVE), (1.0, A.ATTENTIVE), (2.0, A.SLEEPING), (3.0, A.SLEEPING)]
    assert timeline_from_samples(samples, 5.0) == [seg(0, 2, A.ATTENTIVE), seg(2, 5, A.SLEEPING)]
    assert timeline_from_samples([(0.0, A.ATTENTIVE), (0.0, A.SLEEPING)], 1.0) == [
        seg(0, 1, A.SLEEPING)
    ]


def test_episodes_merge_adjacent_segments() -> None:
    tl = [seg(0, 1, A.SLEEPING), seg(1, 2, A.SLEEPING), seg(3, 4, A.SLEEPING)]
    assert episodes(tl, A.SLEEPING) == [seg(0, 2, A.SLEEPING), seg(3, 4, A.SLEEPING)]


def test_recall_precision_and_latency() -> None:
    predicted = [
        seg(0, 64.5, A.ATTENTIVE),
        seg(64.5, 82, A.SLEEPING),  # detected 4.5 s late
        seg(82, 120, A.ATTENTIVE),
        seg(120, 125, A.SLEEPING),  # false
        seg(125, 300, A.ATTENTIVE),  # second sleep missed
    ]
    m = state_metrics(TRUTH, predicted, A.SLEEPING, tolerance_s=2.0)
    assert (m.labelled, m.detected, m.predicted, m.true_predictions) == (2, 1, 2, 1)
    assert m.recall == 0.5
    assert m.precision == 0.5
    assert m.latencies_s == (4.5,)
    assert m.mean_latency_s == m.max_latency_s == 4.5


def test_tolerance_lets_a_trailing_prediction_match() -> None:
    truth = [seg(0, 10, A.SLEEPING), seg(10, 20, A.ATTENTIVE)]
    predicted = [seg(0, 11, A.ATTENTIVE), seg(11, 13, A.SLEEPING), seg(13, 20, A.ATTENTIVE)]
    assert state_metrics(truth, predicted, A.SLEEPING, 0.0).detected == 0
    hit = state_metrics(truth, predicted, A.SLEEPING, 2.0)
    assert hit.detected == 1
    assert hit.latencies_s == (11.0,)


def test_empty_metrics() -> None:
    m = StateMetrics()
    assert m.recall is None and m.precision is None
    assert m.mean_latency_s is None and m.max_latency_s is None


def test_false_alarms_count_only_predictions_in_attentive_time() -> None:
    predicted = [
        seg(0, 30, A.ATTENTIVE),
        seg(30, 40, A.DISTRACTED),  # false alarm
        seg(40, 66, A.ATTENTIVE),
        seg(66, 80, A.SLEEPING),  # true
        seg(80, 300, A.ATTENTIVE),
    ]
    count, attentive = false_alarms(TRUTH, predicted, 2.0)
    assert count == 1
    assert attentive == 250


def test_confusion_matrix_samples_both_timelines() -> None:
    truth = [seg(0, 1, A.ATTENTIVE), seg(1, 2, A.SLEEPING)]
    predicted = [seg(0, 1.5, A.ATTENTIVE), seg(1.5, 2, A.SLEEPING)]
    matrix = confusion(truth, predicted, step_s=0.1)
    assert matrix[A.ATTENTIVE] == {A.ATTENTIVE: 10}
    assert matrix[A.SLEEPING] == {A.ATTENTIVE: 5, A.SLEEPING: 5}
    assert confusion([], predicted) == {}
    # A gap in the prediction is skipped, not counted.
    assert confusion(truth, [seg(0, 0.5, A.ATTENTIVE)], 0.1) == {A.ATTENTIVE: {A.ATTENTIVE: 5}}


def test_aggregate_and_false_alarms_per_shift() -> None:
    perfect = evaluate_recording("a", {"glasses": False}, TRUTH, TRUTH)
    one_false = evaluate_recording(
        "b",
        {"glasses": True},
        TRUTH,
        [
            *TRUTH[:2],
            seg(80, 120, A.ATTENTIVE),
            seg(120, 130, A.DROWSY),
            seg(130, 200, A.ATTENTIVE),
            *TRUTH[3:],
        ],
    )
    agg = aggregate([perfect, one_false])
    assert agg.recordings == 2
    assert agg.duration_s == 600
    assert agg.recall(A.SLEEPING) == 1.0
    assert agg.false_alarms == 1
    assert agg.false_alarms_per_shift == pytest.approx(1 / 500 * SHIFT_SECONDS)
    assert agg.confusion[A.SLEEPING][A.SLEEPING] == 1000
    assert agg.confusion[A.ATTENTIVE][A.DROWSY] == 100
    assert meets_targets(agg) == {
        "sleeping_recall_above_95": True,
        "false_alarms_below_2_per_shift": False,
    }


def test_targets_without_data() -> None:
    agg = aggregate([])
    assert agg.false_alarms_per_shift is None
    assert meets_targets(agg) == {
        "sleeping_recall_above_95": None,
        "false_alarms_below_2_per_shift": None,
    }


def test_fairness_flags_a_slice_more_than_5_points_below() -> None:
    def rec(name: str, tone: str, hit: bool) -> RecordingResult:
        predicted = TRUTH if hit else [seg(0, 300, A.ATTENTIVE)]
        return evaluate_recording(name, {"skin_tone_group": tone}, TRUTH, predicted)

    results = [rec(f"x{i}", "I-III", True) for i in range(9)] + [
        rec("y0", "IV-VI", True),
        rec("y1", "IV-VI", False),
    ]
    overall = aggregate(results)
    slices = fairness_slices(results, overall)
    by_value = {s.value: s for s in slices}
    assert by_value["i-iii"].flagged_states == ()
    assert by_value["iv-vi"].flagged_states == (A.SLEEPING,)
    assert by_value["iv-vi"].metrics.recordings == 2


def test_mean_latency_is_the_same_on_every_python_version() -> None:
    # Python 3.12 made sum() of floats compensated; 3.11 gives
    # 5.699999999999999 for these latencies, so the committed baseline only
    # matched on 3.12+. The mean must be correctly rounded on every version.
    metrics = StateMetrics(
        labelled=8, detected=8, latencies_s=(4.2, 8.2, 4.2, 8.2, 4.2, 4.2, 4.2, 8.2)
    )
    assert metrics.mean_latency_s == 5.7
