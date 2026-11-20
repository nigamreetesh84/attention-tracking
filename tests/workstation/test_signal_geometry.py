"""Pure geometry behind the signals: EAR, MAR, ROI polygon, posture, selection."""

import pytest
from ag_shared.config import RoiPoint
from ag_workstation.landmarks import (
    Box,
    MouthPoints,
    Point,
    PoseObservation,
    PosePoint,
    synthetic_face,
    synthetic_pose,
)
from ag_workstation.signals.geometry import (
    distance,
    eye_aspect_ratio,
    mouth_aspect_ratio,
    point_in_polygon,
    posture_slump,
)
from ag_workstation.signals.roi import select_face, select_pose

SQUARE = tuple(RoiPoint(x=x, y=y) for x, y in ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)))
# An L shape: the notch (upper right) is outside.
L_SHAPE = tuple(
    RoiPoint(x=x, y=y)
    for x, y in ((0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.0, 1.0))
)


# --- distance, EAR, MAR -------------------------------------------------------


def test_distance_scales_x_by_the_aspect_ratio() -> None:
    assert distance(Point(0.0, 0.0), Point(0.3, 0.4), 1.0) == pytest.approx(0.5)
    # 16:9: 0.1 of the width is 0.1 * 16/9 image heights.
    assert distance(Point(0.0, 0.0), Point(0.1, 0.0), 16 / 9) == pytest.approx(0.1 * 16 / 9)


@pytest.mark.parametrize("ear", [0.05, 0.2, 0.3, 0.42])
@pytest.mark.parametrize("aspect", [1.0, 4 / 3, 16 / 9])
def test_eye_aspect_ratio_of_a_synthetic_eye(ear: float, aspect: float) -> None:
    face = synthetic_face(ear=ear, aspect=aspect)
    assert eye_aspect_ratio(face.right_eye, aspect) == pytest.approx(ear)
    assert eye_aspect_ratio(face.left_eye, aspect) == pytest.approx(ear)


def test_eye_aspect_ratio_by_hand() -> None:
    eye = (
        Point(0.0, 0.5),
        Point(0.1, 0.44),
        Point(0.3, 0.44),
        Point(0.4, 0.5),
        Point(0.3, 0.56),
        Point(0.1, 0.56),
    )
    # (0.12 + 0.12) / (2 * 0.4)
    assert eye_aspect_ratio(eye, 1.0) == pytest.approx(0.3)


def test_eye_aspect_ratio_of_a_degenerate_eye_is_none() -> None:
    p = Point(0.5, 0.5)
    assert eye_aspect_ratio((p, p, p, p, p, p), 1.0) is None


@pytest.mark.parametrize("mar", [0.0, 0.05, 0.6, 0.9])
@pytest.mark.parametrize("aspect", [1.0, 16 / 9])
def test_mouth_aspect_ratio_of_a_synthetic_mouth(mar: float, aspect: float) -> None:
    face = synthetic_face(mar=mar, aspect=aspect)
    assert mouth_aspect_ratio(face.mouth, aspect) == pytest.approx(mar, abs=1e-9)


def test_mouth_aspect_ratio_of_a_degenerate_mouth_is_none() -> None:
    p = Point(0.5, 0.5)
    assert mouth_aspect_ratio(MouthPoints(p, p, (p, p, p), (p, p, p)), 1.0) is None


# --- ROI polygon ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "inside"),
    [
        (Point(0.5, 0.5), True),
        (Point(0.21, 0.79), True),
        (Point(0.1, 0.5), False),
        (Point(0.5, 0.9), False),
        (Point(0.95, 0.95), False),
    ],
)
def test_point_in_square_polygon(point: Point, inside: bool) -> None:
    assert point_in_polygon(point, SQUARE) is inside


@pytest.mark.parametrize(
    ("point", "inside"),
    [
        (Point(0.25, 0.25), True),
        (Point(0.75, 0.75), True),
        (Point(0.25, 0.75), True),
        (Point(0.75, 0.25), False),  # the notch
    ],
)
def test_point_in_concave_polygon(point: Point, inside: bool) -> None:
    assert point_in_polygon(point, L_SHAPE) is inside


def test_polygon_winding_does_not_matter() -> None:
    assert point_in_polygon(Point(0.5, 0.5), tuple(reversed(SQUARE)))
    assert not point_in_polygon(Point(0.1, 0.1), tuple(reversed(SQUARE)))


# --- face selection ------------------------------------------------------------


