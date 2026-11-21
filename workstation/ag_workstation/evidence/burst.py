"""Burst assembly: pick the frames of one event and hand them to a sink.

`docs/detection-engine.md` section 7 and assumption A5. On a warned scan:
the key frame at scan time plus `pre_frames` before and `post_frames` after
it from the primary camera, and the key frame of each usable secondary. A
`state_episode` of a key-frame state saves one key frame from the primary.

Post frames do not exist yet when the scan happens, so a burst stays pending
until they arrive or until their time is clearly past; then it is delivered
with whatever frames exist and the missing ones listed. Nothing touches disk.

Bursts are the workstation-wide `engine_api.EvidenceBurst` (frame roles,
SHA-256, `expected_frames`, `missing` labels), delivered to an
`engine_api.EvidenceSink` through `submit`; in production that sink is the
consent gate's `GatedEvidenceSink` in front of the `EvidenceStore`.
Offsets are relative to the trigger (scan time or episode start). A
secondary camera's key frame has role `key` and its own slot; its missing
label is `secondary_key:<slot>`.
"""

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ag_shared.config import CaptureConfig

from ag_workstation.engine_api import (
    BurstKind,
    EvidenceBurst,
    EvidenceFrame,
    EvidenceSink,
    FrameRole,
)
from ag_workstation.evidence.ring import EvidenceRings, RingFrame

log = logging.getLogger(__name__)

MAX_PENDING = 32
"""Bound on bursts waiting for post frames; the oldest is delivered early."""

KEY_TOLERANCE_FACTOR = 1.5
"""A key frame may be at most this many sample intervals away from the key time."""

_POST_MIN_S = 0.0005
"""A post frame must be at least this far after the trigger, so its offset in
whole milliseconds is positive (a frame processed late but captured before
the scan is not a post frame)."""


@dataclass(frozen=True, slots=True)
class _Pick:
    """One selected ring frame and its place in the burst."""

    frame: RingFrame
    role: FrameRole


class AsyncSink:
    """Delivers bursts to another sink on its own thread, through a bounded queue.

    Burst assembly runs on camera threads and on the scan path; storing a
    burst (encrypting, writing) must not slow either. A full queue drops the
    burst and counts it rather than block. Errors of the inner sink are
    counted and logged, never raised.
    """

    def __init__(self, inner: EvidenceSink, *, max_queued: int = 64) -> None:
        self._inner = inner
        self._queue: queue.Queue[EvidenceBurst | None] = queue.Queue(maxsize=max_queued)
        self.dropped = 0
        self.errors = 0
        self.delivered = 0
        self._thread = threading.Thread(target=self._run, name="evidence-sink", daemon=True)
        self._thread.start()

    def submit(self, burst: EvidenceBurst) -> None:
        """Queue `burst`; never blocks."""
        try:
            self._queue.put_nowait(burst)
        except queue.Full:
            self.dropped += 1
            log.error("evidence queue full; burst dropped")

    def _run(self) -> None:
        while True:
            burst = self._queue.get()
            if burst is None:
                return
            try:
                self._inner.submit(burst)
                self.delivered += 1
            except Exception as exc:
                self.errors += 1
                log.error("evidence sink error (%s)", type(exc).__name__)
            finally:
                self._queue.task_done()

    def drain(self) -> None:
        """Wait until every queued burst was handed on (tests, shutdown)."""
        self._queue.join()

    def close(self, timeout: float = 5.0) -> None:
        """Finish the queued bursts and stop the thread."""
        self._queue.put(None)
        self._thread.join(timeout)


class MemorySink:
    """An `EvidenceSink` that keeps bursts in a list (tests and replay)."""

    def __init__(self) -> None:
        self.bursts: list[EvidenceBurst] = []

    def submit(self, burst: EvidenceBurst) -> None:
        """Keep `burst`."""
        self.bursts.append(burst)


@dataclass(slots=True)
class _Pending:
    event_id: UUID
    key_ts: float
    triggered_at: datetime | None
    primary: int
    picks: list[_Pick]
    missing: list[str]
    expected: int
    post_needed: int
    post_after_ts: float
    deadline_ts: float
    post: list[_Pick] = field(default_factory=list)


