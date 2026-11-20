"""Synthetic landmarks and a fake provider for tests, demos and CI.

`synthetic_face` and `synthetic_pose` build observations with chosen signal
values (EAR, MAR, head pose, posture); `raw_face` and `raw_pose` wrap them in
MediaPipe-shaped containers to exercise the mapping; `FakeLandmarkProvider`
stands in for the real models.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from ag_workstation.capture.types import Frame
from ag_workstation.landmarks import mapping
from ag_workstation.landmarks.types import (
    Box,
    EyePoints,
    FaceObservation,
    HeadPose,
    LandmarkError,
    LandmarkResult,
    MouthPoints,
    Point,
    PoseObservation,
    PosePoint,
    RegionStats,
)

LEVEL_HEAD = HeadPose(0.0, 0.0, 0.0)
GOOD_STATS = RegionStats(brightness=0.5, sharpness=100.0)
_CENTER = Point(0.5, 0.5)


def _eye(cx: float, cy: float, half_length: float, ear: float, aspect: float) -> EyePoints:
    """An eye whose aspect ratio is exactly `ear` (lengths in image heights)."""
    a = half_length / aspect  # x offsets are in image widths
    e = ear * half_length
    return (
        Point(cx - a, cy),
        Point(cx - a / 2, cy - e),
        Point(cx + a / 2, cy - e),
        Point(cx + a, cy),
        Point(cx + a / 2, cy + e),
        Point(cx - a / 2, cy + e),
    )


def synthetic_face(
    *,
    center: Point = _CENTER,
    width: float = 0.2,
    ear: float = 0.30,
    left_ear: float | None = None,
    mar: float = 0.05,
    head_pose: HeadPose | None = LEVEL_HEAD,
    blink: float | None = None,
    glare: tuple[float, float] | None = None,
    stats: RegionStats | None = GOOD_STATS,
    confidence: float | None = None,
    aspect: float = 1.0,
) -> FaceObservation:
    """A face observation with the given measurements.

    `width` is the face width as a share of the image width; `ear` applies to
    both eyes unless `left_ear` is given; `glare` is (right, left); `aspect`
    is the image width over height the points are laid out for.
    """
    height = width * aspect * 1.3
    cx, cy = center
    eye_half = 0.12 * width * aspect
    eye_y = cy - 0.15 * height
    mouth_half = 0.15 * width * aspect
    mouth_y = cy + 0.25 * height
    gap = mar * mouth_half
    mouth_xs = (cx - mouth_half / (2 * aspect), cx, cx + mouth_half / (2 * aspect))
    return FaceObservation(
        box=Box(cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2),
        right_eye=_eye(cx - 0.2 * width, eye_y, eye_half, ear, aspect),
        left_eye=_eye(
            cx + 0.2 * width, eye_y, eye_half, ear if left_ear is None else left_ear, aspect
        ),
        mouth=MouthPoints(
            left=Point(cx - mouth_half / aspect, mouth_y),
            right=Point(cx + mouth_half / aspect, mouth_y),
            upper=(
                Point(mouth_xs[0], mouth_y - gap),
                Point(mouth_xs[1], mouth_y - gap),
                Point(mouth_xs[2], mouth_y - gap),
            ),
            lower=(
                Point(mouth_xs[0], mouth_y + gap),
                Point(mouth_xs[1], mouth_y + gap),
                Point(mouth_xs[2], mouth_y + gap),
            ),
        ),
        anchors=(Point(cx, cy), Point(cx, cy + height / 2), Point(cx, cy - height / 2)),
        head_pose=head_pose,
        blink_right=blink,
        blink_left=blink,
        confidence=confidence,
        stats=stats,
        glare_right=None if glare is None else glare[0],
        glare_left=None if glare is None else glare[1],
    )


def synthetic_pose(
    *,
    center_x: float = 0.5,
    nose_y: float = 0.4,
    shoulder_width: float = 0.3,
    head_height_ratio: float = 0.6,
    visibility: float = 1.0,
    aspect: float = 1.0,
) -> PoseObservation:
    """An upper body with the nose `head_height_ratio` shoulder widths above
    the shoulder line. `shoulder_width` is a share of the image width."""
    shoulder_y = nose_y + head_height_ratio * shoulder_width * aspect
    return PoseObservation(
        nose=PosePoint(center_x, nose_y, visibility),
        # The worker's left shoulder is on the right of an unmirrored image.
        left_shoulder=PosePoint(center_x + shoulder_width / 2, shoulder_y, visibility),
        right_shoulder=PosePoint(center_x - shoulder_width / 2, shoulder_y, visibility),
    )


def synthetic_result(
    ts: float,
    faces: Sequence[FaceObservation] = (),
    *,
    poses: Sequence[PoseObservation] | None = None,
    camera_slot: int = 1,
    width: int = 640,
    height: int = 640,
    frame_stats: RegionStats | None = GOOD_STATS,
) -> LandmarkResult:
    """A `LandmarkResult` around the given observations."""
    return LandmarkResult(
        camera_slot=camera_slot,
        ts=ts,
        width=width,
        height=height,
        faces=tuple(faces),
        poses=None if poses is None else tuple(poses),
        frame_stats=frame_stats,
    )


def rotation_matrix(
    yaw_deg: float,
    pitch_deg: float,
    roll_deg: float,
    *,
    scale: float = 1.0,
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> npt.NDArray[np.float64]:
    """A 4x4 facial transformation matrix for the given head pose.

    Same convention as `mapping.head_pose_from_matrix`: camera space with x
    right, y up, z towards the camera; R = Ry(yaw) * Rx(-pitch) * Rz(roll).
    """
    a, b, c = math.radians(yaw_deg), math.radians(-pitch_deg), math.radians(roll_deg)
    ry = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])
    rx = np.array([[1, 0, 0], [0, math.cos(b), -math.sin(b)], [0, math.sin(b), math.cos(b)]])
    rz = np.array([[math.cos(c), -math.sin(c), 0], [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = scale * (ry @ rx @ rz)
    matrix[:3, 3] = translation
    return matrix


# --- MediaPipe-shaped containers ---------------------------------------------------


@dataclass
class RawLandmark:
    """Stands in for a MediaPipe `NormalizedLandmark`."""

    x: float
    y: float
    z: float = 0.0
    visibility: float | None = None


@dataclass
class RawCategory:
    """Stands in for a MediaPipe blendshape `Category`."""

    category_name: str
    score: float


@dataclass
class RawFaceResult:
    """Stands in for a MediaPipe `FaceLandmarkerResult`."""

    face_landmarks: list[list[RawLandmark]]
    face_blendshapes: list[list[RawCategory]] = field(default_factory=list)
    facial_transformation_matrixes: list[npt.NDArray[np.float64]] = field(default_factory=list)


@dataclass
class RawPoseResult:
    """Stands in for a MediaPipe `PoseLandmarkerResult`."""

    pose_landmarks: list[list[RawLandmark]]


def raw_face(face: FaceObservation, count: int = 478) -> list[RawLandmark]:
    """A MediaPipe-shaped landmark list that carries `face`'s points."""
    center = face.box.center
    raw = [RawLandmark(center.x, center.y) for _ in range(count)]
    # Indices the mapping does not read; they only span the bounding box.
    raw[234] = RawLandmark(face.box.x_min, center.y)
    raw[454] = RawLandmark(face.box.x_max, center.y)
    raw[9] = RawLandmark(center.x, face.box.y_min)
    raw[200] = RawLandmark(center.x, face.box.y_max)
    placed: tuple[tuple[Sequence[int], Sequence[Point]], ...] = (
        (mapping.RIGHT_EYE, face.right_eye),
        (mapping.LEFT_EYE, face.left_eye),
        ((mapping.MOUTH_LEFT, mapping.MOUTH_RIGHT), (face.mouth.left, face.mouth.right)),
        (mapping.MOUTH_UPPER, face.mouth.upper),
        (mapping.MOUTH_LOWER, face.mouth.lower),
        (mapping.MOTION_ANCHORS, face.anchors),
    )
    for indices, points in placed:
        for index, point in zip(indices, points, strict=True):
            raw[index] = RawLandmark(point.x, point.y)
    return raw


