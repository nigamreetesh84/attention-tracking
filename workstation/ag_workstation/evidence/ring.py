"""In-memory evidence ring: recent JPEG frames per camera.

`docs/detection-engine.md` section 7: each camera keeps a short ring of
recent JPEG frames in memory (default 3 s at 2 fps). Nothing is written to
disk here; frames leave the ring only inside an `EvidenceBurst` handed to an
`EvidenceSink`, and the consent gate (task 2.5) decides whether that sink may
store them.
"""

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ag_shared.config import CaptureConfig

from ag_workstation.capture.types import Frame, Image

JpegEncoder = Callable[[Image, int], bytes]
"""(BGR image, quality 1-100) -> JPEG bytes. The real one lives in the capture
layer (`capture.jpeg`), the only place that may import OpenCV."""

_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class RingFrame:
    """One JPEG frame held in memory."""

    camera_slot: int
    seq: int
    ts: float
    captured_at: datetime
    jpeg: bytes = field(repr=False)


class CameraRing:
    """The ring of one camera: samples every `interval_s`, keeps `capacity`.

    Memory is bounded by `capacity` frames. Sampling uses frame timestamps,
    so the ring behaves the same live and in replay.
    """

    def __init__(
        self, slot: int, *, capacity: int, interval_s: float, quality: int, encoder: JpegEncoder
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.slot = slot
        self._interval = interval_s
        self._quality = quality
        self._encode = encoder
        self._frames: deque[RingFrame] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        """Most frames held."""
        maxlen = self._frames.maxlen
        assert maxlen is not None  # noqa: S101 - always bounded
        return maxlen

    def offer(self, frame: Frame) -> RingFrame | None:
        """Keep `frame` if a sample is due; returns the stored frame or None."""
        if frame.camera_slot != self.slot:
            raise ValueError("frame belongs to another camera")
        if self._frames:
            last = self._frames[-1]
            if frame.ts < last.ts:
                self._frames.clear()  # replay restarted
            elif frame.ts - last.ts < self._interval - _EPS:
                return None
        stored = RingFrame(
            camera_slot=frame.camera_slot,
            seq=frame.seq,
            ts=frame.ts,
            captured_at=frame.captured_at,
            jpeg=self._encode(frame.image, self._quality),
        )
        self._frames.append(stored)
        return stored

    def frames(self) -> list[RingFrame]:
        """A copy of the held frames, oldest first."""
        return list(self._frames)

    def clear(self) -> None:
        """Drop every frame (end of session, live view privacy, shutdown)."""
        self._frames.clear()


class EvidenceRings:
    """The rings of all cameras of a station. Thread-safe.

    `offer` is the engine's frame listener; `snapshot` and `subscribe` are
    used by the burst assembler.
    """

    def __init__(
        self, slots: tuple[int, ...], capture: CaptureConfig, encoder: JpegEncoder
    ) -> None:
        self._lock = threading.Lock()
        self._rings = {
            slot: CameraRing(
                slot,
                capacity=capture.ring_frames,
                interval_s=capture.frame_interval_ms / 1000,
                quality=capture.jpeg_quality,
                encoder=encoder,
            )
            for slot in slots
        }
        self._listeners: list[Callable[[RingFrame], None]] = []

    @property
    def slots(self) -> tuple[int, ...]:
        """Cameras with a ring."""
        return tuple(self._rings)

    def subscribe(self, listener: Callable[[RingFrame], None]) -> None:
        """Call `listener` with every frame stored from now on."""
        with self._lock:
            self._listeners.append(listener)

    def offer(self, frame: Frame) -> None:
        """Sample `frame` into its camera's ring (frames of unknown cameras are ignored)."""
        with self._lock:
            ring = self._rings.get(frame.camera_slot)
            if ring is None:
                return
            stored = ring.offer(frame)
            listeners = list(self._listeners) if stored is not None else []
        for listener in listeners:
            assert stored is not None  # noqa: S101 - listeners only when stored
            listener(stored)

    def snapshot(self, slot: int) -> list[RingFrame]:
        """The frames of camera `slot`, oldest first (empty if unknown)."""
        with self._lock:
            ring = self._rings.get(slot)
            return ring.frames() if ring is not None else []

    def clear(self) -> None:
        """Drop every frame of every camera."""
        with self._lock:
            for ring in self._rings.values():
                ring.clear()

    def memory_bytes(self) -> int:
        """JPEG bytes currently held (for health)."""
        with self._lock:
            return sum(len(f.jpeg) for r in self._rings.values() for f in r.frames())
