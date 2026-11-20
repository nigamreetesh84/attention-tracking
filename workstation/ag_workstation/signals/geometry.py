"""Pure geometry over landmark points: distances, EAR, MAR, polygon test, posture.

x coordinates are in image widths and y in image heights, so every distance
takes the image aspect ratio (width / height) and is returned in image
heights.
"""

import math
from collections.abc import Sequence

from ag_shared.config import RoiPoint

from ag_workstation.landmarks.types import EyePoints, MouthPoints, Point, PoseObservation

_EPS = 1e-9


def clamp(value: float, low: float, high: float) -> float:
    """`value` limited to [low, high]."""
    return max(low, min(high, value))


def ramp(value: float, low: float, high: float) -> float:
    """0 at or below `low`, 1 at or above `high`, linear in between."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return clamp((value - low) / (high - low), 0.0, 1.0)


def distance(a: Point, b: Point, aspect: float) -> float:
    """Distance between two normalized points, in image heights."""
    return math.hypot((a.x - b.x) * aspect, a.y - b.y)


def eye_aspect_ratio(eye: EyePoints, aspect: float) -> float | None:
    """EAR = (|p2 p6| + |p3 p5|) / (2 |p1 p4|); None for a collapsed eye."""
    p1, p2, p3, p4, p5, p6 = eye
    length = distance(p1, p4, aspect)
    if length < _EPS:
        return None
    return (distance(p2, p6, aspect) + distance(p3, p5, aspect)) / (2.0 * length)


def mouth_aspect_ratio(mouth: MouthPoints, aspect: float) -> float | None:
    """Mean inner-lip opening over the mouth width; None for a collapsed mouth."""
    width = distance(mouth.left, mouth.right, aspect)
    if width < _EPS:
        return None
    gaps = [distance(up, low, aspect) for up, low in zip(mouth.upper, mouth.lower, strict=True)]
    return sum(gaps) / (len(gaps) * width)


def point_in_polygon(point: Point, polygon: Sequence[RoiPoint]) -> bool:
    """Whether `point` lies inside `polygon` (even-odd rule, any winding)."""
    inside = False
    count = len(polygon)
    for i in range(count):
        a, b = polygon[i], polygon[(i + 1) % count]
        if (a.y > point.y) != (b.y > point.y):
            crossing = a.x + (point.y - a.y) * (b.x - a.x) / (b.y - a.y)
            if point.x < crossing:
                inside = not inside
    return inside


def shoulder_width(pose: PoseObservation, aspect: float) -> float:
    """Distance between the shoulders, in image heights."""
    return distance(pose.left_shoulder.point, pose.right_shoulder.point, aspect)


def pose_visible(pose: PoseObservation, min_visibility: float) -> bool:
    """Whether nose and both shoulders are seen well enough to be used."""
    return all(
        p.visibility >= min_visibility for p in (pose.nose, pose.left_shoulder, pose.right_shoulder)
    )


def posture_slump(
    pose: PoseObservation, aspect: float, *, upright_ratio: float, min_visibility: float
) -> float | None:
    """How far the head has sunk towards the shoulder line, 0-1.

    0 is upright (the nose `upright_ratio` shoulder widths above the
    shoulders, or higher), 1 is the nose at or below shoulder level. None
    when the shoulders or the head are not visible.
    """
    if not pose_visible(pose, min_visibility):
        return None
    width = shoulder_width(pose, aspect)
    if width < _EPS or upright_ratio <= 0:
        return None
    shoulder_y = (pose.left_shoulder.y + pose.right_shoulder.y) / 2.0
    head_height = (shoulder_y - pose.nose.y) / width
    return clamp(1.0 - head_height / upright_ratio, 0.0, 1.0)