class BurstAssembler:
    """Builds bursts from the rings and delivers them to a sink. Thread-safe."""

    def __init__(
        self,
        rings: EvidenceRings,
        capture: CaptureConfig,
        sink: EvidenceSink,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        mono_clock: Callable[[], float] | None = None,
    ) -> None:
        """`wall_clock`/`mono_clock` date a burst none of whose frames exist
        (so there is no frame to derive the trigger's wall time from)."""
        self._rings = rings
        self._capture = capture
        self._sink = sink
        self._wall = wall_clock or (lambda: datetime.now(UTC))
        self._mono = mono_clock
        self._interval = capture.frame_interval_ms / 1000
        self._lock = threading.Lock()
        self._pending: list[_Pending] = []
        self.delivery_errors = 0
        self.dropped_frames = 0
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
        triggered_at: datetime | None = None,
    ) -> None:
        """Start the burst of a warned scan at `key_ts` (engine seconds).

        `triggered_at` is the scan's UTC time; when omitted it is derived
        from the frames' capture times."""
        cap = self._capture
        picks: list[_Pick] = []
        missing: list[str] = []
        frames = self._rings.snapshot(primary_slot)
        key = self._key_frame(frames, key_ts)
        if key is None:
            missing.append("key")
            anchor_ts = key_ts
        else:
            picks.append(_Pick(key, FrameRole.KEY))
            anchor_ts = key.ts
        before = (
            [f for f in frames if f.ts < anchor_ts - 1e-9][-cap.pre_frames :]
            if cap.pre_frames
            else []
        )
        picks[:0] = [_Pick(f, FrameRole.PRE) for f in before]
        if len(before) < cap.pre_frames:
            missing.extend(["pre"] * (cap.pre_frames - len(before)))
        for slot in secondary_slots:
            if slot == primary_slot:
                continue
            sec = self._key_frame(self._rings.snapshot(slot), key_ts)
            if sec is None:
                missing.append(f"secondary_key:{slot}")
            else:
                picks.append(_Pick(sec, FrameRole.KEY))
        post_after = max(anchor_ts, key_ts + _POST_MIN_S)
        already = [f for f in frames if f.ts > post_after + 1e-9][: cap.post_frames]
        expected = cap.pre_frames + 1 + cap.post_frames
        expected += len({s for s in secondary_slots if s != primary_slot})
        pending = _Pending(
            event_id=event_id,
            key_ts=key_ts,
            triggered_at=triggered_at,
            primary=primary_slot,
            picks=picks,
            missing=missing,
            expected=expected,
            post_needed=cap.post_frames,
            post_after_ts=post_after,
            deadline_ts=anchor_ts + (cap.post_frames + 1) * self._interval,
            post=[_Pick(f, FrameRole.POST) for f in already],
        )
        self._submit(pending)

    def request_keyframe(
        self,
        event_id: UUID,
        key_ts: float,
        *,
        primary_slot: int,
        triggered_at: datetime | None = None,
    ) -> None:
        """One key frame from the primary (Sleeping/Absent episodes)."""
        key = self._key_frame(self._rings.snapshot(primary_slot), key_ts)
        picks = [] if key is None else [_Pick(key, FrameRole.KEY)]
        missing = ["key"] if key is None else []
        try:
            burst = self._build(
                event_id, BurstKind.EPISODE_KEYFRAME, key_ts, triggered_at, picks, missing, 1
            )
        except Exception as exc:  # a malformed burst loses only its image
            self.delivery_errors += 1
            log.error("burst not built (%s)", type(exc).__name__)
            return
        self._hand_over(burst)

    def _trigger_time(self, key_ts: float, picks: Sequence[_Pick]) -> datetime:
        if picks:
            f = picks[0].frame
            return f.captured_at - timedelta(seconds=f.ts - key_ts)
        wall = self._wall()
        if self._mono is None:
            return wall
        return wall - timedelta(seconds=self._mono() - key_ts)

    def _build(
        self,
        event_id: UUID,
        kind: BurstKind,
        key_ts: float,
        triggered_at: datetime | None,
        picks: Sequence[_Pick],
        missing: list[str],
        expected: int,
    ) -> EvidenceBurst:
        """Turn picks into an `EvidenceBurst`; a frame that fails validation
        (not a JPEG, offset on the wrong side of the trigger) is left out
        and listed as missing, never raised."""
        at = triggered_at if triggered_at is not None else self._trigger_time(key_ts, picks)
        frames: list[EvidenceFrame] = []
        seen: set[tuple[int, int]] = set()
        labels = list(missing)
        for pick in picks:
            f = pick.frame
            ident = (f.camera_slot, f.seq)
            try:
                if ident in seen:
                    raise ValueError("duplicate frame")
                frames.append(
                    EvidenceFrame(
                        camera_slot=f.camera_slot,
                        captured_at=f.captured_at,
                        offset_ms=round((f.ts - key_ts) * 1000),
                        seq=f.seq,
                        role=pick.role,
                        jpeg=f.jpeg,
                    )
                )
                seen.add(ident)
            except ValueError as exc:
                self.dropped_frames += 1
                labels.append(pick.role.value)
                log.warning("burst frame left out (%s)", type(exc).__name__)
        return EvidenceBurst(
            event_id=event_id,
            kind=kind,
            triggered_at=at,
            trigger_ts=key_ts,
            frames=tuple(frames),
            expected_frames=expected,
            missing=tuple(labels),
        )

    def _hand_over(self, burst: EvidenceBurst) -> None:
        """Give one burst to the sink; a failure loses only that burst."""
        try:
            self._sink.submit(burst)
        except Exception as exc:
            self.delivery_errors += 1
            log.error("evidence sink error (%s)", type(exc).__name__)

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
                    p.post.append(_Pick(frame, FrameRole.POST))
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
        burst = None
        try:
            burst = self._build(
                p.event_id,
                BurstKind.SCAN_WARNING,
                p.key_ts,
                p.triggered_at,
                [*p.picks, *post],
                missing,
                p.expected,
            )
        except Exception as exc:  # a malformed burst loses only its images
            self.delivery_errors += 1
            log.error("burst not built (%s)", type(exc).__name__)
        if burst is not None:
            self._hand_over(burst)
