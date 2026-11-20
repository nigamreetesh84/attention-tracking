"""Evidence ring buffer and burst capture (in memory only)."""

from pathlib import Path
from uuid import UUID

import numpy as np
import pytest
from ag_shared.config import CaptureConfig
from ag_workstation.capture.jpeg import encode_jpeg
from ag_workstation.capture.types import CaptureError, Frame, Image
from ag_workstation.engine.fakes import T0
from ag_workstation.evidence import (
    AsyncSink,
    BurstAssembler,
    CameraRing,
    EvidenceBurst,
    EvidenceRings,
    MemorySink,
)
from ag_workstation.evidence import burst as burst_module

EVENT = UUID(int=7)


def fake_jpeg(image: Image, quality: int) -> bytes:
    return bytes([int(image[0, 0, 0]), quality])


def frame(slot: int, seq: int, ts: float) -> Frame:
    img = np.full((4, 4, 3), seq % 256, dtype=np.uint8)
    return Frame(camera_slot=slot, seq=seq, ts=ts, captured_at=T0, image=img)


def feed(
    rings: EvidenceRings, slots: tuple[int, ...], start: float, end: float, fps: int = 10
) -> None:
    i = round(start * fps)
    while i / fps < end - 1e-9:
        for slot in slots:
            rings.offer(frame(slot, i + 1, i / fps))
        i += 1


CAP = CaptureConfig()  # 2 pre, 2 post, 500 ms, ring 3 s -> 6 frames


# --- ring ---


def test_ring_samples_at_the_frame_interval_and_is_bounded() -> None:
    ring = CameraRing(1, capacity=6, interval_s=0.5, quality=80, encoder=fake_jpeg)
    stored = [ring.offer(frame(1, i + 1, i / 10)) for i in range(100)]
    kept = [s for s in stored if s is not None]
    assert len(kept) == 20  # 10 s at 2 fps
    held = ring.frames()
    assert len(held) == ring.capacity == 6
    assert [f.ts for f in held] == [7.0, 7.5, 8.0, 8.5, 9.0, 9.5]
    assert held[-1].jpeg == bytes([96, 80])  # seq 96, quality 80


def test_ring_restarts_when_time_goes_back_and_rejects_other_cameras() -> None:
    ring = CameraRing(1, capacity=3, interval_s=0.5, quality=80, encoder=fake_jpeg)
    ring.offer(frame(1, 1, 5.0))
    ring.offer(frame(1, 2, 1.0))
    assert [f.ts for f in ring.frames()] == [1.0]
    with pytest.raises(ValueError, match="another camera"):
        ring.offer(frame(2, 1, 2.0))
    ring.clear()
    assert ring.frames() == []
    with pytest.raises(ValueError, match="capacity"):
        CameraRing(1, capacity=0, interval_s=0.5, quality=80, encoder=fake_jpeg)


def test_station_rings_memory_is_bounded() -> None:
    rings = EvidenceRings((1, 2), CAP, fake_jpeg)
    feed(rings, (1, 2), 0.0, 600.0)
    assert len(rings.snapshot(1)) == 6
    assert rings.memory_bytes() == 2 * 6 * 2
    rings.offer(frame(3, 1, 0.0))  # unknown camera: ignored
    assert rings.snapshot(3) == []
    assert rings.slots == (1, 2)
    rings.clear()
    assert rings.memory_bytes() == 0


# --- bursts ---


def test_scan_burst_selects_key_pre_post_and_secondary_by_timestamp() -> None:
    rings = EvidenceRings((1, 2), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1, 2), 0.0, 10.05)  # ring frames at ... 9.0, 9.5, 10.0
    assembler.request_scan_burst(EVENT, 10.0, primary_slot=1, secondary_slots=(2,))
    assert sink.bursts == []  # waiting for post frames
    assert assembler.pending == 1
    feed(rings, (1, 2), 10.1, 11.05)
    assert assembler.pending == 0
    (burst,) = sink.bursts
    roles = [(i.role, i.frame.camera_slot, i.offset_ms) for i in burst.images]
    assert roles == [
        ("pre", 1, -1000),
        ("pre", 1, -500),
        ("key", 1, 0),
        ("secondary_key", 2, 0),
        ("post", 1, 500),
        ("post", 1, 1000),
    ]
    assert burst.missing == ()
    assert burst.event_id == EVENT


def test_key_frame_is_the_nearest_within_tolerance() -> None:
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1,), 0.0, 10.05)
    assembler.request_scan_burst(EVENT, 10.2, primary_slot=1)
    feed(rings, (1,), 10.1, 12.0)
    key = next(i for i in sink.bursts[0].images if i.role == "key")
    assert key.frame.ts == 10.0
    assert key.offset_ms == -200


def test_missing_frames_are_listed() -> None:
    rings = EvidenceRings((1, 2), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1,), 0.0, 0.55)  # only two frames: 0.0 and 0.5; camera 2 has none
    assembler.request_scan_burst(EVENT, 0.5, primary_slot=1, secondary_slots=(2,))
    # The camera stops: post frames never come; poll after the deadline.
    assembler.poll(1.0)
    assert sink.bursts == []
    assembler.poll(2.1)
    (burst,) = sink.bursts
    assert [i.role for i in burst.images] == ["pre", "key"]
    assert burst.missing == ("pre", "secondary_key:2", "post", "post")


