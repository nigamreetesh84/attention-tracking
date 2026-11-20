"""Labels, traces, replay, report, baseline comparison and the CLI."""

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from ag_shared.config import CameraConfig
from ag_shared.ppe import PpeProfileKind
from ag_shared.states import AttentionState
from ag_shared.thresholds import Thresholds
from ag_workstation.capture.types import Frame, Image
from ag_workstation.engine.synthetic import (
    ATTENTIVE,
    EYES_CLOSED,
    LOOKING_AWAY,
    NO_FACE,
    make_frame,
)
from ag_workstation.harness import cli
from ag_workstation.harness.labels import LabelFile, find_labels, load_label
from ag_workstation.harness.replay import (
    Recording,
    opencv_reader,
    replay_videos,
    session_config,
)
from ag_workstation.harness.report import (
    compare,
    group_sessions,
    load_baseline,
    render_markdown,
    run_directory,
    write_baseline,
)
from ag_workstation.harness.synthetic import write_synthetic_recording
from ag_workstation.harness.traces import (
    TraceHeader,
    TraceWriter,
    frame_from_json,
    frame_to_json,
    read_trace,
)
from ag_workstation.landmarks import FakeLandmarkProvider, LandmarkError
from ag_workstation.landmarks.fake import synthetic_face, synthetic_result
from ag_workstation.landmarks.types import LandmarkResult
from pydantic import ValidationError

A = AttentionState
NOW = datetime(2026, 11, 20, tzinfo=UTC)

SCRIPT = [
    (ATTENTIVE, 20.0, A.ATTENTIVE),
    (EYES_CLOSED, 8.0, A.SLEEPING),
    (ATTENTIVE, 20.0, A.ATTENTIVE),
    (LOOKING_AWAY, 8.0, A.DISTRACTED),
    (ATTENTIVE, 20.0, A.ATTENTIVE),
    (NO_FACE, 14.0, A.ABSENT),
    (ATTENTIVE, 10.0, A.ATTENTIVE),
]


# --- labels ---


def _label(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "video": "ws1_cam1_0007.mp4",
        "fps": 15,
        "camera_role": "primary",
        "ppe_profile": "open_face",
        "tags": {"glasses": False, "lighting": "low", "age_band": "30-45"},
        "intervals": [
            {"start_s": 0.0, "end_s": 41.2, "state": "attentive"},
            {"start_s": 41.2, "end_s": 50.0, "state": "sleeping"},
        ],
    }
    base.update(changes)
    return base


def test_label_file_from_the_design_doc_parses() -> None:
    label = LabelFile.model_validate(_label())
    assert label.slot == 1
    assert label.duration_s == 50.0
    assert label.state_at(41.0) is A.ATTENTIVE
    assert label.state_at(41.2) is A.SLEEPING
    assert label.state_at(50.0) is None
    secondary = LabelFile.model_validate(_label(camera_role="secondary"))
    assert secondary.slot == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"video": "../secret.mp4"},
        {"video": "a/b.mp4"},
        {"intervals": []},
        {"intervals": [{"start_s": 5.0, "end_s": 5.0, "state": "attentive"}]},
        {"intervals": [{"start_s": 0.0, "end_s": 5.0, "state": "on_break"}]},
        {
            "intervals": [
                {"start_s": 0.0, "end_s": 5.0, "state": "attentive"},
                {"start_s": 4.0, "end_s": 6.0, "state": "sleeping"},
            ]
        },
        {"fps": 0},
        {"name": "Ravi"},
    ],
)
def test_invalid_labels_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LabelFile.model_validate(_label(**changes))


