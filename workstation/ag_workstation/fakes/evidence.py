"""Deterministic evidence bursts and a recording sink for tests."""

import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from ag_shared.config import CaptureConfig

from ag_workstation.engine_api import (
    JPEG_SOI,
    BurstKind,
    EvidenceBurst,
    EvidenceFrame,
    FrameRole,
)

_JPEG_EOI = b"\xff\xd9"


def fake_jpeg(tag: str) -> bytes:
    """Bytes that look like a JPEG (SOI ... EOI) and differ per `tag`."""
    return JPEG_SOI + b"\xff\xe0" + tag.encode() + _JPEG_EOI


class BurstBuilder:
    """Builds bursts shaped like section 7 of detection-engine.md.

    Scan warning: `pre_frames` before, the key frame and `post_frames` after
    from the primary camera, plus the key frame of each secondary. Episode key
    frame: the primary's key frame only. Frames are spaced by
    `frame_interval_ms`; seq numbers and bytes are deterministic.
    """

    def __init__(
        self,
        capture: CaptureConfig | None = None,
        *,
        primary_slot: int = 1,
        secondary_slots: tuple[int, ...] = (),
        jpeg: Callable[[str], bytes] = fake_jpeg,
    ) -> None:
        self._capture = capture or CaptureConfig()
        self._primary = primary_slot
        self._secondaries = secondary_slots
        self._jpeg = jpeg

    def layout(self, kind: BurstKind) -> tuple[tuple[int, FrameRole, int], ...]:
        """(camera slot, role, offset_ms) of every expected frame."""
        step = self._capture.frame_interval_ms
        if kind is BurstKind.EPISODE_KEYFRAME:
            return ((self._primary, FrameRole.KEY, 0),)
        pre = [
            (self._primary, FrameRole.PRE, -k * step)
            for k in range(self._capture.pre_frames, 0, -1)
        ]
        post = [
            (self._primary, FrameRole.POST, k * step)
            for k in range(1, self._capture.post_frames + 1)
        ]
        secondaries = [(s, FrameRole.KEY, 0) for s in self._secondaries]
        return (*pre, (self._primary, FrameRole.KEY, 0), *post, *secondaries)

    def build(
        self,
        event_id: UUID,
        kind: BurstKind,
        triggered_at: datetime,
        trigger_ts: float = 0.0,
        *,
        missing: Collection[tuple[int, int]] = (),
    ) -> EvidenceBurst:
        """A burst; `missing` lists (camera slot, offset_ms) frames to leave out."""
        frames = []
        layout = self.layout(kind)
        for index, (slot, role, offset) in enumerate(layout):
            if (slot, offset) in missing:
                continue
            frames.append(
                EvidenceFrame(
                    camera_slot=slot,
                    captured_at=triggered_at + timedelta(milliseconds=offset),
                    offset_ms=offset,
                    seq=1000 + index,
                    role=role,
                    jpeg=self._jpeg(f"{event_id}/{slot}/{offset}"),
                )
            )
        return EvidenceBurst(
            event_id=event_id,
            kind=kind,
            triggered_at=triggered_at,
            trigger_ts=trigger_ts,
            frames=tuple(frames),
            expected_frames=len(layout),
        )


@dataclass
class RecordingEvidenceSink:
    """An `EvidenceSink` that keeps what it receives; can be told to fail."""

    bursts: list[EvidenceBurst] = field(default_factory=list)
    fail_with: BaseException | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def submit(self, burst: EvidenceBurst) -> None:
        """Record `burst`, or raise `fail_with` if set."""
        if self.fail_with is not None:
            raise self.fail_with
        with self._lock:
            self.bursts.append(burst)
