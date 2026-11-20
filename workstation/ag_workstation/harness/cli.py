"""Command line for the replay harness.

    python -m ag_workstation.harness run DIR [--models DIR] [--baseline FILE]
        [--write-baseline DOCS_DIR] [--json FILE] [--tolerance S]

Replays every labelled recording under DIR (videos need the MediaPipe model
files, from `--models` or `AG_MODEL_DIR`), prints the headline metrics,
optionally writes the baseline documents, and exits with status 1 when a
`--baseline` comparison finds a regression.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ag_shared.config import CameraConfig

from ag_workstation.harness.metrics import DEFAULT_TOLERANCE_S
from ag_workstation.harness.report import (
    ProviderFactory,
    compare,
    load_baseline,
    render_markdown,
    run_directory,
    write_baseline,
)
from ag_workstation.landmarks.types import LandmarkProvider


def mediapipe_factory(model_dir: Path) -> ProviderFactory:
    """Landmark providers on the MediaPipe models in `model_dir`."""

    def factory(camera: CameraConfig) -> LandmarkProvider:
        from ag_workstation.landmarks.mediapipe_provider import (
            MediaPipeLandmarkProvider,
            model_paths,
        )

        return MediaPipeLandmarkProvider(model_paths(model_dir))

    return factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ag_workstation.harness", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="replay labelled recordings and score them")
    run.add_argument("directory", type=Path)
    run.add_argument("--models", type=Path, default=None, help="MediaPipe .task directory")
    run.add_argument("--baseline", type=Path, default=None, help="baseline JSON to compare with")
    run.add_argument("--write-baseline", type=Path, default=None, metavar="DOCS_DIR")
    run.add_argument("--json", type=Path, default=None, help="write the report JSON here")
    run.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_S)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit status."""
    args = _parser().parse_args(argv)
    models = args.models or (
        Path(os.environ["AG_MODEL_DIR"]) if os.environ.get("AG_MODEL_DIR") else None
    )
    report = run_directory(
        args.directory,
        provider_factory=mediapipe_factory(models) if models is not None else None,
        tolerance_s=args.tolerance,
    )
    sys.stdout.write(render_markdown(report))
    if args.json is not None:
        args.json.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if args.write_baseline is not None:
        md, js = write_baseline(report, args.write_baseline)
        sys.stdout.write(f"\nWrote {md} and {js}\n")
    if args.baseline is not None:
        problems = compare(report, load_baseline(args.baseline))
        if problems:
            sys.stdout.write("\nREGRESSION against the baseline:\n")
            for problem in problems:
                sys.stdout.write(f"- {problem}\n")
            return 1
        sys.stdout.write("\nNo regression against the baseline.\n")
    return 0
