"""Signal extraction (detection-engine.md section 2): landmarks to signals.

Pure Python over the landmark types: no camera, no model, no pixels and no
clock. `SignalExtractor` is the per-camera entry point; the functions and
window objects it is built from are importable from the submodules.
"""

from ag_workstation.signals.extractor import SignalExtractor
from ag_workstation.signals.geometry import (
    eye_aspect_ratio,
    mouth_aspect_ratio,
    point_in_polygon,
    posture_slump,
)
from ag_workstation.signals.params import ExtractionParams
from ag_workstation.signals.quality import face_quality, frame_quality
from ag_workstation.signals.roi import select_face, select_pose
from ag_workstation.signals.windows import (
    BlinkRateCounter,
    EarBaseline,
    MotionTracker,
    NodDetector,
    PerclosWindow,
    YawnDetector,
)

__all__ = [
    "BlinkRateCounter",
    "EarBaseline",
    "ExtractionParams",
    "MotionTracker",
    "NodDetector",
    "PerclosWindow",
    "SignalExtractor",
    "YawnDetector",
    "eye_aspect_ratio",
    "face_quality",
    "frame_quality",
    "mouth_aspect_ratio",
    "point_in_polygon",
    "posture_slump",
    "select_face",
    "select_pose",
]
