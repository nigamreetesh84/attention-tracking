"""The `--dev --camera` manual-check mode: real engine in the UI process."""

import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from ag_shared.config import CameraConfig, CameraKind
from ag_shared.states import AttentionState
from ag_workstation.auth import StaticRosterSource
from ag_workstation.capture import CaptureManager, FakeCamera
from ag_workstation.clock import SystemClock
from ag_workstation.landmarks import FakeLandmarkProvider
from ag_workstation.queue import InMemoryKeyProvider
from ag_workstation.ui import app as app_module
from ag_workstation.ui.app import DEV_PIN, assemble, dev_bundle
from ag_workstation.ui.camera_dev import start_local_station
from argon2 import PasswordHasher

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def _wait(cond: Callable[[], bool], limit: float = 10.0) -> None:
    end = time.monotonic() + limit
    while not cond():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.02)


def test_dev_bundle_takes_the_camera() -> None:
    usb = dev_bundle(FAST_HASHER.hash(DEV_PIN), camera="1")
    assert usb.cameras[0].kind is CameraKind.USB and usb.cameras[0].uri == "1"
    rtsp = dev_bundle(FAST_HASHER.hash(DEV_PIN), camera="rtsp://cam/stream")
    assert rtsp.cameras[0].kind is CameraKind.RTSP


def test_camera_mode_needs_dev() -> None:
    with pytest.raises(SystemExit):
        app_module._parse(["--camera", "0"])
    args = app_module._parse(["--dev", "--camera", "0", "--models", "models"])
    assert args.camera == "0" and args.models == Path("models")


def test_local_station_runs_the_real_engine_for_the_scan_screen(tmp_path: Path) -> None:
    clock = SystemClock()
    keys = InMemoryKeyProvider(bytes(32))
    fake_cam = FakeCamera(width=64, height=48, fps=15)

    def feed(cams: Sequence[CameraConfig]) -> CaptureManager:
        return CaptureManager(cams, source_factory=lambda c: fake_cam.make_source())

    local = start_local_station(
        data_dir=tmp_path,
        models=None,
        keys=keys,
        clock=clock,
        feed_factory=feed,
        provider_factory=lambda cam: FakeLandmarkProvider(),
    )
    assembly = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=clock,
        ipc=local.ipc,
        control=local.control,
        roster=StaticRosterSource(dev_bundle(FAST_HASHER.hash(DEV_PIN))),
        keys=keys,
        hasher=FAST_HASHER,
    )
    try:
        assert assembly.engine_mode == "ipc"
        # The UI pushes the dev bundle's camera config; that starts the real engine.
        _wait(lambda: local.child.adapter.engine is not None)
        _wait(lambda: local.ipc.current_state().state is not AttentionState.UNKNOWN)
        assert assembly.flow is not None
        result = assembly.flow.on_barcode("ABC123")
        assert result.barcode == "ABC123"
    finally:
        assembly.close()
        local.close()
