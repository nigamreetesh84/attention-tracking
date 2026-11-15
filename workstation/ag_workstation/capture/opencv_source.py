"""OpenCV-backed sources. The only capture module that imports OpenCV.

`OpenCvSource` covers USB, built-in and RTSP cameras; `FileSource` replays a
video file. Error messages and reprs never include the camera URI.
"""

import time
from collections.abc import Callable
from pathlib import Path

import cv2

from ag_workstation.capture.types import CaptureError, EndOfStream, Image
from ag_workstation.clock import Clock, SystemClock

_DEFAULT_FILE_FPS = 15.0


def _as_image(frame: object) -> Image:
    # cv2 returns MatLike; capture always delivers 8-bit BGR.
    return frame  # type: ignore[return-value]  # MatLike is an 8-bit ndarray here


class OpenCvSource:
    """A live camera through `cv2.VideoCapture`.

    `locator` is a device index (USB, built-in) or a stream URI (RTSP). For
    URIs the FFmpeg backend is used with open and read timeouts so a dead
    stream fails instead of blocking forever.
    """

    def __init__(
        self,
        locator: int | str,
        *,
        open_timeout_ms: int = 5000,
        read_timeout_ms: int = 5000,
    ) -> None:
        self.locator = locator
        self._open_timeout_ms = open_timeout_ms
        self._read_timeout_ms = read_timeout_ms
        self._cap: cv2.VideoCapture | None = None

    def __repr__(self) -> str:
        if isinstance(self.locator, int):
            return f"OpenCvSource(device={self.locator})"
        return "OpenCvSource(<uri hidden>)"

    def open(self) -> None:
        """Open the device or stream; raises `CaptureError` if it does not open."""
        if isinstance(self.locator, int):
            cap = cv2.VideoCapture(self.locator)
        else:
            params = [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                self._open_timeout_ms,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                self._read_timeout_ms,
            ]
            cap = cv2.VideoCapture(self.locator, cv2.CAP_FFMPEG, params)
        if not cap.isOpened():
            cap.release()
            raise CaptureError("camera did not open")
        self._cap = cap

    def read(self) -> Image | None:
        """The next frame, or None when the read failed."""
        if self._cap is None:
            raise CaptureError("source is not open")
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        return _as_image(frame)

    def close(self) -> None:
        """Release the device."""
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()


class FileSource:
    """Replays a video file, optionally paced to its frame rate.

    With `realtime`, frame `n` is delivered no earlier than `n / fps` seconds
    after the first, using the injected clock and sleep. At the end it loops
    or raises `EndOfStream`.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        loop: bool = False,
        realtime: bool = True,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self.loop = loop
        self.realtime = realtime
        self.fps = _DEFAULT_FILE_FPS
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._cap: cv2.VideoCapture | None = None
        self._start = 0.0
        self._index = 0

    def open(self) -> None:
        """Open the file; raises `CaptureError` if it cannot be read."""
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            cap.release()
            raise CaptureError("video file did not open")
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        self.fps = fps if fps > 0 else _DEFAULT_FILE_FPS
        self._cap = cap
        self._start = self._clock.monotonic()
        self._index = 0

    def read(self) -> Image | None:
        """The next frame of the file."""
        if self._cap is None:
            raise CaptureError("source is not open")
        if self.realtime:
            due = self._start + self._index / self.fps
            delay = due - self._clock.monotonic()
            if delay > 0:
                self._sleep(delay)
        ok, frame = self._cap.read()
        if not ok and self.loop:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok:
                raise CaptureError("video file has no frames")
        if not ok:
            raise EndOfStream("end of video file")
        self._index += 1
        return _as_image(frame)

    def close(self) -> None:
        """Release the file."""
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()
