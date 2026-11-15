"""Core capture types: frames, status, errors and the `CameraSource` interface."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

import numpy as np
import numpy.typing as npt

Image = npt.NDArray[np.uint8]
"""A BGR image, shape (height, width, 3), as OpenCV delivers it."""


class CaptureError(Exception):
    """A camera could not be opened or read.

    Messages must never contain a camera URI or credentials.
    """


class EndOfStream(CaptureError):  # noqa: N818 - a signal, not an error condition
    """A finite source (a video file) has no more frames. Not a failure."""


class CameraStatus(StrEnum):
    """Status of one camera as reported in health and heartbeats."""

    STARTING = "starting"
    """Not yet delivered a frame and not yet failed."""
    ONLINE = "online"
    OFFLINE = "offline"
    """Failed, hung or stale; reconnecting with backoff."""
    STOPPED = "stopped"
    """Stopped on request or at the end of a finite stream."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured frame.

    `ts` is seconds on the injected monotonic clock (the same timeline as
    `SignalFrame.ts`); `captured_at` is UTC wall time for records.
    """

    camera_slot: int
    seq: int
    """1-based, increasing per camera across reconnects."""
    ts: float
    captured_at: datetime
    image: Image = field(repr=False)


@dataclass(frozen=True, slots=True)
class CameraHealth:
    """A snapshot of one camera's capture health."""

    slot: int
    status: CameraStatus
    fps: float
    """Recent delivered frame rate; 0 when not online."""
    frames_captured: int
    frames_dropped: int
    """Frames replaced before a consumer took them (latest-frame-wins)."""
    failures: int
    """Failed open or read attempts since start."""
    reconnects: int
    """Times the camera came back online after being offline."""
    last_error: str | None
    """Exception type name of the last failure, `Hung` or `Stale`. Never a message."""
    last_frame_ts: float | None
    offline_since: float | None
    """Monotonic seconds when the current offline period began."""


class CameraSource(Protocol):
    """A camera or video that frames are read from, one at a time.

    Implementations may block in `open` and `read`; the worker runs them on
    its own thread. They may raise anything; the worker turns that into
    `offline`. A source is used once: after `close` a new one is created.
    """

    def open(self) -> None:
        """Connect to the device. Raises on failure."""
        ...

    def read(self) -> Image | None:
        """The next frame, or None when none could be read.

        Raises `EndOfStream` at the end of a finite source.
        """
        ...

    def close(self) -> None:
        """Release the device. Safe to call more than once."""
        ...


SourceFactory = Callable[[], CameraSource]
"""Makes a fresh, unopened source for each connection attempt."""
