"""Replay harness and accuracy metrics (`docs/detection-engine.md` section 9).

Labelled videos or signal traces are replayed through the pipeline with frame
time as the clock; the predicted timelines are scored against the labels and
compared with the stored baseline. Run it with `python -m ag_workstation.harness`.
"""

from ag_workstation.harness.labels import LabelFile, LabelInterval, find_labels, load_label
from ag_workstation.harness.metrics import (
    Aggregate,
    RecordingResult,
    Segment,
    StateMetrics,
    aggregate,
    evaluate_recording,
    fairness_slices,
    meets_targets,
    timeline_from_samples,
)
from ag_workstation.harness.replay import Recording, replay_traces, replay_videos
from ag_workstation.harness.report import (
    HarnessReport,
    build_report,
    compare,
    load_baseline,
    render_markdown,
    run_directory,
    write_baseline,
)
from ag_workstation.harness.traces import TraceHeader, TraceWriter, read_trace, write_trace

__all__ = [
    "Aggregate",
    "HarnessReport",
    "LabelFile",
    "LabelInterval",
    "Recording",
    "RecordingResult",
    "Segment",
    "StateMetrics",
    "TraceHeader",
    "TraceWriter",
    "aggregate",
    "build_report",
    "compare",
    "evaluate_recording",
    "fairness_slices",
    "find_labels",
    "load_baseline",
    "load_label",
    "meets_targets",
    "read_trace",
    "render_markdown",
    "replay_traces",
    "replay_videos",
    "run_directory",
    "timeline_from_samples",
    "write_baseline",
    "write_trace",
]
