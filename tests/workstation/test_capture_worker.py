"""CameraWorker: reconnect with backoff, status reporting, hang recovery.

Most tests drive `step()` directly on a fake clock, so no real sleeping and no
threads. The threaded tests use tiny backoffs and bounded waits.
"""

import logging
import threading
import time
from collections.abc import Callable

import pytest
from ag_workstation.capture import (
    Backoff,
    CameraSource,
    CameraStatus,
    CameraWorker,
    CaptureError,
    FakeCamera,
)
from ag_workstation.clock import FakeClock, SystemClock

URI_WITH_CREDENTIALS = "rtsp://admin:hunter2@10.0.0.9/stream1"


def _worker(
    camera: FakeCamera,
    clock: FakeClock,
    *,
    backoff: Backoff | None = None,
    stale_after_s: float = 5.0,
) -> CameraWorker:
    return CameraWorker(
        slot=1,
        source_factory=camera.make_source,
        clock=clock,
        backoff=backoff or Backoff(initial_s=1.0, factor=2.0, max_s=8.0),
        stale_after_s=stale_after_s,
    )


def test_starts_in_starting_state() -> None:
    worker = _worker(FakeCamera(), FakeClock())
    health = worker.health()
    assert health.slot == 1
    assert health.status is CameraStatus.STARTING
    assert health.fps == 0.0
    assert health.last_frame_ts is None


def test_frames_get_timestamps_from_the_injected_clock() -> None:
    clock = FakeClock(start_monotonic=100.0)
    worker = _worker(FakeCamera(), clock)
    assert worker.step() == 0.0
    clock.advance(0.1)
    assert worker.step() == 0.0
    frame = worker.frames.take()
    assert frame is not None
    assert frame.camera_slot == 1
    assert frame.seq == 2
    assert frame.ts == pytest.approx(100.1)
    assert frame.captured_at == clock.now()
    health = worker.health()
    assert health.status is CameraStatus.ONLINE
    assert health.frames_captured == 2
    assert health.last_frame_ts == pytest.approx(100.1)
    assert health.fps == pytest.approx(10.0)


def test_open_failure_goes_offline_and_retries_with_backoff() -> None:
    clock = FakeClock()
    camera = FakeCamera()
    camera.available = False
    worker = _worker(camera, clock)

    assert worker.step() == pytest.approx(1.0)
    health = worker.health()
    assert health.status is CameraStatus.OFFLINE
    assert health.last_error == "CaptureError"
    assert health.failures == 1
    assert health.offline_since == 0.0

    # Before the backoff expires nothing is attempted.
    clock.advance(0.5)
    assert worker.step() == pytest.approx(0.5)
    assert camera.opens == 0

    clock.advance(0.5)
    assert worker.step() == pytest.approx(2.0)
    clock.advance(2.0)
    assert worker.step() == pytest.approx(4.0)
    clock.advance(4.0)
    assert worker.step() == pytest.approx(8.0)
    clock.advance(8.0)
    assert worker.step() == pytest.approx(8.0)  # capped
    assert worker.health().failures == 5
    assert worker.health().offline_since == 0.0


def test_recovers_and_resets_backoff() -> None:
    clock = FakeClock()
    camera = FakeCamera()
    camera.available = False
    worker = _worker(camera, clock)
    worker.step()
    clock.advance(1.0)
    worker.step()  # second failure, next delay 2 s

    camera.available = True
    clock.advance(2.0)
    assert worker.step() == 0.0
    health = worker.health()
    assert health.status is CameraStatus.ONLINE
    assert health.offline_since is None
    assert health.reconnects == 1

    # A new failure starts again from the initial delay.
    camera.failing = True
    assert worker.step() == pytest.approx(1.0)
    assert worker.health().status is CameraStatus.OFFLINE


def test_read_failure_closes_the_source_and_reopens_a_fresh_one() -> None:
    clock = FakeClock()
    camera = FakeCamera()
    worker = _worker(camera, clock)
    worker.step()
    camera.failing = True
    worker.step()
    assert camera.closes == 1
    camera.failing = False
    clock.advance(1.0)
    assert worker.step() == 0.0
    assert camera.opens == 2
    assert worker.health().status is CameraStatus.ONLINE


