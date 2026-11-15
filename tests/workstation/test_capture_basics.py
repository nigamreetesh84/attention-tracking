"""Capture building blocks: clock, backoff, latest-frame slot, fake source."""

import threading
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from ag_workstation.capture import (
    Backoff,
    CaptureError,
    EndOfStream,
    FakeCamera,
    FakeSource,
    Frame,
    LatestFrame,
)
from ag_workstation.clock import FakeClock, SystemClock


def _frame(seq: int, ts: float = 0.0) -> Frame:
    return Frame(
        camera_slot=1,
        seq=seq,
        ts=ts,
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        image=np.zeros((2, 2, 3), dtype=np.uint8),
    )


# --- clock -------------------------------------------------------------------


def test_fake_clock_advances_both_timelines() -> None:
    clock = FakeClock(start_monotonic=10.0, start_utc=datetime(2026, 11, 16, tzinfo=UTC))
    clock.advance(2.5)
    assert clock.monotonic() == 12.5
    assert clock.now() == datetime(2026, 11, 16, tzinfo=UTC) + timedelta(seconds=2.5)


def test_fake_clock_rejects_going_backwards_and_naive_start() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="UTC"):
        FakeClock(start_utc=datetime(2026, 1, 1))


def test_system_clock_is_utc_and_monotonic() -> None:
    clock = SystemClock()
    a = clock.monotonic()
    assert clock.monotonic() >= a
    assert clock.now().utcoffset() == timedelta(0)


# --- backoff -----------------------------------------------------------------


def test_backoff_grows_to_the_cap_and_resets() -> None:
    backoff = Backoff(initial_s=0.5, factor=2.0, max_s=3.0)
    assert [backoff.next_delay() for _ in range(5)] == [0.5, 1.0, 2.0, 3.0, 3.0]
    assert backoff.attempts == 5
    backoff.reset()
    assert backoff.attempts == 0
    assert backoff.next_delay() == 0.5


@pytest.mark.parametrize(
    ("initial", "factor", "cap"),
    [(0.0, 2.0, 1.0), (1.0, 0.5, 2.0), (2.0, 2.0, 1.0)],
)
def test_backoff_rejects_bad_parameters(initial: float, factor: float, cap: float) -> None:
    with pytest.raises(ValueError):
        Backoff(initial_s=initial, factor=factor, max_s=cap)


# --- latest frame wins -------------------------------------------------------


def test_latest_frame_empty() -> None:
    slot = LatestFrame()
    assert slot.peek() is None
    assert slot.take() is None
    assert slot.dropped == 0


def test_latest_frame_wins_and_counts_drops() -> None:
    slot = LatestFrame()
    for seq in range(1, 6):
        slot.put(_frame(seq))
    taken = slot.take()
    assert taken is not None and taken.seq == 5
    # Frames 1-4 were replaced before anyone took them.
    assert slot.dropped == 4
    assert slot.received == 5
    # Nothing newer: take returns None, peek still shows the last frame.
    assert slot.take() is None
    peeked = slot.peek()
    assert peeked is not None and peeked.seq == 5
    slot.put(_frame(6))
    again = slot.take()
    assert again is not None and again.seq == 6
    assert slot.dropped == 4


def test_latest_frame_take_waits_for_a_new_frame() -> None:
    slot = LatestFrame()
    slot.put(_frame(1))
    assert slot.take() is not None

    def later() -> None:
        slot.put(_frame(2))

    timer = threading.Timer(0.02, later)
    timer.start()
    try:
        got = slot.take(timeout=2.0)
    finally:
        timer.cancel()
    assert got is not None and got.seq == 2


def test_latest_frame_take_times_out() -> None:
    slot = LatestFrame()
    assert slot.take(timeout=0.01) is None


def test_latest_frame_take_after_seq() -> None:
    slot = LatestFrame()
    slot.put(_frame(3))
    assert slot.take(after_seq=3) is None
    got = slot.take(after_seq=2)
    assert got is not None and got.seq == 3


# --- fake source -------------------------------------------------------------


def test_fake_source_produces_numbered_frames() -> None:
    camera = FakeCamera(width=4, height=3)
    source = camera.make_source()
    source.open()
    first = source.read()
    second = source.read()
    assert first is not None and second is not None
    assert first.shape == (3, 4, 3)
    assert first.dtype == np.uint8
    assert int(first[0, 0, 0]) == 1
    assert int(second[0, 0, 0]) == 2
    source.close()
    assert camera.opens == 1
    assert camera.closes == 1
    assert camera.reads == 2


def test_fake_source_failure_modes() -> None:
    camera = FakeCamera()
    camera.available = False
    with pytest.raises(CaptureError):
        camera.make_source().open()
    camera.available = True
    source = camera.make_source()
    source.open()
    camera.failing = True
    with pytest.raises(CaptureError):
        source.read()
    camera.failing = False
    camera.return_none = True
    assert source.read() is None


def test_fake_source_must_be_open_to_read() -> None:
    with pytest.raises(CaptureError):
        FakeSource().read()


def test_fake_source_finite_stream_ends() -> None:
    camera = FakeCamera(frames=2)
    source = camera.make_source()
    source.open()
    assert source.read() is not None
    assert source.read() is not None
    with pytest.raises(EndOfStream):
        source.read()


def test_fake_source_hang_blocks_until_released() -> None:
    camera = FakeCamera()
    source = camera.make_source()
    source.open()
    camera.hang()
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(source.read()), daemon=True)
    thread.start()
    thread.join(0.05)
    assert thread.is_alive()
    camera.release()
    thread.join(2.0)
    assert not thread.is_alive()
    assert result and result[0] is not None
