"""Synthetic labelled recordings: a signal trace plus its label file.

Used by the CI tests and to produce the committed fixtures, so the harness,
the metrics and the baseline check run end to end without any video.
"""

from collections.abc import Sequence
from pathlib import Path

from ag_shared.config import CameraRole
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState

from ag_workstation.engine.synthetic import Look, trace
from ag_workstation.harness.labels import LABEL_SUFFIX, LabelFile, LabelInterval
from ag_workstation.harness.traces import TRACE_SUFFIX, TraceHeader, write_trace

Script = Sequence[tuple[Look, float, AttentionState]]
"""(what the camera sees, seconds, the state a labeller would mark)."""


def label_intervals(script: Script) -> tuple[LabelInterval, ...]:
    """The labelled intervals of a script; adjacent equal states merge."""
    intervals: list[LabelInterval] = []
    t = 0.0
    for _, seconds, state in script:
        end = round(t + seconds, 6)
        if intervals and intervals[-1].state is state:
            intervals[-1] = LabelInterval(start_s=intervals[-1].start_s, end_s=end, state=state)
        else:
            intervals.append(LabelInterval(start_s=t, end_s=end, state=state))
        t = end
    return tuple(intervals)


def write_synthetic_recording(
    directory: Path,
    name: str,
    script: Script,
    *,
    profile: PpeProfileKind = PpeProfileKind.OPEN_FACE,
    fps: float = 10.0,
    slot: int = 1,
    session: str | None = None,
    tags: dict[str, bool | str] | None = None,
    labels: Script | None = None,
) -> tuple[Path, Path]:
    """Write `<name>.trace.jsonl` and `<name>.labels.json`; returns both paths.

    `labels` lets a secondary camera share the primary's ground truth.
    """
    directory.mkdir(parents=True, exist_ok=True)
    trace_path = directory / f"{name}{TRACE_SUFFIX}"
    label_path = directory / f"{name}{LABEL_SUFFIX}"
    prof = builtin_profile(profile)
    write_trace(
        trace_path,
        TraceHeader(fps=fps, ppe_profile=profile, slots=(slot,), note="synthetic"),
        trace([(look, seconds) for look, seconds, _ in script], fps=fps, slot=slot, profile=prof),
    )
    label = LabelFile(
        video=trace_path.name,
        fps=fps,
        camera_role=CameraRole.PRIMARY if slot == 1 else CameraRole.SECONDARY,
        camera_slot=slot,
        ppe_profile=profile,
        session=session,
        tags=tags or {},
        intervals=label_intervals(labels if labels is not None else script),
    )
    label_path.write_text(label.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    return trace_path, label_path