def test_empty_read_counts_as_failure() -> None:
    clock = FakeClock()
    camera = FakeCamera()
    camera.return_none = True
    worker = _worker(camera, clock)
    assert worker.step() == pytest.approx(1.0)
    health = worker.health()
    assert health.status is CameraStatus.OFFLINE
    assert health.last_error == "NoFrame"


def test_factory_errors_are_caught() -> None:
    def broken() -> CameraSource:
        raise ValueError("bad uri " + URI_WITH_CREDENTIALS)

    worker = CameraWorker(slot=2, source_factory=broken, clock=FakeClock())
    delay = worker.step()
    assert delay is not None and delay > 0
    assert worker.health().status is CameraStatus.OFFLINE
    assert worker.health().last_error == "ValueError"


def test_errors_in_close_are_swallowed() -> None:
    clock = FakeClock()
    camera = FakeCamera()
    camera.fail_close = True
    worker = _worker(camera, clock)
    worker.step()
    camera.failing = True
    assert worker.step() == pytest.approx(1.0)
    assert worker.health().status is CameraStatus.OFFLINE


def test_end_of_stream_stops_without_reconnecting() -> None:
    clock = FakeClock()
    camera = FakeCamera(frames=1)
    worker = _worker(camera, clock)
    assert worker.step() == 0.0
    assert worker.step() is None
    assert worker.health().status is CameraStatus.STOPPED
    assert camera.closes == 1
    assert worker.step() is None
    assert camera.opens == 1


def test_stale_frames_report_offline() -> None:
    clock = FakeClock()
    worker = _worker(FakeCamera(), clock, stale_after_s=2.0)
    worker.step()
    clock.advance(1.9)
    assert worker.health().status is CameraStatus.ONLINE
    clock.advance(0.2)
    health = worker.health()
    assert health.status is CameraStatus.OFFLINE
    assert health.fps == 0.0
    assert health.last_error == "Stale"


def test_logs_never_contain_the_uri(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> CameraSource:
        raise CaptureError("cannot open " + URI_WITH_CREDENTIALS)

    caplog.set_level(logging.DEBUG)
    worker = CameraWorker(slot=3, source_factory=broken, clock=FakeClock())
    worker.step()
    assert caplog.records, "an offline transition is logged"
    assert "hunter2" not in caplog.text
    assert URI_WITH_CREDENTIALS not in caplog.text
    assert worker.health().last_error == "CaptureError"


def test_bad_stale_after_is_rejected() -> None:
    with pytest.raises(ValueError):
        CameraWorker(slot=1, source_factory=FakeCamera().make_source, stale_after_s=0)


# --- threaded ---------------------------------------------------------------


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.005)
    return predicate()


def test_thread_runs_and_stops() -> None:
    camera = FakeCamera(fps=200.0)
    worker = CameraWorker(slot=1, source_factory=camera.make_source, clock=SystemClock())
    worker.start()
    worker.start()  # idempotent
    try:
        assert worker.is_running
        assert worker.frames.take(timeout=2.0) is not None
    finally:
        worker.stop(timeout=2.0)
    assert not worker.is_running
    assert worker.health().status is CameraStatus.STOPPED
    assert camera.closes == camera.opens


def test_hung_read_is_abandoned_and_capture_restarts() -> None:
    camera = FakeCamera(fps=200.0)
    worker = CameraWorker(
        slot=1,
        source_factory=camera.make_source,
        clock=SystemClock(),
        backoff=Backoff(initial_s=0.01, factor=2.0, max_s=0.05),
        stale_after_s=0.05,
    )
    worker.start()
    try:
        assert worker.frames.take(timeout=2.0) is not None
        camera.hang()
        assert _wait_until(lambda: worker.health().status is CameraStatus.OFFLINE)
        assert worker.health().last_error == "Hung"
        assert worker.restart_if_hung()
        # The new thread opens a fresh source; it gets frames once the
        # (shared) fake device stops hanging.
        camera.release()
        seen = worker.frames.peek()
        assert seen is not None
        assert _wait_until(lambda: (f := worker.frames.peek()) is not None and f.seq > seen.seq)
        assert worker.health().status is CameraStatus.ONLINE
        assert not worker.restart_if_hung()
    finally:
        worker.stop(timeout=2.0)
    # The abandoned source was closed when its read returned.
    assert _wait_until(lambda: camera.closes == camera.opens)
