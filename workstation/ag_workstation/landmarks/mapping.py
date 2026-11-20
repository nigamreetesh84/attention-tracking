"""Turn MediaPipe-shaped results into observations.

Pure functions over duck-typed inputs: this module does not load MediaPipe,
so the mapping is tested with plain fakes. Only the listed landmark indices
are copied out of the face mesh; the rest of the mesh is dropped here.
"""

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

import numpy as np

from ag_workstation.capture.types import Frame, Image
from ag_workstation.landmarks.image_stats import eye_region, highlight_fraction, region_stats
from ag_workstation.landmarks.types import (
    Box,
    EyePoints,
    FaceObservation,
    HeadPose,
    LandmarkResult,
    MouthPoints,
    Point,
    PoseObservation,
    PosePoint,
)

# MediaPipe face mesh indices (canonical 468-point topology).
RIGHT_EYE = (33, 160, 158, 133, 153, 144)
LEFT_EYE = (362, 385, 387, 263, 373, 380)
MOUTH_LEFT = 78
MOUTH_RIGHT = 308
MOUTH_UPPER = (81, 13, 311)
MOUTH_LOWER = (178, 14, 402)
MOTION_ANCHORS = (1, 152, 10)
"""Nose tip, chin, forehead."""
FACE_LANDMARK_COUNT = 468
"""Mesh size without the iris points (478 with them)."""

# MediaPipe pose indices.
NOSE = 0
LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
POSE_LANDMARK_COUNT = 13

BLINK_LEFT = "eyeBlinkLeft"
BLINK_RIGHT = "eyeBlinkRight"

_EPS = 1e-9


class LandmarkLike(Protocol):
    """A MediaPipe `NormalizedLandmark`, as far as it is read here."""

    @property
    def x(self) -> float: ...
    @property
    def y(self) -> float: ...


class PoseLandmarkLike(LandmarkLike, Protocol):
    """A pose landmark; `visibility` may be missing."""

    @property
    def visibility(self) -> float | None: ...


class CategoryLike(Protocol):
    """A MediaPipe blendshape `Category`."""

    @property
    def category_name(self) -> str: ...
    @property
    def score(self) -> float: ...


class FaceResultLike(Protocol):
    """A MediaPipe `FaceLandmarkerResult`."""

    @property
    def face_landmarks(self) -> Sequence[Sequence[LandmarkLike]]: ...
    @property
    def face_blendshapes(self) -> Sequence[Sequence[CategoryLike]]: ...
    @property
    def facial_transformation_matrixes(self) -> Sequence[object]: ...


class PoseResultLike(Protocol):
    """A MediaPipe `PoseLandmarkerResult`."""

    @property
    def pose_landmarks(self) -> Sequence[Sequence[PoseLandmarkLike]]: ...


