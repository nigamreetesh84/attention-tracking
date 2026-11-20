"""Accuracy on the labelled video set (marker `video`; needs AG_TEST_VIDEOS).

Run with `pytest -m video`. Needs `AG_TEST_VIDEOS` (the labelled videos) and
`AG_MODEL_DIR` (the MediaPipe `.task` files). The run fails when accuracy
regresses against `docs/accuracy-baseline.json`; with no baseline stored yet
it writes one to the directory in `AG_BASELINE_OUT` if that is set.
"""

import os
from pathlib import Path

import pytest
from ag_workstation.harness.cli import mediapipe_factory
from ag_workstation.harness.report import compare, load_baseline, run_directory, write_baseline

pytestmark = pytest.mark.video

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "docs" / "accuracy-baseline.json"


def _dir(env: str) -> Path:
    value = os.environ.get(env, "").strip()
    if not value or not Path(value).is_dir():
        pytest.skip(f"{env} does not point at a directory")
    return Path(value)


def test_video_set_against_the_baseline() -> None:
    videos = _dir("AG_TEST_VIDEOS")
    models = _dir("AG_MODEL_DIR")
    report = run_directory(videos, provider_factory=mediapipe_factory(models))
    assert report.sessions > 0, "no *.labels.json found under AG_TEST_VIDEOS"
    if not BASELINE.is_file() or load_baseline(BASELINE).source != "video":
        out = os.environ.get("AG_BASELINE_OUT")
        if out:
            write_baseline(report, Path(out))
        pytest.skip("no video baseline stored yet; set AG_BASELINE_OUT to write one")
    assert compare(report, load_baseline(BASELINE)) == []
