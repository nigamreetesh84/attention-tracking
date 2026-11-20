"""Committed fixtures and the committed baseline stay in step with the code."""

from pathlib import Path

from ag_workstation.harness.fixtures import FIXTURES, fixture_report, main, write_fixtures
from ag_workstation.harness.labels import find_labels
from ag_workstation.harness.report import compare, load_baseline, render_markdown
from ag_workstation.harness.traces import read_trace

REPO = Path(__file__).resolve().parents[2]
TRACES = REPO / "tests" / "fixtures" / "traces"
LABELS = REPO / "tests" / "fixtures" / "labels"
DOCS = REPO / "docs"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_committed_fixtures_match_the_generator(tmp_path: Path) -> None:
    written = write_fixtures(tmp_path)
    assert len(written) == 2 * len(FIXTURES)
    committed = sorted(p.name for p in TRACES.iterdir())
    assert committed == sorted(p.name for p in written)
    for path in written:
        assert _text(path) == _text(TRACES / path.name), path.name


def test_fixture_labels_and_traces_are_valid() -> None:
    found = find_labels(TRACES)
    assert len(found) == len(FIXTURES)
    for path, label in found:
        header, frames = read_trace(path.parent / label.video)
        assert header.ppe_profile is label.ppe_profile
        assert frames, path.name
        assert frames[-1].ts < label.duration_s


def test_sample_video_labels_validate() -> None:
    found = find_labels(LABELS)
    assert len(found) == 3
    sessions = {label.session for _, label in found}
    assert "ws1_0007" in sessions


def test_harness_output_matches_the_committed_baseline() -> None:
    report = fixture_report(TRACES)
    stored = load_baseline(DOCS / "accuracy-baseline.json")
    assert compare(report, stored) == []
    assert report == stored
    assert render_markdown(report) == _text(DOCS / "accuracy-baseline.md")


def test_regenerate_command(tmp_path: Path) -> None:
    assert main([str(tmp_path / "traces"), str(tmp_path / "docs")]) == 0
    assert (tmp_path / "docs" / "accuracy-baseline.md").is_file()
    assert len(list((tmp_path / "traces").iterdir())) == 2 * len(FIXTURES)
