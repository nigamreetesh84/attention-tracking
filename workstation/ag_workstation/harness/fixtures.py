"""The committed synthetic trace fixtures and the baseline built from them.

    python -m ag_workstation.harness.fixtures [TRACE_DIR] [DOCS_DIR]

regenerates `tests/fixtures/traces/` and `docs/accuracy-baseline.{md,json}`.
The output is deterministic; a test checks that the committed files match.
These traces exercise the harness and pin the engine's behaviour on scripted
scenarios. They are not a substitute for the labelled video set: their
accuracy numbers say nothing about real cameras or real people.
"""

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ag_shared.ppe import PpeProfileKind
from ag_shared.states import AttentionState

from ag_workstation.engine.synthetic import (
    ATTENTIVE,
    EYES_CLOSED,
    HEAD_DOWN_STILL,
    LOOKING_AWAY,
    NO_FACE,
)
from ag_workstation.harness.report import HarnessReport, run_directory, write_baseline
from ag_workstation.harness.synthetic import Script, write_synthetic_recording

A = AttentionState
K = PpeProfileKind

FIXTURE_FPS = 5.0
BASELINE_TIME = datetime(2026, 11, 20, tzinfo=UTC)
"""Fixed so that regenerating the baseline is byte-identical."""

GLANCE = LOOKING_AWAY.with_(yaw=40.0)
BLINK_RUN = EYES_CLOSED
YAWN = ATTENTIVE.with_(yawn=True, mar=0.8)
HEAVY_EYES = ATTENTIVE.with_(perclos=0.45)
GLARE_HEAD_DOWN = HEAD_DOWN_STILL.with_(eyes_closed=None, ear=None, perclos=None)
EYES_SHUT_HEAD_DOWN = HEAD_DOWN_STILL.with_(eyes_closed=True, ear=0.3, motion=0.2)


@dataclass(frozen=True)
class Fixture:
    """One synthetic recording (or one camera of a paired session)."""

    name: str
    script: Script
    profile: PpeProfileKind = K.OPEN_FACE
    slot: int = 1
    session: str | None = None
    tags: dict[str, bool | str] = field(default_factory=dict)
    labels: Script | None = None


_PAIRED_PRIMARY: Script = [
    (ATTENTIVE, 15, A.ATTENTIVE),
    (NO_FACE, 15, A.ATTENTIVE),  # camera 1 blocked; camera 2 sees the worker
    (ATTENTIVE, 10, A.ATTENTIVE),
    (EYES_CLOSED, 8, A.SLEEPING),
    (ATTENTIVE, 12, A.ATTENTIVE),
]
_PAIRED_SECONDARY: Script = [
    (ATTENTIVE, 40, A.ATTENTIVE),
    (EYES_CLOSED, 8, A.SLEEPING),
    (ATTENTIVE, 12, A.ATTENTIVE),
]

FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        "open_face_sleep",
        [
            (ATTENTIVE, 20, A.ATTENTIVE),
            (EYES_CLOSED, 8, A.SLEEPING),
            (ATTENTIVE, 20, A.ATTENTIVE),
            (BLINK_RUN, 3, A.ATTENTIVE),  # a long blink: below the 4 s timer
            (ATTENTIVE, 15, A.ATTENTIVE),
        ],
        tags={"glasses": False, "lighting": "normal"},
    ),
    Fixture(
        "open_face_distracted_absent",
        [
            (ATTENTIVE, 15, A.ATTENTIVE),
            (LOOKING_AWAY, 9, A.DISTRACTED),
            (ATTENTIVE, 15, A.ATTENTIVE),
            (GLANCE, 3, A.ATTENTIVE),  # a short glance: below the 5 s timer
            (ATTENTIVE, 10, A.ATTENTIVE),
            (NO_FACE, 15, A.ABSENT),
            (ATTENTIVE, 12, A.ATTENTIVE),
        ],
        tags={"glasses": False, "lighting": "low"},
    ),
    Fixture(
        "open_face_drowsy",
        [
            (ATTENTIVE, 15, A.ATTENTIVE),
            (HEAVY_EYES, 15, A.DROWSY),
            (ATTENTIVE, 15, A.ATTENTIVE),
            (YAWN, 2, A.DROWSY),
            (ATTENTIVE, 4, A.DROWSY),
            (YAWN, 2, A.DROWSY),
            (ATTENTIVE, 4, A.DROWSY),
            (YAWN, 2, A.DROWSY),
            (ATTENTIVE, 15, A.ATTENTIVE),
        ],
        tags={"glasses": False, "lighting": "normal"},
    ),
    Fixture(
        "clear_glasses_glare",
        [
            (ATTENTIVE, 15, A.ATTENTIVE),
            (EYES_CLOSED, 7, A.SLEEPING),
            (ATTENTIVE, 15, A.ATTENTIVE),
            (GLARE_HEAD_DOWN, 12, A.SLEEPING),  # eyes unreadable: head-pose fallback
            (ATTENTIVE, 15, A.ATTENTIVE),
        ],
        profile=K.CLEAR_GLASSES,
        tags={"glasses": True, "lighting": "normal"},
    ),
    Fixture(
        "sunglasses_head_drop",
        [
            (ATTENTIVE, 15, A.ATTENTIVE),
            (HEAD_DOWN_STILL, 12, A.SLEEPING),
            (ATTENTIVE, 15, A.ATTENTIVE),
            (EYES_CLOSED, 6, A.ATTENTIVE),  # eyes are not read with sunglasses
            (ATTENTIVE, 10, A.ATTENTIVE),
        ],
        profile=K.SUNGLASSES,
        tags={"glasses": True, "lighting": "bright"},
    ),
    Fixture(
        "mask_sleep",
        [
            (ATTENTIVE, 15, A.ATTENTIVE),
            (EYES_CLOSED, 7, A.SLEEPING),
            (ATTENTIVE, 15, A.ATTENTIVE),
            (YAWN, 2, A.ATTENTIVE),  # yawning is not read under a mask
            (ATTENTIVE, 4, A.ATTENTIVE),
            (YAWN, 2, A.ATTENTIVE),
            (ATTENTIVE, 4, A.ATTENTIVE),
            (YAWN, 2, A.ATTENTIVE),
            (ATTENTIVE, 10, A.ATTENTIVE),
        ],
        profile=K.MASK,
        tags={"glasses": False, "lighting": "normal"},
    ),
    Fixture(
        "helmet_sleep",
        [
            (ATTENTIVE, 15, A.ATTENTIVE),
            (EYES_SHUT_HEAD_DOWN, 7, A.SLEEPING),
            (ATTENTIVE, 15, A.ATTENTIVE),
            (HEAD_DOWN_STILL, 11, A.SLEEPING),
            (ATTENTIVE, 15, A.ATTENTIVE),
        ],
        profile=K.HELMET_SHIELD,
        tags={"glasses": False, "lighting": "low"},
    ),
    Fixture(
        "paired_cam1",
        _PAIRED_PRIMARY,
        slot=1,
        session="paired_two_cameras",
        tags={"glasses": False, "lighting": "normal"},
    ),
    Fixture(
        "paired_cam2",
        _PAIRED_SECONDARY,
        slot=2,
        session="paired_two_cameras",
        labels=_PAIRED_PRIMARY,
    ),
    Fixture(
        "attentive_long",
        [
            *[(ATTENTIVE, 25, A.ATTENTIVE), (GLANCE, 2, A.ATTENTIVE), (BLINK_RUN, 0.4, A.ATTENTIVE)]
            * 10,
            (ATTENTIVE, 20, A.ATTENTIVE),
        ],
        tags={"glasses": False, "lighting": "normal"},
    ),
)


def write_fixtures(directory: Path, fixtures: Sequence[Fixture] = FIXTURES) -> list[Path]:
    """Write every fixture's trace and label into `directory`."""
    paths: list[Path] = []
    for fx in fixtures:
        paths.extend(
            write_synthetic_recording(
                directory,
                fx.name,
                fx.script,
                profile=fx.profile,
                fps=FIXTURE_FPS,
                slot=fx.slot,
                session=fx.session,
                tags=fx.tags,
                labels=fx.labels,
            )
        )
    return paths


def fixture_report(directory: Path) -> HarnessReport:
    """The harness report of the fixtures, stamped with the fixed baseline time."""
    return run_directory(directory, now=BASELINE_TIME)


def main(argv: Sequence[str] | None = None) -> int:
    """Regenerate the fixtures and the baseline documents."""
    args = list(sys.argv[1:] if argv is None else argv)
    traces = Path(args[0]) if args else Path("tests/fixtures/traces")
    docs = Path(args[1]) if len(args) > 1 else Path("docs")
    write_fixtures(traces)
    md, js = write_baseline(fixture_report(traces), docs)
    sys.stdout.write(f"Wrote fixtures to {traces} and the baseline to {md} and {js}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
