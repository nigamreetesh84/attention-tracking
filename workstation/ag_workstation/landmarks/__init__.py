"""Landmark layer (detection-engine.md section 1): frames to face/pose points.

`LandmarkProvider` is the interface; `MediaPipeLandmarkProvider` (in
`mediapipe_provider`, the only module that loads MediaPipe, and only on first
use) is the real implementation and is not re-exported here;
`FakeLandmarkProvider` and the `synthetic_*` builders serve tests and CI.

An observation carries a handful of points and numbers for one frame. No
face template, embedding or other identity feature is computed or stored.
"""

from ag_workstation.landmarks.fake import (
    FakeLandmarkProvider,
    RawCategory,
    RawFaceResult,
    RawLandmark,
    RawPoseResult,
    raw_face,
    raw_pose,
    rotation_matrix,
    synthetic_face,
    synthetic_pose,
    synthetic_result,
)
from ag_workstation.landmarks.types import (
    Box,
    EyePoints,
    FaceObservation,
    HeadPose,
    LandmarkError,
    LandmarkProvider,
    LandmarkResult,
    MouthPoints,
    Point,
    PoseObservation,
    PosePoint,
    RegionStats,
)

__all__ = [
    "Box",
    "EyePoints",
    "FaceObservation",
    "FakeLandmarkProvider",
    "HeadPose",
    "LandmarkError",
    "LandmarkProvider",
    "LandmarkResult",
    "MouthPoints",
    "Point",
    "PoseObservation",
    "PosePoint",
    "RawCategory",
    "RawFaceResult",
    "RawLandmark",
    "RawPoseResult",
    "RegionStats",
    "raw_face",
    "raw_pose",
    "rotation_matrix",
    "synthetic_face",
    "synthetic_pose",
    "synthetic_result",
]
