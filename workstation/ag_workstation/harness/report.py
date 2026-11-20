"""Harness runs, the accuracy report and the baseline comparison.

`run_directory` finds every label file, groups recordings into sessions,
replays them and computes the metrics. The report is written as
`docs/accuracy-baseline.md` (for people) and `docs/accuracy-baseline.json`
(for the regression check). A later run fails when a metric falls below the
stored baseline by more than the allowed margin.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ag_shared.config import CameraConfig, CameraRole
from ag_shared.states import AttentionState
from ag_shared.thresholds import Thresholds
from pydantic import BaseModel, ConfigDict

from ag_workstation.harness.labels import LABEL_SUFFIX, LabelFile, find_labels
from ag_workstation.harness.metrics import (
    DEFAULT_TOLERANCE_S,
    EVENT_STATES,
    Aggregate,
    RecordingResult,
    aggregate,
    evaluate_recording,
    fairness_slices,
    meets_targets,
)
from ag_workstation.harness.replay import (
    Recording,
    VideoReader,
    opencv_reader,
    replay_traces,
    replay_videos,
    truth_timeline,
)
from ag_workstation.harness.traces import TRACE_SUFFIX
from ag_workstation.landmarks.types import LandmarkProvider

MAX_RECALL_DROP_POINTS = 2.0
MAX_PRECISION_DROP_POINTS = 2.0
MAX_FALSE_ALARM_RISE = 0.5
"""Allowed worsening against the baseline before the run counts as a regression."""

ProviderFactory = Callable[[CameraConfig], LandmarkProvider]


class StateSummary(BaseModel):
    """Headline numbers of one state."""

    model_config = ConfigDict(frozen=True)

    labelled: int
    detected: int
    predicted: int
    recall: float | None
    precision: float | None
    mean_latency_s: float | None
    max_latency_s: float | None


class SliceSummary(BaseModel):
    """Recall per state of one tag slice."""

    model_config = ConfigDict(frozen=True)

    tag: str
    value: str
    recordings: int
    recall: dict[AttentionState, float | None]
    flagged_states: tuple[AttentionState, ...]


class HarnessReport(BaseModel):
    """Everything a harness run measured."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    source: str
    """`video`, `trace` or `mixed`."""
    sessions: int
    duration_s: float
    tolerance_s: float
    states: dict[AttentionState, StateSummary]
    false_alarms: int
    attentive_s: float
    false_alarms_per_shift: float | None
    targets: dict[str, bool | None]
    slices: tuple[SliceSummary, ...]
    confusion: dict[AttentionState, dict[AttentionState, int]]
    session_names: tuple[str, ...]


def _summary(agg: Aggregate) -> dict[AttentionState, StateSummary]:
    return {
        s: StateSummary(
            labelled=m.labelled,
            detected=m.detected,
            predicted=m.predicted,
            recall=m.recall,
            precision=m.precision,
            mean_latency_s=m.mean_latency_s,
            max_latency_s=m.max_latency_s,
        )
        for s, m in agg.states.items()
    }


def group_sessions(directory: Path) -> dict[str, list[Recording]]:
    """Recordings by session key (a recording without one is its own session)."""
    sessions: dict[str, list[Recording]] = {}
    for path, label in find_labels(directory):
        media = path.parent / label.video
        key = label.session or path.name.removesuffix(LABEL_SUFFIX)
        sessions.setdefault(key, []).append(Recording(label, media))
    return dict(sorted(sessions.items()))


def _truth_label(recordings: Sequence[Recording]) -> LabelFile:
    for rec in recordings:
        if rec.label.camera_role is CameraRole.PRIMARY:
            return rec.label
    return recordings[0].label


def _tags(recordings: Sequence[Recording]) -> dict[str, bool | str]:
    tags: dict[str, bool | str] = {}
    for rec in recordings:
        tags.update(rec.label.tags)
    return tags


def is_trace(recording: Recording) -> bool:
    """Whether the recording is a signal trace rather than a video."""
    return recording.path.name.endswith(TRACE_SUFFIX)


def run_sessions(
    sessions: dict[str, list[Recording]],
    *,
    provider_factory: ProviderFactory | None = None,
    reader: VideoReader = opencv_reader,
    thresholds: Thresholds | None = None,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> tuple[list[RecordingResult], set[str]]:
    """Replay every session; returns per-session metrics and the source kinds seen."""
    results: list[RecordingResult] = []
    kinds: set[str] = set()
    for name, recordings in sessions.items():
        traces = [is_trace(r) for r in recordings]
        if all(traces):
            kinds.add("trace")
            replay = replay_traces(name, recordings, thresholds=thresholds)
        elif not any(traces):
            if provider_factory is None:
                raise ValueError("video recordings need a landmark provider (set AG_MODEL_DIR)")
            kinds.add("video")
            replay = replay_videos(
                name, recordings, provider_factory, reader=reader, thresholds=thresholds
            )
        else:
            raise ValueError(f"session {name!r} mixes videos and traces")
        truth = truth_timeline(_truth_label(recordings))
        results.append(
            evaluate_recording(name, _tags(recordings), truth, replay.predicted, tolerance_s)
        )
    return results, kinds


def build_report(
    results: Sequence[RecordingResult],
    kinds: set[str],
    *,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
    now: datetime | None = None,
) -> HarnessReport:
    """Aggregate per-session results into a report."""
    overall = aggregate(results)
    slices = fairness_slices(results, overall)
    return HarnessReport(
        generated_at=now or datetime.now(UTC),
        source=kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else "none"),
        sessions=overall.recordings,
        duration_s=round(overall.duration_s, 3),
        tolerance_s=tolerance_s,
        states=_summary(overall),
        false_alarms=overall.false_alarms,
        attentive_s=round(overall.attentive_s, 3),
        false_alarms_per_shift=overall.false_alarms_per_shift,
        targets=meets_targets(overall),
        slices=tuple(
            SliceSummary(
                tag=s.tag,
                value=s.value,
                recordings=s.metrics.recordings,
                recall={st: s.metrics.recall(st) for st in EVENT_STATES},
                flagged_states=s.flagged_states,
            )
            for s in slices
        ),
        confusion=overall.confusion,
        session_names=tuple(r.name for r in results),
    )


