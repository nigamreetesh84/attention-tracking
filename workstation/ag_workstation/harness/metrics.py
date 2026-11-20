"""Accuracy metrics over state timelines (`docs/detection-engine.md` section 9).

- Event-level recall and precision per state: a labelled episode counts as
  detected when a predicted episode of the same state overlaps it, with the
  labelled episode widened by `tolerance_s` on both sides.
- Detection latency per state: predicted start minus labelled start.
- False alarms per 8-hour shift: predicted warned episodes that touch no
  labelled non-attentive time, scaled from the attentive footage.
- Frame-level confusion matrix.
- The same per tag slice, flagged when a slice's recall is more than
  `FAIRNESS_GAP_POINTS` below the overall recall.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ag_shared.states import WARNED_STATES, AttentionState
from pydantic import BaseModel, ConfigDict

SHIFT_SECONDS = 8 * 3600.0
FAIRNESS_GAP_POINTS = 5.0
DEFAULT_TOLERANCE_S = 2.0

TARGET_SLEEPING_RECALL = 0.95
TARGET_FALSE_ALARMS_PER_SHIFT = 2.0

EVENT_STATES: tuple[AttentionState, ...] = (
    AttentionState.SLEEPING,
    AttentionState.ABSENT,
    AttentionState.DROWSY,
    AttentionState.DISTRACTED,
)


@dataclass(frozen=True, slots=True)
class Segment:
    """A stretch of one state, [start, end)."""

    start: float
    end: float
    state: AttentionState


Timeline = Sequence[Segment]


def timeline_from_samples(
    samples: Sequence[tuple[float, AttentionState]], end: float
) -> list[Segment]:
    """Merge (time, state) samples into segments; each sample lasts until the next."""
    segments: list[Segment] = []
    for i, (t, state) in enumerate(samples):
        stop = samples[i + 1][0] if i + 1 < len(samples) else end
        if stop <= t:
            continue
        if segments and segments[-1].state is state and abs(segments[-1].end - t) < 1e-9:
            segments[-1] = Segment(segments[-1].start, stop, state)
        else:
            segments.append(Segment(t, stop, state))
    return segments


def episodes(timeline: Timeline, state: AttentionState) -> list[Segment]:
    """Maximal stretches of `state` (adjacent segments merged)."""
    out: list[Segment] = []
    for seg in timeline:
        if seg.state is not state:
            continue
        if out and abs(out[-1].end - seg.start) < 1e-9:
            out[-1] = Segment(out[-1].start, seg.end, state)
        else:
            out.append(seg)
    return out


def _overlaps(a: Segment, b: Segment, tolerance: float = 0.0) -> bool:
    return a.start - tolerance < b.end and b.start < a.end + tolerance


class StateMetrics(BaseModel):
    """Event metrics of one state."""

    model_config = ConfigDict(frozen=True)

    labelled: int = 0
    detected: int = 0
    predicted: int = 0
    true_predictions: int = 0
    latencies_s: tuple[float, ...] = ()

    @property
    def recall(self) -> float | None:
        """Detected / labelled episodes (None without labelled episodes)."""
        return self.detected / self.labelled if self.labelled else None

    @property
    def precision(self) -> float | None:
        """Predictions that match a labelled episode / all predictions."""
        return self.true_predictions / self.predicted if self.predicted else None

    @property
    def mean_latency_s(self) -> float | None:
        """Mean detection latency."""
        return sum(self.latencies_s) / len(self.latencies_s) if self.latencies_s else None

    @property
    def max_latency_s(self) -> float | None:
        """Worst detection latency."""
        return max(self.latencies_s) if self.latencies_s else None

    def __add__(self, other: "StateMetrics") -> "StateMetrics":
        return StateMetrics(
            labelled=self.labelled + other.labelled,
            detected=self.detected + other.detected,
            predicted=self.predicted + other.predicted,
            true_predictions=self.true_predictions + other.true_predictions,
            latencies_s=self.latencies_s + other.latencies_s,
        )


def state_metrics(
    truth: Timeline, predicted: Timeline, state: AttentionState, tolerance_s: float
) -> StateMetrics:
    """Event recall, precision and latency of `state` on one recording."""
    true_eps = episodes(truth, state)
    pred_eps = episodes(predicted, state)
    detected = 0
    latencies: list[float] = []
    for t in true_eps:
        hits = [p for p in pred_eps if _overlaps(t, p, tolerance_s)]
        if hits:
            detected += 1
            latencies.append(round(max(0.0, min(p.start for p in hits) - t.start), 3))
    true_predictions = sum(
        1 for p in pred_eps if any(_overlaps(t, p, tolerance_s) for t in true_eps)
    )
    return StateMetrics(
        labelled=len(true_eps),
        detected=detected,
        predicted=len(pred_eps),
        true_predictions=true_predictions,
        latencies_s=tuple(latencies),
    )


def false_alarms(truth: Timeline, predicted: Timeline, tolerance_s: float) -> tuple[int, float]:
    """(false alarm count, attentive seconds) of one recording.

    A false alarm is a predicted warned episode that overlaps no labelled
    non-attentive, non-unknown time (widened by the tolerance).
    """
    flagged = [
        s
        for s in truth
        if s.state is not AttentionState.ATTENTIVE and s.state is not AttentionState.UNKNOWN
    ]
    count = 0
    for state in WARNED_STATES:
        for p in episodes(predicted, state):
            if not any(_overlaps(t, p, tolerance_s) for t in flagged):
                count += 1
    attentive = sum(s.end - s.start for s in truth if s.state is AttentionState.ATTENTIVE)
    return count, attentive


def confusion(
    truth: Timeline, predicted: Timeline, step_s: float = 0.1
) -> dict[AttentionState, dict[AttentionState, int]]:
    """Frame-level confusion matrix: matrix[truth][predicted] = samples."""
    matrix: dict[AttentionState, dict[AttentionState, int]] = {}
    if not truth:
        return matrix
    end = truth[-1].end
    i = 0
    t = truth[0].start
    while t < end - 1e-9:
        true_state = _state_at(truth, t)
        pred_state = _state_at(predicted, t)
        if true_state is not None and pred_state is not None:
            row = matrix.setdefault(true_state, {})
            row[pred_state] = row.get(pred_state, 0) + 1
        i += 1
        t = truth[0].start + i * step_s
    return matrix


def _state_at(timeline: Timeline, t: float) -> AttentionState | None:
    for seg in timeline:
        if seg.start <= t < seg.end:
            return seg.state
    return None


class RecordingResult(BaseModel):
    """Metrics of one recording (or one paired session)."""

    model_config = ConfigDict(frozen=True)

    name: str
    tags: dict[str, bool | str]
    duration_s: float
    states: dict[AttentionState, StateMetrics]
    false_alarms: int
    attentive_s: float
    confusion: dict[AttentionState, dict[AttentionState, int]]


def evaluate_recording(
    name: str,
    tags: Mapping[str, bool | str],
    truth: Timeline,
    predicted: Timeline,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> RecordingResult:
    """All metrics of one recording."""
    fa, attentive = false_alarms(truth, predicted, tolerance_s)
    return RecordingResult(
        name=name,
        tags=dict(tags),
        duration_s=truth[-1].end - truth[0].start if truth else 0.0,
        states={s: state_metrics(truth, predicted, s, tolerance_s) for s in EVENT_STATES},
        false_alarms=fa,
        attentive_s=attentive,
        confusion=confusion(truth, predicted),
    )


class Aggregate(BaseModel):
    """Metrics summed over a set of recordings."""

    model_config = ConfigDict(frozen=True)

    recordings: int
    duration_s: float
    states: dict[AttentionState, StateMetrics]
    false_alarms: int
    attentive_s: float
    confusion: dict[AttentionState, dict[AttentionState, int]]

    @property
    def false_alarms_per_shift(self) -> float | None:
        """False alarms scaled to an 8-hour shift of attentive work."""
        if self.attentive_s <= 0:
            return None
        return self.false_alarms / self.attentive_s * SHIFT_SECONDS

    def recall(self, state: AttentionState) -> float | None:
        """Event recall of `state`."""
        return self.states[state].recall


def aggregate(results: Iterable[RecordingResult]) -> Aggregate:
    """Sum the metrics of several recordings."""
    items = list(results)
    states = {s: StateMetrics() for s in EVENT_STATES}
    matrix: dict[AttentionState, dict[AttentionState, int]] = {}
    for r in items:
        for s in EVENT_STATES:
            states[s] = states[s] + r.states[s]
        for true_state, row in r.confusion.items():
            out = matrix.setdefault(true_state, {})
            for pred_state, n in row.items():
                out[pred_state] = out.get(pred_state, 0) + n
    return Aggregate(
        recordings=len(items),
        duration_s=sum(r.duration_s for r in items),
        states=states,
        false_alarms=sum(r.false_alarms for r in items),
        attentive_s=sum(r.attentive_s for r in items),
        confusion=matrix,
    )


class SliceResult(BaseModel):
    """Metrics of one tag slice and whether it falls short on fairness."""

    model_config = ConfigDict(frozen=True)

    tag: str
    value: str
    metrics: Aggregate
    flagged_states: tuple[AttentionState, ...]


def fairness_slices(
    results: Sequence[RecordingResult], overall: Aggregate, gap_points: float = FAIRNESS_GAP_POINTS
) -> list[SliceResult]:
    """Metrics per tag value; a state is flagged when the slice's recall is
    more than `gap_points` percentage points below the overall recall."""
    groups: dict[tuple[str, str], list[RecordingResult]] = {}
    for r in results:
        for tag, value in r.tags.items():
            groups.setdefault((tag, str(value).lower()), []).append(r)
    slices: list[SliceResult] = []
    for (tag, value), members in sorted(groups.items()):
        metrics = aggregate(members)
        flagged: list[AttentionState] = []
        for state in EVENT_STATES:
            base, here = overall.recall(state), metrics.recall(state)
            if base is not None and here is not None and (base - here) * 100 > gap_points + 1e-9:
                flagged.append(state)
        slices.append(
            SliceResult(tag=tag, value=value, metrics=metrics, flagged_states=tuple(flagged))
        )
    return slices


def meets_targets(overall: Aggregate) -> dict[str, bool | None]:
    """The scope's accuracy targets; None when there is no data for one."""
    recall = overall.recall(AttentionState.SLEEPING)
    fa = overall.false_alarms_per_shift
    return {
        "sleeping_recall_above_95": None if recall is None else recall > TARGET_SLEEPING_RECALL,
        "false_alarms_below_2_per_shift": None
        if fa is None
        else fa < TARGET_FALSE_ALARMS_PER_SHIFT,
    }