def test_find_and_load_labels(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.labels.json").write_text(json.dumps(_label()), encoding="utf-8")
    (tmp_path / "ignored.json").write_text("{}", encoding="utf-8")
    found = find_labels(tmp_path)
    assert [p.name for p, _ in found] == ["a.labels.json"]
    assert load_label(found[0][0]).video == "ws1_cam1_0007.mp4"


# --- traces ---


def test_trace_round_trip_keeps_values_and_validity(tmp_path: Path) -> None:
    frame = make_frame(EYES_CLOSED.with_(yaw=None), 1.25, slot=2)
    assert frame_from_json(frame_to_json(frame)) == frame
    path, _ = write_synthetic_recording(tmp_path, "r", SCRIPT[:2])
    header, frames = read_trace(path)
    assert header.ppe_profile is PpeProfileKind.OPEN_FACE
    assert len(frames) == 280
    assert frames[-1].ts == pytest.approx(27.9)


def test_trace_holds_numbers_only(tmp_path: Path) -> None:
    path, _ = write_synthetic_recording(tmp_path, "r", SCRIPT[:2])
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        raw = json.loads(line)
        assert set(raw) == {"slot", "ts", "signals"}
        for value, valid in raw["signals"].values():
            assert isinstance(value, float | int)
            assert isinstance(valid, bool)


def test_trace_writer_limit() -> None:
    stream = io.StringIO()
    writer = TraceWriter(
        stream, TraceHeader(fps=10, ppe_profile=PpeProfileKind.MASK, slots=(1,)), max_frames=2
    )
    for i in range(5):
        writer(make_frame(ATTENTIVE, i / 10))
    assert (writer.frames_written, writer.frames_dropped) == (2, 3)
    assert len(stream.getvalue().splitlines()) == 3


# --- replay of traces ---


def test_synthetic_trace_replay_scores_perfectly(tmp_path: Path) -> None:
    write_synthetic_recording(tmp_path, "s1", SCRIPT, tags={"glasses": False})
    report = run_directory(tmp_path, now=NOW)
    assert report.source == "trace"
    assert report.sessions == 1
    for state in (A.SLEEPING, A.DISTRACTED, A.ABSENT):
        assert report.states[state].recall == 1.0, state
        assert report.states[state].precision == 1.0, state
    assert report.states[A.SLEEPING].mean_latency_s == pytest.approx(4.1, abs=0.05)
    assert report.states[A.ABSENT].mean_latency_s == pytest.approx(10.1, abs=0.05)
    assert report.false_alarms == 0
    assert report.targets == {
        "sleeping_recall_above_95": True,
        "false_alarms_below_2_per_shift": True,
    }


def test_paired_session_fuses_two_cameras(tmp_path: Path) -> None:
    # Camera 1 loses the face; camera 2 still sees it: the station is not Absent.
    primary = [(ATTENTIVE, 5.0, A.ATTENTIVE), (NO_FACE, 15.0, A.ATTENTIVE)]
    secondary = [(ATTENTIVE, 20.0, A.ATTENTIVE)]
    write_synthetic_recording(tmp_path, "p", primary, slot=1, session="s")
    write_synthetic_recording(tmp_path, "q", secondary, slot=2, session="s", labels=primary)
    sessions = group_sessions(tmp_path)
    assert list(sessions) == ["s"]
    report = run_directory(tmp_path, now=NOW)
    assert report.states[A.ABSENT].predicted == 0
    assert report.false_alarms == 0
    # The same primary alone does go Absent.
    alone = tmp_path / "alone"
    write_synthetic_recording(alone, "p", primary, slot=1)
    assert run_directory(alone, now=NOW).states[A.ABSENT].predicted == 1


def test_thresholds_change_the_result(tmp_path: Path) -> None:
    write_synthetic_recording(tmp_path, "s1", SCRIPT)
    strict = run_directory(tmp_path, thresholds=Thresholds(sleeping_eyes_closed_s=10.0), now=NOW)
    assert strict.states[A.SLEEPING].recall == 0.0


def test_session_config_rules(tmp_path: Path) -> None:
    write_synthetic_recording(tmp_path, "a", SCRIPT[:1], slot=1, session="s")
    write_synthetic_recording(
        tmp_path, "b", SCRIPT[:1], slot=2, session="s", profile=PpeProfileKind.MASK
    )
    with pytest.raises(ValueError, match="PPE profile"):
        session_config(group_sessions(tmp_path)["s"])
    label = load_label(tmp_path / "a.labels.json")
    rec = Recording(label, tmp_path / label.video)
    with pytest.raises(ValueError, match="different camera slots"):
        session_config([rec, rec])
    secondary_only = Recording(
        label.model_copy(update={"camera_role": "secondary", "camera_slot": 3}), rec.path
    )
    assert session_config([secondary_only]).primary_camera.slot == 3


def test_mixed_session_and_videos_without_models_are_refused(tmp_path: Path) -> None:
    write_synthetic_recording(tmp_path, "a", SCRIPT[:1], session="s")
    video_label = LabelFile.model_validate(
        _label(video="b.mp4", camera_role="secondary", session="s")
    )
    (tmp_path / "b.labels.json").write_text(video_label.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="mixes"):
        run_directory(tmp_path)
    (tmp_path / "a.labels.json").unlink()
    with pytest.raises(ValueError, match="landmark provider"):
        run_directory(tmp_path)


# --- replay of videos (fake reader and provider) ---


def _video_reader(frames: int) -> object:
    def reader(path: Path) -> Iterator[Image]:
        for i in range(frames):
            yield np.full((6, 8, 3), i % 256, dtype=np.uint8)

    return reader


def _eyes_by_time(frame: Frame, with_pose: bool) -> LandmarkResult:
    if 3.0 <= frame.ts < 3.3:
        raise LandmarkError("unreadable frame")
    ear = 0.05 if 10.0 <= frame.ts < 18.0 else 0.30
    return synthetic_result(
        frame.ts, (synthetic_face(ear=ear),), camera_slot=frame.camera_slot, width=8, height=6
    )


def test_video_replay_through_the_landmark_model(tmp_path: Path) -> None:
    label = LabelFile.model_validate(
        _label(
            video="v.mp4",
            fps=10,
            intervals=[
                {"start_s": 0.0, "end_s": 10.0, "state": "attentive"},
                {"start_s": 10.0, "end_s": 18.0, "state": "sleeping"},
                {"start_s": 18.0, "end_s": 30.0, "state": "attentive"},
            ],
        )
    )
    providers: list[FakeLandmarkProvider] = []

    def factory(camera: CameraConfig) -> FakeLandmarkProvider:
        providers.append(FakeLandmarkProvider(_eyes_by_time))
        return providers[-1]

    seen: list[float] = []
    result = replay_videos(
        "v",
        [Recording(label, tmp_path / "v.mp4")],
        factory,
        reader=_video_reader(300),  # type: ignore[arg-type]  # a plain generator function
        signal_listener=lambda f: seen.append(f.ts),
    )
    states = {s.state for s in result.predicted}
    assert A.SLEEPING in states
    assert providers[0].closed
    assert len(seen) == 297  # three unreadable frames skipped
    (tmp_path / "v.labels.json").write_text(label.model_dump_json(), encoding="utf-8")
    report = run_directory(
        tmp_path,
        provider_factory=factory,
        reader=_video_reader(300),  # type: ignore[arg-type]
        now=NOW,
    )
    assert report.source == "video"
    assert report.states[A.SLEEPING].recall == 1.0


def test_opencv_reader_reads_a_real_file(tmp_path: Path) -> None:
    import cv2

    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), 10.0, (16, 12))
    for i in range(5):
        writer.write(np.full((12, 16, 3), i * 40, dtype=np.uint8))
    writer.release()
    assert len(list(opencv_reader(path))) == 5