def test_no_key_frame_at_all() -> None:
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    assembler.request_scan_burst(EVENT, 5.0, primary_slot=1)
    assembler.flush()
    (burst,) = sink.bursts
    assert burst.images == ()
    assert burst.missing == ("key", "pre", "pre", "post", "post")


def test_post_frames_already_in_the_ring_complete_at_once() -> None:
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1,), 0.0, 6.0)
    assembler.request_scan_burst(EVENT, 4.0, primary_slot=1)  # scan handled late
    (burst,) = sink.bursts
    assert [i.offset_ms for i in burst.images] == [-1000, -500, 0, 500, 1000]


def test_late_frame_past_the_deadline_delivers() -> None:
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1,), 0.0, 2.05)
    assembler.request_scan_burst(EVENT, 2.0, primary_slot=1)
    rings.offer(frame(1, 99, 5.0))  # one frame, long after the deadline
    (burst,) = sink.bursts
    assert [i.role for i in burst.images].count("post") == 1
    assert burst.missing == ("post",)


def test_pending_bursts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burst_module, "MAX_PENDING", 2)
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1,), 0.0, 1.05)
    for i in range(3):
        assembler.request_scan_burst(UUID(int=i), 1.0, primary_slot=1)
    assert assembler.pending == 2
    assert [b.event_id for b in sink.bursts] == [UUID(int=0)]


def test_episode_keyframe() -> None:
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1,), 0.0, 3.05)
    assembler.request_keyframe(EVENT, 3.0, primary_slot=1)
    assembler.request_keyframe(UUID(int=8), 60.0, primary_slot=1)
    first, second = sink.bursts
    assert [(i.role, i.offset_ms) for i in first.images] == [("key", 0)]
    assert second.images == () and second.missing == ("key",)


def test_no_pre_or_post_frames_configured() -> None:
    cap = CaptureConfig(pre_frames=0, post_frames=0)
    rings = EvidenceRings((1,), cap, fake_jpeg)
    sink = MemorySink()
    assembler = BurstAssembler(rings, cap, sink)
    feed(rings, (1,), 0.0, 1.05)
    assembler.request_scan_burst(EVENT, 1.0, primary_slot=1)
    (burst,) = sink.bursts
    assert [i.role for i in burst.images] == ["key"]


def test_nothing_touches_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rings = EvidenceRings((1, 2), CAP, fake_jpeg)
    assembler = BurstAssembler(rings, CAP, MemorySink())
    feed(rings, (1, 2), 0.0, 20.0)
    assembler.request_scan_burst(EVENT, 19.0, primary_slot=1, secondary_slots=(2,))
    assembler.flush()
    assert list(tmp_path.iterdir()) == []


def test_real_jpeg_encoder() -> None:
    data = encode_jpeg(np.zeros((8, 8, 3), dtype=np.uint8), 80)
    assert data[:2] == b"\xff\xd8"


def test_jpeg_encoder_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import cv2

    monkeypatch.setattr(cv2, "imencode", lambda *a: (False, None))
    with pytest.raises(CaptureError, match="JPEG"):
        encode_jpeg(np.zeros((8, 8, 3), dtype=np.uint8), 80)


class FailingSink:
    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.bursts: list[object] = []

    def deliver(self, burst: object) -> None:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise OSError("disk full")
        self.bursts.append(burst)


def test_a_failing_sink_loses_only_that_burst() -> None:
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    sink = FailingSink(fail_first=1)
    assembler = BurstAssembler(rings, CAP, sink)
    feed(rings, (1,), 0.0, 2.05)
    assembler.request_scan_burst(UUID(int=1), 2.0, primary_slot=1)
    assembler.request_scan_burst(UUID(int=2), 2.0, primary_slot=1)
    feed(rings, (1,), 2.1, 4.0)  # both complete on the same frame
    assert assembler.delivery_errors == 1
    assert len(sink.bursts) == 1
    assembler.request_keyframe(UUID(int=3), 3.0, primary_slot=1)
    assert len(sink.bursts) == 2


def test_a_failing_ring_listener_does_not_stop_the_others() -> None:
    rings = EvidenceRings((1,), CAP, fake_jpeg)
    seen: list[int] = []

    def broken(frame: object) -> None:
        raise RuntimeError("bug")

    rings.subscribe(broken)
    rings.subscribe(lambda f: seen.append(f.seq))
    feed(rings, (1,), 0.0, 1.05)
    assert seen == [1, 6, 11]
    assert rings.listener_errors == 3


def test_async_sink_delivers_off_thread_and_never_blocks() -> None:
    import threading

    gate = threading.Event()
    threads: list[str] = []

    class SlowSink:
        def __init__(self) -> None:
            self.bursts: list[object] = []

        def deliver(self, burst: object) -> None:
            threads.append(threading.current_thread().name)
            gate.wait(5)
            if len(self.bursts) == 0 and len(threads) == 1:
                self.bursts.append(burst)
                return
            raise OSError("write failed")

    inner = SlowSink()
    sink = AsyncSink(inner, max_queued=2)
    burst = EvidenceBurst(EVENT, 0.0, ())
    for _ in range(5):
        sink.deliver(burst)  # 1 in progress, 2 queued, the rest dropped
    assert sink.dropped >= 2
    gate.set()
    sink.drain()
    sink.close()
    assert threads[0] == "evidence-sink"
    assert sink.delivered == 1
    assert sink.errors == len(threads) - 1