def head_pose_from_matrix(matrix: object) -> HeadPose | None:
    """Yaw, pitch and roll in degrees from a facial transformation matrix.

    The matrix maps the canonical face into camera space (x right, y up, z
    towards the camera) and may carry scale and translation, which are
    ignored. The rotation is read as R = Ry(yaw) * Rx(-pitch) * Rz(roll), so
    pitch is negative when the head is down. Returns None for anything that
    is not a usable rotation.
    """
    try:
        m = np.asarray(matrix, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if m.ndim != 2 or m.shape[0] < 3 or m.shape[1] < 3:
        return None
    r = m[:3, :3]
    norms = np.linalg.norm(r, axis=0)
    if not np.all(np.isfinite(r)) or np.any(norms < _EPS):
        return None
    r = r / norms
    pitch = math.asin(max(-1.0, min(1.0, float(r[1, 2]))))
    yaw = math.atan2(float(r[0, 2]), float(r[2, 2]))
    roll = math.atan2(float(r[1, 0]), float(r[1, 1]))
    return HeadPose(
        yaw_deg=math.degrees(yaw), pitch_deg=math.degrees(pitch), roll_deg=math.degrees(roll)
    )


def _point(landmark: LandmarkLike) -> Point:
    return Point(float(landmark.x), float(landmark.y))


def _finite(points: Sequence[Point]) -> bool:
    return all(math.isfinite(p.x) and math.isfinite(p.y) for p in points)


def _blink_scores(
    blendshapes: Sequence[CategoryLike] | None,
) -> tuple[float | None, float | None]:
    """(right, left) blink scores; other blendshapes are not read."""
    right = left = None
    for category in blendshapes or ():
        if category.category_name == BLINK_RIGHT:
            right = float(category.score)
        elif category.category_name == BLINK_LEFT:
            left = float(category.score)
    return right, left


def map_face(
    landmarks: Sequence[LandmarkLike],
    blendshapes: Sequence[CategoryLike] | None,
    matrix: object | None,
) -> FaceObservation | None:
    """One face's observation, or None when the landmark list is unusable."""
    if len(landmarks) < FACE_LANDMARK_COUNT:
        return None
    xs = [float(lm.x) for lm in landmarks]
    ys = [float(lm.y) for lm in landmarks]
    if not all(map(math.isfinite, xs)) or not all(map(math.isfinite, ys)):
        return None

    def pick(index: int) -> Point:
        return Point(xs[index], ys[index])

    r, le = RIGHT_EYE, LEFT_EYE
    right_eye: EyePoints = (pick(r[0]), pick(r[1]), pick(r[2]), pick(r[3]), pick(r[4]), pick(r[5]))
    left_eye: EyePoints = (
        pick(le[0]),
        pick(le[1]),
        pick(le[2]),
        pick(le[3]),
        pick(le[4]),
        pick(le[5]),
    )
    up, lo = MOUTH_UPPER, MOUTH_LOWER
    mouth = MouthPoints(
        left=pick(MOUTH_LEFT),
        right=pick(MOUTH_RIGHT),
        upper=(pick(up[0]), pick(up[1]), pick(up[2])),
        lower=(pick(lo[0]), pick(lo[1]), pick(lo[2])),
    )
    blink_right, blink_left = _blink_scores(blendshapes)
    return FaceObservation(
        box=Box(
            x_min=max(0.0, min(xs)),
            y_min=max(0.0, min(ys)),
            x_max=min(1.0, max(xs)),
            y_max=min(1.0, max(ys)),
        ),
        right_eye=right_eye,
        left_eye=left_eye,
        mouth=mouth,
        anchors=tuple(pick(i) for i in MOTION_ANCHORS),
        head_pose=head_pose_from_matrix(matrix) if matrix is not None else None,
        blink_right=blink_right,
        blink_left=blink_left,
    )


def map_pose(landmarks: Sequence[PoseLandmarkLike]) -> PoseObservation | None:
    """Nose and shoulders of one pose, or None when the list is unusable."""
    if len(landmarks) < POSE_LANDMARK_COUNT:
        return None

    def pick(index: int) -> PosePoint:
        landmark = landmarks[index]
        visibility = getattr(landmark, "visibility", None)
        return PosePoint(
            x=float(landmark.x),
            y=float(landmark.y),
            visibility=1.0 if visibility is None else float(visibility),
        )

    pose = PoseObservation(
        nose=pick(NOSE), left_shoulder=pick(LEFT_SHOULDER), right_shoulder=pick(RIGHT_SHOULDER)
    )
    points = (pose.nose, pose.left_shoulder, pose.right_shoulder)
    if not _finite([p.point for p in points]):
        return None
    if not all(math.isfinite(p.visibility) for p in points):
        return None
    return pose


def _with_image_stats(face: FaceObservation, image: Image, aspect: float) -> FaceObservation:
    return replace(
        face,
        stats=region_stats(image, face.box),
        glare_right=highlight_fraction(image, eye_region(face.right_eye, aspect)),
        glare_left=highlight_fraction(image, eye_region(face.left_eye, aspect)),
    )


def build_result(
    frame: Frame, face_result: FaceResultLike, pose_result: PoseResultLike | None
) -> LandmarkResult:
    """The `LandmarkResult` of one frame from the two model outputs.

    `pose_result` is None when the pose model was not run.
    """
    image = frame.image
    height, width = int(image.shape[0]), int(image.shape[1])
    aspect = width / height if height > 0 else 1.0
    blendshapes = face_result.face_blendshapes or ()
    matrices = face_result.facial_transformation_matrixes
    if matrices is None:  # MediaPipe leaves the field empty or None when not requested
        matrices = ()
    faces: list[FaceObservation] = []
    for index, landmarks in enumerate(face_result.face_landmarks or ()):
        face = map_face(
            landmarks,
            blendshapes[index] if index < len(blendshapes) else None,
            matrices[index] if index < len(matrices) else None,
        )
        if face is not None:
            faces.append(_with_image_stats(face, image, aspect))
    poses: tuple[PoseObservation, ...] | None = None
    if pose_result is not None:
        mapped = (map_pose(landmarks) for landmarks in pose_result.pose_landmarks or ())
        poses = tuple(pose for pose in mapped if pose is not None)
    return LandmarkResult(
        camera_slot=frame.camera_slot,
        ts=frame.ts,
        width=width,
        height=height,
        faces=tuple(faces),
        poses=poses,
        frame_stats=region_stats(image, None),
    )
