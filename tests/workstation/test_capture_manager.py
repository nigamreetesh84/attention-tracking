"""CaptureManager: one worker per enabled camera, isolated failures.

Acceptance (PLAN 1.3): a failing source reports `offline` and recovers
without affecting other sources.
"""

import threading
import time
from collections.abc import Callable

import pytest
from ag_shared.config import CameraConfig, CameraKind, CameraRole
from ag_workstation.capture import (
    Backoff,
    CameraSource,
    CameraStatus,
    CaptureManager,
    FakeCamera,
)
from ag_workstation.clock import FakeClock, SystemClock


def _cam(slot: int, *, enabled: bool = True) -> CameraConfig:
    return CameraConfig(
        slot=slot,
        role=CameraRole.PRIMARY if slot == 1 else CameraRole.SECONDARY,
        kind=CameraKind.RTSP,
        uri=f"rtsp://user:secret@10.0.0.{slot}/s",
        is_enabled=enabled,
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.005)
    return predicate()


def test_only_enabled_cameras_get_workers() -> None:
    fakes = {1: FakeCamera(), 2: FakeCamera(), 3: FakeCamera()}
    manager = CaptureManager(
        [_cam(1), _cam(2, enabled=False), _cam(3)],
        source_factory=lambda cfg: fakes[cfg.slot].make_source(),
        clock=FakeClock(),
    )
    assert manager.slots == (1, 3)
    assert set(manager.health()) == {1, 3}
    with pytest.raises(KeyError):
        manager.frames(2)


def test_duplicate_slots_are_rejected() -> None:
    with pytest.raises(ValueError, match="slot"):
        CaptureManager([_cam(1), _cam(1)], source_factory=lambda _c: FakeCamera().make_source())


def test_failing_source_is_offline_and_recovers_without_affecting_others() -> None:
    clock = FakeClock()
    good, bad = FakeCamera(), FakeCamera()
    fakes = {1: good, 2: bad}
    manager = CaptureManager(
        [_cam(1), _cam(2)],
        source_factory=lambda cfg: fakes[cfg.slot].make_source(),
        clock=clock,
        backoff_factory=lambda: Backoff(initial_s=1.0, factor=2.0, max_s=4.0),
    )
    manager.step_all()
    assert {s: h.status for s, h in manager.health().items()} == {
        1: CameraStatus.ONLINE,
        2: CameraStatus.ONLINE,
    }

    bad.failing = True
    for _ in range(5):
        clock.advance(0.1)
        manager.step_all()
    health = manager.health()
    assert health[1].status is CameraStatus.ONLINE
    assert health[2].status is CameraStatus.OFFLINE
    latest_good = manager.latest(1)
    assert latest_good is not None and latest_good.ts == pytest.approx(0.5)
    assert manager.latest(2) is not None  # last good frame stays visible

    bad.failing = False
    clock.advance(1.0)
    manager.step_all()
    health = manager.health()
    assert health[1].status is CameraStatus.ONLINE
    assert health[2].status is CameraStatus.ONLINE
    assert health[2].reconnects == 1
    assert health[1].failures == 0


def test_threaded_isolation_with_a_hanging_and_a_failing_camera() -> None:
    good, flaky, hanging = FakeCamera(fps=200.0), FakeCamera(fps=200.0), FakeCamera(fps=200.0)
    fakes = {1: good, 2: flaky, 3: hanging}
    manager = CaptureManager(
        [_cam(1), _cam(2), _cam(3)],
        source_factory=lambda cfg: fakes[cfg.slot].make_source(),
        clock=SystemClock(),
        backoff_factory=lambda: Backoff(initial_s=0.01, factor=2.0, max_s=0.05),
        stale_after_s=0.1,
        supervise_interval_s=0.02,
    )
    manager.start()
    try:
        for slot in (1, 2, 3):
            assert manager.frames(slot).take(timeout=2.0) is not None

        flaky.failing = True
        hanging.hang()
        assert _wait_until(
            lambda: (
                manager.health()[2].status is CameraStatus.OFFLINE
                and manager.health()[3].status is CameraStatus.OFFLINE
            )
        )
        # Camera 1 keeps delivering fresh frames the whole time.
        before = manager.frames(1).take(timeout=1.0)
        assert before is not None
        after = manager.frames(1).take(timeout=1.0)
        assert after is not None and after.seq > before.seq
        assert manager.health()[1].status is CameraStatus.ONLINE

        flaky.failing = False
        hanging.release()
        assert _wait_until(
            lambda: all(h.status is CameraStatus.ONLINE for h in manager.health().values())
        )
    finally:
        manager.stop(timeout=2.0)
    assert all(h.status is CameraStatus.STOPPED for h in manager.health().values())


def test_start_and_stop_are_idempotent() -> None:
    manager = CaptureManager(
        [_cam(1)],
        source_factory=lambda _c: FakeCamera(fps=200.0).make_source(),
        clock=SystemClock(),
    )
    manager.stop()
    manager.start()
    manager.start()
    manager.stop(timeout=2.0)
    manager.stop(timeout=2.0)


def test_default_factory_is_used_when_none_given(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[int] = []

    def fake_make_source(config: CameraConfig) -> CameraSource:
        made.append(config.slot)
        return FakeCamera().make_source()

    monkeypatch.setattr("ag_workstation.capture.manager.make_source", fake_make_source)
    manager = CaptureManager([_cam(1)], clock=FakeClock())
    manager.step_all()
    assert made == [1]
    assert manager.health()[1].status is CameraStatus.ONLINE