def run_directory(
    directory: Path,
    *,
    provider_factory: ProviderFactory | None = None,
    reader: VideoReader = opencv_reader,
    thresholds: Thresholds | None = None,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
    now: datetime | None = None,
) -> HarnessReport:
    """Find, replay and score every labelled recording under `directory`."""
    results, kinds = run_sessions(
        group_sessions(directory),
        provider_factory=provider_factory,
        reader=reader,
        thresholds=thresholds,
        tolerance_s=tolerance_s,
    )
    return build_report(results, kinds, tolerance_s=tolerance_s, now=now)


# --- rendering ---


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _sec(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


def _check(value: bool | None) -> str:
    return "no data" if value is None else ("met" if value else "NOT MET")


def render_markdown(report: HarnessReport) -> str:
    """The human-readable baseline document."""
    fa = report.false_alarms_per_shift
    fa_text = "n/a" if fa is None else f"{fa:.2f}"
    lines = [
        "# Accuracy baseline",
        "",
        "Written by the replay harness (`python -m ag_workstation.harness`); do not",
        "edit by hand. The numbers the regression check reads are in",
        "`accuracy-baseline.json` next to this file.",
        "",
        f"- Generated: {report.generated_at.isoformat(timespec='seconds')}",
        f"- Source: {report.source} ({report.sessions} sessions, "
        f"{report.duration_s / 60:.1f} minutes)",
        f"- Matching tolerance: {report.tolerance_s:.1f} s",
        "",
        "## Targets",
        "",
        "| Target | Result |",
        "|---|---|",
        f"| Sleeping recall above 95% | {_check(report.targets['sleeping_recall_above_95'])} "
        f"({_pct(report.states[AttentionState.SLEEPING].recall)}) |",
        f"| Fewer than 2 false alarms per 8-hour shift | "
        f"{_check(report.targets['false_alarms_below_2_per_shift'])} "
        f"({fa_text}) |",
        "",
        "## Event metrics per state",
        "",
        "| State | Labelled | Detected | Predicted | Recall | Precision "
        "| Mean latency | Max latency |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for state, s in report.states.items():
        lines.append(
            f"| {state.value} | {s.labelled} | {s.detected} | {s.predicted} | {_pct(s.recall)} | "
            f"{_pct(s.precision)} | {_sec(s.mean_latency_s)} | {_sec(s.max_latency_s)} |"
        )
    lines += [
        "",
        f"False alarms: {report.false_alarms} in {report.attentive_s / 3600:.2f} h of attentive "
        "footage.",
        "",
        "## Fairness slices",
        "",
        "A state is flagged when the slice's recall is more than 5 points below the",
        "overall recall.",
        "",
    ]
    if report.slices:
        header = (
            "| Tag | Value | Sessions | "
            + " | ".join(s.value for s in EVENT_STATES)
            + " | Flagged |"
        )
        lines += [header, "|---|---|---|" + "---|" * len(EVENT_STATES) + "---|"]
        for sl in report.slices:
            recalls = " | ".join(_pct(sl.recall[s]) for s in EVENT_STATES)
            flagged = ", ".join(s.value for s in sl.flagged_states) or "-"
            lines.append(f"| {sl.tag} | {sl.value} | {sl.recordings} | {recalls} | {flagged} |")
    else:
        lines.append("No tagged recordings.")
    lines += ["", "## Frame-level confusion (rows: labelled, columns: predicted)", ""]
    columns = list(AttentionState)
    lines += [
        "| | " + " | ".join(c.value for c in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for row_state in columns:
        row = report.confusion.get(row_state)
        if row is None:
            continue
        lines.append(
            f"| {row_state.value} | " + " | ".join(str(row.get(c, 0)) for c in columns) + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_baseline(report: HarnessReport, directory: Path) -> tuple[Path, Path]:
    """Write `accuracy-baseline.md` and `.json` into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    md = directory / "accuracy-baseline.md"
    js = directory / "accuracy-baseline.json"
    md.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    js.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    return md, js


def load_baseline(path: Path) -> HarnessReport:
    """Read a stored baseline JSON."""
    return HarnessReport.model_validate_json(path.read_text(encoding="utf-8"))


def compare(current: HarnessReport, baseline: HarnessReport) -> list[str]:
    """Regressions of `current` against `baseline` (empty: no regression)."""
    problems: list[str] = []
    for state in EVENT_STATES:
        now, before = current.states[state], baseline.states[state]
        for metric, limit in (
            ("recall", MAX_RECALL_DROP_POINTS),
            ("precision", MAX_PRECISION_DROP_POINTS),
        ):
            a, b = getattr(now, metric), getattr(before, metric)
            if a is not None and b is not None and (b - a) * 100 > limit + 1e-9:
                problems.append(
                    f"{state.value} {metric} fell from {b * 100:.1f}% to {a * 100:.1f}%"
                )
    fa_now, fa_before = current.false_alarms_per_shift, baseline.false_alarms_per_shift
    if (
        fa_now is not None
        and fa_before is not None
        and fa_now - fa_before > MAX_FALSE_ALARM_RISE + 1e-9
    ):
        problems.append(f"false alarms per shift rose from {fa_before:.2f} to {fa_now:.2f}")
    return problems
