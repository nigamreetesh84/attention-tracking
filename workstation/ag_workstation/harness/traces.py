"""Signal traces: numeric signals recorded to a file, no images.

A trace is JSON Lines. The first line is a header; every other line is one
`SignalFrame`:

    {"format": "ag-signal-trace", "version": 1, "fps": 10.0,
     "ppe_profile": "open_face", "slots": [1, 2], "note": "..."}
    {"slot": 1, "ts": 0.0, "signals": {"face_present": [1.0, true], ...}}

Traces are used for threshold tuning in the pilot and as regression fixtures
that can be committed to git. They hold numbers and flags only: no images,
no landmarks and nothing that identifies a person.
"""

import json
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, Annotated, Literal

from ag_shared.ppe import PpeProfileKind
from ag_shared.signals import SignalFrame, SignalName, SignalReading
from pydantic import BaseModel, ConfigDict, Field

TRACE_SUFFIX = ".trace.jsonl"
FORMAT: Literal["ag-signal-trace"] = "ag-signal-trace"


class TraceHeader(BaseModel):
    """First line of a trace file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["ag-signal-trace"] = FORMAT
    version: Literal[1] = 1
    fps: Annotated[float, Field(gt=0, le=120)]
    ppe_profile: PpeProfileKind
    slots: tuple[Annotated[int, Field(ge=1, le=4)], ...]
    note: str = ""


def frame_to_json(frame: SignalFrame) -> str:
    """One trace line."""
    return json.dumps(
        {
            "slot": frame.camera_slot,
            "ts": round(frame.ts, 6),
            "signals": {
                name.value: [round(r.value, 6), r.valid] for name, r in frame.signals.items()
            },
        },
        separators=(",", ":"),
    )


def frame_from_json(line: str) -> SignalFrame:
    """Parse one trace line."""
    raw = json.loads(line)
    return SignalFrame(
        camera_slot=raw["slot"],
        ts=float(raw["ts"]),
        signals={
            SignalName(name): SignalReading(value=float(value), valid=bool(valid))
            for name, (value, valid) in raw["signals"].items()
        },
    )


class TraceWriter:
    """Writes a trace. Thread-safe; usable as a pipeline `signal_listener`.

    `max_frames` bounds the file (production recording has size limits).
    """

    def __init__(
        self, stream: IO[str], header: TraceHeader, *, max_frames: int | None = None
    ) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._max = max_frames
        self.frames_written = 0
        self.frames_dropped = 0
        stream.write(header.model_dump_json() + "\n")

    def __call__(self, frame: SignalFrame) -> None:
        """Append one frame (or count it as dropped past the limit)."""
        with self._lock:
            if self._max is not None and self.frames_written >= self._max:
                self.frames_dropped += 1
                return
            self._stream.write(frame_to_json(frame) + "\n")
            self.frames_written += 1


def write_trace(path: Path, header: TraceHeader, frames: Iterable[SignalFrame]) -> int:
    """Write a whole trace file; returns the number of frames."""
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        writer = TraceWriter(stream, header)
        for frame in frames:
            writer(frame)
        return writer.frames_written


def read_trace(path: Path) -> tuple[TraceHeader, list[SignalFrame]]:
    """Read a trace file: its header and frames in file order."""
    with path.open(encoding="utf-8") as stream:
        lines = iter(stream)
        header = TraceHeader.model_validate_json(next(lines))
        frames = [frame_from_json(line) for line in lines if line.strip()]
    return header, frames


def iter_trace(path: Path) -> Iterator[SignalFrame]:
    """Stream the frames of a trace file."""
    with path.open(encoding="utf-8") as stream:
        TraceHeader.model_validate_json(next(stream))
        for line in stream:
            if line.strip():
                yield frame_from_json(line)
