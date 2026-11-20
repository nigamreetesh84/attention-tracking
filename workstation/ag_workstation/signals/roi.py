"""ROI filter: which detected face and body belong to the worker.

`docs/detection-engine.md` section 2: the largest face inside the ROI wins.
A face is inside when the centre of its box is inside the polygon; no ROI
means the whole frame.
"""

from collections.abc import Sequence

from ag_shared.config import RoiPoint

from ag_workstation.landmarks.types import FaceObservation, Point, PoseObservation
from ag_workstation.signals.geometry import point_in_polygon

Roi = Sequence[RoiPoint] | None


def in_roi(point: Point, roi: Roi) -> bool:
    """Whether `point` is inside the ROI (always true without one)."""
    return roi is None or point_in_polygon(point, roi)


def select_face(faces: Sequence[FaceObservation], roi: Roi) -> FaceObservation | None:
    """The largest face whose centre is inside the ROI, or None."""
    candidates = [face for face in faces if in_roi(face.box.center, roi)]
    if not candidates:
        return None
    return max(candidates, key=lambda face: face.box.area)


def select_pose(
    poses: Sequence[PoseObservation], roi: Roi, face: FaceObservation | None
) -> PoseObservation | None:
    """The worker's body among the detected poses, or None.

    Only poses whose nose is inside the ROI count. With a selected face the
    pose whose nose is nearest to it is taken; otherwise the one with the
    widest shoulders (the nearest person).
    """
    candidates = [pose for pose in poses if in_roi(pose.nose.point, roi)]
    if not candidates:
        return None
    if face is not None:
        center = face.box.center

        def offset(pose: PoseObservation) -> float:
            return (pose.nose.x - center.x) ** 2 + (pose.nose.y - center.y) ** 2

        return min(candidates, key=offset)
    return max(candidates, key=lambda pose: abs(pose.left_shoulder.x - pose.right_shoulder.x))
