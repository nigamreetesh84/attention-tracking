"""Test and replay helpers for the engine: configs, a scriptable feed, frames.

Like `capture.fake` and `landmarks.fake`, these let the engine run in CI
without cameras or models.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
from ag_shared.config import CameraConfig, CameraKind, CameraRole, EngineConfig
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.thresholds import Thresholds

from ag_workstation.capture.latest import LatestFrame
from ag_workstation.capture.types import CameraHealth, CameraStatus, Frame
from ag_workstation.landmarks import synthetic_face, synthetic_result
from ag_workstation.landmarks.types import LandmarkResult

T0 = datetime(2026, 11, 16, 4, 30, tzinfo=UTC)  # 10:00 in India


def camera(slot: int, *, primary: bool = False, enabled: bool = True) -> CameraConfig:
    """A USB camera config for `slot`."""
    return CameraConfig(
        slot=slot,
        role=CameraRole.PRIMARY if primary else CameraRole.SECONDARY,
        kind=CameraKind.USB,
        uri=str(slot - 1),
        is_enabled=enabled,
    )


def engine_config(
    cameras: int | tuple[CameraConfig, ...] = 2,
    *,
    kind: PpeProfileKind = PpeProfileKind.OPEN_FACE,
    thresholds: Thresholds | None = None,
    **extra: Any,
) -> EngineConfig:
    """A valid engine config with `cameras` cameras (camera 1 primary)."""
    return EngineConfig(
        cameras=(
            cameras
            if isinstance(cameras, tuple)
            else tuple(camera(s, primary=s == 1) for s in range(1, cameras + 1))
        ),
        profile=builtin_profile(kind),
        thresholds=thresholds or Thresholds(),
        **extra,
    )


def image() -> np.ndarray[Any, np.dtype[np.uint8]]:
    """A tiny black BGR image."""
    return np.zeros((6, 8, 3), dtype=np.uint8)


def frame(slot: int, seq: int, ts: float) -> Frame:
    """A captured frame with a tiny image."""
    return Frame(camera_slot=slot, seq=seq, ts=ts, captured_at=T0, image=image())


def face_result(frame: Frame, *, ear: float = 0.30) -> LandmarkResult:
    """Landmarks of one frontal face with eye aspect ratio `ear`."""
    return synthetic_result(
        frame.ts, (synthetic_face(ear=ear),), camera_slot=frame.camera_slot, width=8, height=6
    )


def no_face_result(frame: Frame) -> LandmarkResult:
    """Landmarks of a frame with nobody in it."""
    return synthetic_result(frame.ts, (), camera_slot=frame.camera_slot, width=8, height=6)


class StubFeed:
    """A `FrameFeed` whose frames and health the test controls."""

    def __init__(self, cameras: Sequence[CameraConfig]) -> None:
        self._slots = tuple(c.slot for c in cameras)
        self._frames = {slot: LatestFrame() for slot in self._slots}
        self.status = dict.fromkeys(self._slots, CameraStatus.ONLINE)
        self.started = False
        self.stopped = False

    @property
    def slots(self) -> tuple[int, ...]:
        return self._slots

    def frames(self, slot: int) -> LatestFrame:
        return self._frames[slot]

    def health(self) -> dict[int, CameraHealth]:
        return {
            slot: CameraHealth(
                slot=slot,
                status=status,
                fps=10.0,
                frames_captured=0,
                frames_dropped=0,
                failures=0,
                reconnects=0,
                last_error=None,
                last_frame_ts=None,
                offline_since=None,
            )
            for slot, status in self.status.items()
        }

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True
