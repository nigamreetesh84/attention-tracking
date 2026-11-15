"""Latest-frame-wins hand-off between a capture thread and its consumer."""

import threading
import time

from ag_workstation.capture.types import Frame


class LatestFrame:
    """Holds only the newest frame of one camera.

    The producer never waits: a new frame replaces the old one, and a frame
    replaced before anyone took it counts as dropped. The engine therefore
    never processes stale, queued frames.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._taken_seq = 0
        self._received = 0
        self._dropped = 0

    @property
    def received(self) -> int:
        """Frames put since creation."""
        with self._cond:
            return self._received

    @property
    def dropped(self) -> int:
        """Frames replaced before they were taken."""
        with self._cond:
            return self._dropped

    def put(self, frame: Frame) -> None:
        """Publish `frame`, replacing any untaken one."""
        with self._cond:
            if self._frame is not None and self._frame.seq > self._taken_seq:
                self._dropped += 1
            self._frame = frame
            self._received += 1
            self._cond.notify_all()

    def peek(self) -> Frame | None:
        """The newest frame, taken or not, without marking it taken."""
        with self._cond:
            return self._frame

    def take(self, after_seq: int | None = None, timeout: float = 0.0) -> Frame | None:
        """The newest frame not yet taken (or newer than `after_seq`).

        Waits up to `timeout` seconds for one; returns None if none arrives.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                floor = self._taken_seq if after_seq is None else after_seq
                frame = self._frame
                if frame is not None and frame.seq > floor:
                    self._taken_seq = max(self._taken_seq, frame.seq)
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