# --- report and baseline ---


def test_report_markdown_and_baseline_round_trip(tmp_path: Path) -> None:
    write_synthetic_recording(tmp_path / "rec", "s1", SCRIPT, tags={"lighting": "low"})
    report = run_directory(tmp_path / "rec", now=NOW)
    md, js = write_baseline(report, tmp_path / "docs")
    text = md.read_text(encoding="utf-8")
    assert "# Accuracy baseline" in text
    assert "| sleeping | 1 | 1 | 1 | 100.0% | 100.0%" in text
    assert "| lighting | low | 1 |" in text
    assert load_baseline(js) == report
    assert compare(report, report) == []


def test_compare_finds_regressions(tmp_path: Path) -> None:
    write_synthetic_recording(tmp_path, "s1", SCRIPT)
    good = run_directory(tmp_path, now=NOW)
    worse = run_directory(tmp_path, thresholds=Thresholds(sleeping_eyes_closed_s=10.0), now=NOW)
    problems = compare(worse, good)
    assert problems == ["sleeping recall fell from 100.0% to 0.0%"]
    noisy = good.model_copy(update={"false_alarms_per_shift": 3.0})
    assert compare(noisy, good) == ["false alarms per shift rose from 0.00 to 3.00"]


def test_markdown_without_tags_or_data() -> None:
    from ag_workstation.harness.report import build_report

    text = render_markdown(build_report([], set(), now=NOW))
    assert "No tagged recordings." in text
    assert "no data" in text


# --- CLI ---


def test_cli_writes_and_checks_the_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AG_MODEL_DIR", raising=False)
    rec = tmp_path / "rec"
    write_synthetic_recording(rec, "s1", SCRIPT)
    docs = tmp_path / "docs"
    out_json = tmp_path / "report.json"
    assert cli.main(["run", str(rec), "--write-baseline", str(docs), "--json", str(out_json)]) == 0
    assert out_json.is_file()
    assert cli.main(["run", str(rec), "--baseline", str(docs / "accuracy-baseline.json")]) == 0
    assert "No regression" in capsys.readouterr().out
    # Make the stored baseline better than reality: the run must fail.
    baseline = load_baseline(docs / "accuracy-baseline.json")
    states = dict(baseline.states)
    states[A.DROWSY] = states[A.DROWSY].model_copy(update={"recall": 1.0})
    better = baseline.model_copy(update={"false_alarms_per_shift": -5.0, "states": states})
    (docs / "accuracy-baseline.json").write_text(better.model_dump_json(), encoding="utf-8")
    assert cli.main(["run", str(rec), "--baseline", str(docs / "accuracy-baseline.json")]) == 1
    assert "REGRESSION" in capsys.readouterr().out


def test_cli_uses_the_model_dir_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_synthetic_recording(tmp_path, "s1", SCRIPT[:1])
    monkeypatch.setenv("AG_MODEL_DIR", str(tmp_path / "models"))
    assert cli.main(["run", str(tmp_path)]) == 0
    factory = cli.mediapipe_factory(tmp_path / "models")
    provider = factory(session_config(group_sessions(tmp_path)["s1"]).primary_camera)
    assert provider is not None
    provider.close()
