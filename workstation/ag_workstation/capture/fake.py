"""A controllable fake camera for tests, demos and CI without hardware."""

import threading

import numpy as np

from ag_workstation.capture.types import CaptureError, EndOfStream, Image


class FakeCamera:
    """A fake device whose behaviour tests switch at run time.

    Every source made by `make_source` shares this state, as reconnects to a
    real device would. Frame `n` (1-based, counted across sources) is filled
    with the value `n % 256`, so tests can tell frames apart.
    """

    def __init__(
        self,
        *,
        width: int = 8,
        height: int = 6,
        frames: int | None = None,
        fps: float | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.frames_left = frames
        """None means endless; at 0 a read raises `EndOfStream`."""
        self.fps = fps
        """When set, each read waits 1/fps real seconds (keeps threads calm)."""
        self.available = True
        """False: `open` fails."""
        self.failing = False
        """True: `read` raises."""
        self.return_none = False
        """True: `read` returns None."""
        self.fail_close = False
        """True: `close` raises (after counting)."""
        self.opens = 0
        self.closes = 0
        self.reads = 0
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._gate.set()

    def hang(self) -> None:
        """Make reads block until `release`."""
        self._gate.clear()

    def release(self) -> None:
        """Let blocked reads continue."""
        self._gate.set()

    def make_source(self) -> "FakeSource":
        """A new, unopened source on this device."""
        return FakeSource(self)

    def _open(self) -> None:
        if not self.available:
            raise CaptureError("fake camera unavailable")
        with self._lock:
            self.opens += 1

    def _read(self) -> Image | None:
        self._gate.wait()
        if self.fps:
            threading.Event().wait(1.0 / self.fps)
        if self.failing:
            raise CaptureError("fake read failure")
        if self.return_none:
            return None
        with self._lock:
            if self.frames_left is not None:
                if self.frames_left <= 0:
                    raise EndOfStream("fake stream ended")
                self.frames_left -= 1
            self.reads += 1
            value = self.reads % 256
        return np.full((self.height, self.width, 3), value, dtype=np.uint8)

    def _close(self) -> None:
        with self._lock:
            self.closes += 1
        if self.fail_close:
            raise CaptureError("fake close failure")


class FakeSource:
    """A `CameraSource` on a `FakeCamera`."""

    def __init__(self, camera: FakeCamera | None = None) -> None:
        self.camera = camera if camera is not None else FakeCamera()
        self._open = False

    def open(self) -> None:
        """Open; fails when the camera is unavailable."""
        self.camera._open()
        self._open = True

    def read(self) -> Image | None:
        """Next fake frame (see `FakeCamera` switches)."""
        if not self._open:
            raise CaptureError("source is not open")
        return self.camera._read()

    def close(self) -> None:
        """Close once; later calls do nothing."""
        if self._open:
            self._open = False
            self.camera._close()
