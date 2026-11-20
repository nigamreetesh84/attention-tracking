"""Burst assembly: pick the frames of one event and hand them to a sink.

`docs/detection-engine.md` section 7 and assumption A5. On a warned scan:
the key frame at scan time plus `pre_frames` before and `post_frames` after
it from the primary camera, and the key frame of each usable secondary. A
`state_episode` of a key-frame state saves one key frame from the primary.

Post frames do not exist yet when the scan happens, so a burst stays pending
until they arrive or until their time is clearly past; then it is delivered
with whatever frames exist and the missing ones listed. Nothing touches disk.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ag_shared.config import CaptureConfig

from ag_workstation.evidence.ring import EvidenceRings, RingFrame

MAX_PENDING = 32
"""Bound on bursts waiting for post frames; the oldest is delivered early."""

KEY_TOLERANCE_FACTOR = 1.5
"""A key frame may be at most this many sample intervals away from the key time."""


@dataclass(frozen=True, slots=True)
class BurstImage:
    """One selected frame and where it sits in the burst."""

    frame: RingFrame
    offset_ms: int
    """Relative to the key time; negative for pre-frames (`evidence_image.offset_ms`)."""
    role: str
    """`key`, `pre`, `post` or `secondary_key`."""


@dataclass(frozen=True, slots=True)
class EvidenceBurst:
    """The images of one attention event, ready for the consent gate and store."""

    event_id: UUID
    key_ts: float
    images: tuple[BurstImage, ...]
    missing: tuple[str, ...] = ()
    """Roles that could not be filled, e.g. `post` or `secondary_key:2`."""


class EvidenceSink(Protocol):
    """Receives finished bursts. Task 2.5 wires the consent-gated store here."""

    def deliver(self, burst: EvidenceBurst) -> None:
        """Take ownership of `burst`."""
        ...


class MemorySink:
    """An `EvidenceSink` that keeps bursts in a list (tests and replay)."""

    def __init__(self) -> None:
        self.bursts: list[EvidenceBurst] = []

    def deliver(self, burst: EvidenceBurst) -> None:
        """Keep `burst`."""
        self.bursts.append(burst)


@dataclass(slots=True)
class _Pending:
    event_id: UUID
    key_ts: float
    primary: int
    images: list[BurstImage]
    missing: list[str]
    post_needed: int
    post_after_ts: float
    deadline_ts: float
    post: list[BurstImage] = field(default_factory=list)


def _offset_ms(frame: RingFrame, key_ts: float) -> int:
    return round((frame.ts - key_ts) * 1000)


class BurstAssembler:
    """Builds bursts from the rings and delivers them to a sink. Thread-safe."""

    def __init__(self, rings: EvidenceRings, capture: CaptureConfig, sink: EvidenceSink) -> None:
        self._rings = rings
        self._capture = capture
        self._sink = sink
        self._interval = capture.frame_interval_ms / 1000
        self._lock = threading.Lock()
        self._pending: list[_Pending] = []
        rings.subscribe(self._on_frame)

    @property
    def pending(self) -> int:
        """Bursts still waiting for post frames."""
        with self._lock:
            return len(self._pending)

    def _key_frame(self, frames: Sequence[RingFrame], key_ts: float) -> RingFrame | None:
        """The frame closest to `key_ts` within tolerance, preferring earlier ones."""
        tolerance = self._interval * KEY_TOLERANCE_FACTOR
        best: RingFrame | None = None
        for f in frames:
            gap = abs(f.ts - key_ts)
            if gap > tolerance:
                continue
            if best is None or gap < abs(best.ts - key_ts) - 1e-9:
                best = f
        return best

    def request_scan_burst(
        self,
        event_id: UUID,
        key_ts: float,
        *,
        primary_slot: int,
        secondary_slots: Sequence[int] = (),
    ) -> None:
        """Start the burst of a warned scan at `key_ts` (engine seconds)."""
        cap = self._capture
        images: list[BurstImage] = []
        missing: list[str] = []
        frames = self._rings.snapshot(primary_slot)
        key = self._key_frame(frames, key_ts)
        if key is None:
            missing.append("key")
            anchor_ts = key_ts
        else:
            images.append(BurstImage(key, _offset_ms(key, key_ts), "key"))
            anchor_ts = key.ts
        before = (
            [f for f in frames if f.ts < anchor_ts - 1e-9][-cap.pre_frames :]
            if cap.pre_frames
            else []
        )
        images[:0] = [BurstImage(f, _offset_ms(f, key_ts), "pre") for f in before]
        if len(before) < cap.pre_frames:
            missing.extend(["pre"] * (cap.pre_frames - len(before)))
        for slot in secondary_slots:
            sec = self._key_frame(self._rings.snapshot(slot), key_ts)
            if sec is None:
                missing.append(f"secondary_key:{slot}")
            else:
                images.append(BurstImage(sec, _offset_ms(sec, key_ts), "secondary_key"))
        already = [f for f in frames if f.ts > anchor_ts + 1e-9][: cap.post_frames]
        pending = _Pending(
            event_id=event_id,
            key_ts=key_ts,
            primary=primary_slot,
            images=images,
            missing=missing,
            post_needed=cap.post_frames,
            post_after_ts=anchor_ts,
            deadline_ts=anchor_ts + (cap.post_frames + 1) * self._interval,
            post=[BurstImage(f, _offset_ms(f, key_ts), "post") for f in already],
        )
        self._submit(pending)

    def request_keyframe(self, event_id: UUID, key_ts: float, *, primary_slot: int) -> None:
        """One key frame from the primary (Sleeping/Absent episodes)."""
        key = self._key_frame(self._rings.snapshot(primary_slot), key_ts)
        images = () if key is None else (BurstImage(key, _offset_ms(key, key_ts), "key"),)
        missing = ("key",) if key is None else ()
        self._sink.deliver(EvidenceBurst(event_id, key_ts, images, missing))

    def _submit(self, pending: _Pending) -> None:
        ready: list[_Pending] = []
        with self._lock:
            if len(pending.post) >= pending.post_needed:
                ready.append(pending)
            else:
                self._pending.append(pending)
                while len(self._pending) > MAX_PENDING:
                    ready.append(self._pending.pop(0))
        for p in ready:
            self._deliver(p)

    def _on_frame(self, frame: RingFrame) -> None:
        ready: list[_Pending] = []
        with self._lock:
            for p in list(self._pending):
                if frame.camera_slot == p.primary and frame.ts > p.post_after_ts + 1e-9:
                    p.post.append(BurstImage(frame, _offset_ms(frame, p.key_ts), "post"))
                if len(p.post) >= p.post_needed or frame.ts > p.deadline_ts:
                    self._pending.remove(p)
                    ready.append(p)
        for p in ready:
            self._deliver(p)

    def poll(self, now_ts: float) -> None:
        """Deliver bursts whose post frames are overdue (a camera stopped)."""
        ready: list[_Pending] = []
        with self._lock:
            for p in list(self._pending):
                if now_ts > p.deadline_ts:
                    self._pending.remove(p)
                    ready.append(p)
        for p in ready:
            self._deliver(p)

    def flush(self) -> None:
        """Deliver every pending burst now (shutdown, session end)."""
        with self._lock:
            ready, self._pending = self._pending, []
        for p in ready:
            self._deliver(p)

    def _deliver(self, p: _Pending) -> None:
        post = p.post[: p.post_needed]
        missing = list(p.missing)
        if len(post) < p.post_needed:
            missing.extend(["post"] * (p.post_needed - len(post)))
        self._sink.deliver(
            EvidenceBurst(
                event_id=p.event_id,
                key_ts=p.key_ts,
                images=tuple(p.images) + tuple(post),
                missing=tuple(missing),
            )
        )