def raw_pose(pose: PoseObservation, count: int = 33) -> list[RawLandmark]:
    """A MediaPipe-shaped pose landmark list that carries `pose`'s points."""
    raw = [RawLandmark(0.5, 0.9, visibility=0.9) for _ in range(count)]
    for index, point in (
        (mapping.NOSE, pose.nose),
        (mapping.LEFT_SHOULDER, pose.left_shoulder),
        (mapping.RIGHT_SHOULDER, pose.right_shoulder),
    ):
        raw[index] = RawLandmark(point.x, point.y, visibility=point.visibility)
    return raw


# --- fake provider -----------------------------------------------------------------

Script = Callable[[Frame, bool], LandmarkResult]
"""(frame, with_pose) -> the result to return."""


class FakeLandmarkProvider:
    """A `LandmarkProvider` that needs no models.

    Without a script every frame shows one attentive face (and an upright
    pose when asked for).
    """

    def __init__(self, script: Script | None = None) -> None:
        self._script = script
        self.calls = 0
        self.failing = False
        """True: `detect` raises `LandmarkError`."""
        self.closed = False

    def detect(self, frame: Frame, *, with_pose: bool = False) -> LandmarkResult:
        """The scripted (or default) result for `frame`."""
        if self.closed:
            raise LandmarkError("provider is closed")
        if self.failing:
            raise LandmarkError("fake landmark failure")
        self.calls += 1
        if self._script is not None:
            return self._script(frame, with_pose)
        height, width = frame.image.shape[:2]
        return synthetic_result(
            frame.ts,
            (synthetic_face(aspect=width / height if height else 1.0),),
            poses=(synthetic_pose(),) if with_pose else None,
            camera_slot=frame.camera_slot,
            width=int(width),
            height=int(height),
        )

    def close(self) -> None:
        """Mark the provider closed."""
        self.closed = True