def test_no_faces_selects_nothing() -> None:
    assert select_face((), None) is None
    assert select_face((), SQUARE) is None


def test_without_roi_the_largest_face_wins() -> None:
    small = synthetic_face(center=Point(0.3, 0.5), width=0.1)
    large = synthetic_face(center=Point(0.7, 0.5), width=0.2)
    assert select_face((small, large), None) is large
    assert select_face((large, small), None) is large


def test_largest_face_inside_the_roi_wins() -> None:
    passer_by = synthetic_face(center=Point(0.1, 0.5), width=0.3)  # bigger, outside
    worker = synthetic_face(center=Point(0.5, 0.5), width=0.15)
    colleague = synthetic_face(center=Point(0.6, 0.6), width=0.1)
    assert select_face((passer_by, colleague, worker), SQUARE) is worker


def test_a_face_outside_the_roi_is_not_selected() -> None:
    passer_by = synthetic_face(center=Point(0.1, 0.5), width=0.3)
    assert select_face((passer_by,), SQUARE) is None


def test_roi_membership_uses_the_face_centre() -> None:
    # The box overlaps the ROI but its centre is outside.
    overlapping = synthetic_face(center=Point(0.15, 0.5), width=0.2)
    assert select_face((overlapping,), SQUARE) is None
    straddling = synthetic_face(center=Point(0.25, 0.5), width=0.2)
    assert select_face((straddling,), SQUARE) is straddling


# --- pose selection and posture --------------------------------------------------


def test_pose_selection_prefers_the_pose_under_the_selected_face() -> None:
    face = synthetic_face(center=Point(0.4, 0.4), width=0.15)
    own = synthetic_pose(center_x=0.4, nose_y=0.4, shoulder_width=0.2)
    other = synthetic_pose(center_x=0.7, nose_y=0.4, shoulder_width=0.4)
    assert select_pose((other, own), None, face) is own


def test_pose_selection_without_a_face_takes_the_widest_in_the_roi() -> None:
    outside = synthetic_pose(center_x=0.05, nose_y=0.5, shoulder_width=0.5)
    near = synthetic_pose(center_x=0.5, nose_y=0.4, shoulder_width=0.3)
    far = synthetic_pose(center_x=0.6, nose_y=0.4, shoulder_width=0.1)
    assert select_pose((outside, far, near), SQUARE, None) is near
    assert select_pose((outside,), SQUARE, None) is None
    assert select_pose((), None, None) is None


def test_posture_is_zero_upright_and_one_when_the_head_is_at_shoulder_level() -> None:
    upright = synthetic_pose(shoulder_width=0.3, head_height_ratio=0.6)
    assert posture_slump(upright, 1.0, upright_ratio=0.6, min_visibility=0.5) == pytest.approx(0.0)
    half = synthetic_pose(shoulder_width=0.3, head_height_ratio=0.3)
    assert posture_slump(half, 1.0, upright_ratio=0.6, min_visibility=0.5) == pytest.approx(0.5)
    slumped = synthetic_pose(shoulder_width=0.3, head_height_ratio=-0.1)
    assert posture_slump(slumped, 1.0, upright_ratio=0.6, min_visibility=0.5) == 1.0
    tall = synthetic_pose(shoulder_width=0.3, head_height_ratio=0.9)
    assert posture_slump(tall, 1.0, upright_ratio=0.6, min_visibility=0.5) == 0.0


def test_posture_respects_the_aspect_ratio() -> None:
    pose = synthetic_pose(shoulder_width=0.3, head_height_ratio=0.3, aspect=16 / 9)
    assert posture_slump(pose, 16 / 9, upright_ratio=0.6, min_visibility=0.5) == pytest.approx(0.5)


def test_posture_needs_visible_shoulders_and_head() -> None:
    hidden = synthetic_pose(visibility=0.2)
    assert posture_slump(hidden, 1.0, upright_ratio=0.6, min_visibility=0.5) is None
    p = PosePoint(0.5, 0.5, 1.0)
    collapsed = PoseObservation(nose=p, left_shoulder=p, right_shoulder=p)
    assert posture_slump(collapsed, 1.0, upright_ratio=0.6, min_visibility=0.5) is None


def test_box_properties() -> None:
    box = Box(0.2, 0.1, 0.6, 0.4)
    assert box.width == pytest.approx(0.4)
    assert box.height == pytest.approx(0.3)
    assert box.area == pytest.approx(0.12)
    assert box.center == pytest.approx(Point(0.4, 0.25))
