"""Mapping of MediaPipe-shaped results to observations, and the image statistics."""

import math
from datetime import UTC, datetime

import numpy as np
import pytest
from ag_workstation.capture import Frame, Image
from ag_workstation.landmarks import (
    Box,
    FaceObservation,
    HeadPose,
    Point,
    RawCategory,
    RawFaceResult,
    RawLandmark,
    RawPoseResult,
    raw_face,
    raw_pose,
    rotation_matrix,
    synthetic_face,
    synthetic_pose,
)
from ag_workstation.landmarks.image_stats import eye_region, highlight_fraction, region_stats
from ag_workstation.landmarks.mapping import (
    FACE_LANDMARK_COUNT,
    NOSE,
    RIGHT_EYE,
    build_result,
    head_pose_from_matrix,
    map_face,
    map_pose,
)


def _frame(image: Image, ts: float = 1.5, slot: int = 2) -> Frame:
    return Frame(
        camera_slot=slot, seq=1, ts=ts, captured_at=datetime(2026, 1, 1, tzinfo=UTC), image=image
    )


def _grey(height: int = 100, width: int = 100, value: int = 120) -> Image:
    return np.full((height, width, 3), value, dtype=np.uint8)


# --- face mapping -------------------------------------------------------------------


def test_map_face_keeps_only_the_points_the_signals_need() -> None:
    face = synthetic_face(center=Point(0.4, 0.6), width=0.3, ear=0.25, mar=0.5)
    mapped = map_face(raw_face(face), None, None)
    assert mapped is not None
    assert mapped.box == pytest.approx(face.box)
    assert mapped.right_eye == face.right_eye
    assert mapped.left_eye == face.left_eye
    assert mapped.mouth == face.mouth
    assert mapped.anchors == face.anchors
    assert mapped.head_pose is None
    assert mapped.blink_left is None
    assert mapped.blink_right is None
    assert mapped.confidence is None


def test_map_face_reads_blink_blendshapes_by_name() -> None:
    blendshapes = [
        RawCategory("_neutral", 0.0),
        RawCategory("eyeBlinkLeft", 0.7),
        RawCategory("eyeBlinkRight", 0.2),
    ]
    mapped = map_face(raw_face(synthetic_face()), blendshapes, None)
    assert mapped is not None
    assert mapped.blink_left == pytest.approx(0.7)
    assert mapped.blink_right == pytest.approx(0.2)
    partial = map_face(raw_face(synthetic_face()), [RawCategory("jawOpen", 0.9)], None)
    assert partial is not None
    assert partial.blink_left is None


def test_map_face_reads_the_head_pose_from_the_transform() -> None:
    mapped = map_face(raw_face(synthetic_face()), None, rotation_matrix(20.0, -10.0, 5.0))
    assert mapped is not None
    assert mapped.head_pose is not None
    assert mapped.head_pose.yaw_deg == pytest.approx(20.0)
    assert mapped.head_pose.pitch_deg == pytest.approx(-10.0)
    assert mapped.head_pose.roll_deg == pytest.approx(5.0)


def test_map_face_accepts_the_mesh_without_iris_points() -> None:
    assert FACE_LANDMARK_COUNT == 468
    assert map_face(raw_face(synthetic_face(), count=468), None, None) is not None


def test_map_face_refuses_short_or_broken_landmark_lists() -> None:
    assert map_face(raw_face(synthetic_face())[:100], None, None) is None
    assert map_face([], None, None) is None
    broken = raw_face(synthetic_face())
    broken[RIGHT_EYE[0]] = RawLandmark(math.nan, 0.5)
    assert map_face(broken, None, None) is None


def test_map_face_clamps_the_box_to_the_image() -> None:
    raw = raw_face(synthetic_face(center=Point(0.02, 0.5), width=0.2))
    mapped = map_face(raw, None, None)
    assert mapped is not None
    assert mapped.box.x_min == 0.0
    assert mapped.box.x_max == pytest.approx(0.12)


# --- head pose ----------------------------------------------------------------------


def test_identity_transform_is_a_level_head() -> None:
    assert head_pose_from_matrix(np.eye(4)) == HeadPose(0.0, 0.0, 0.0)


def test_pitch_is_negative_when_the_nose_points_down() -> None:
    s, c = math.sin(math.radians(30)), math.cos(math.radians(30))
    # Right-handed rotation about +x (x right, y up, z towards the camera):
    # the face normal (0, 0, 1) moves to (0, -s, c), i.e. downwards.
    nose_down = np.array([[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]], dtype=float)
    assert (nose_down @ np.array([0, 0, 1, 0]))[1] < 0
    pose = head_pose_from_matrix(nose_down)
    assert pose is not None
    assert pose.pitch_deg == pytest.approx(-30.0)
    assert pose.yaw_deg == pytest.approx(0.0)
    assert pose.roll_deg == pytest.approx(0.0)


def test_yaw_is_positive_when_the_nose_points_to_image_right() -> None:
    s, c = math.sin(math.radians(40)), math.cos(math.radians(40))
    turned = np.array([[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]], dtype=float)
    assert (turned @ np.array([0, 0, 1, 0]))[0] > 0
    pose = head_pose_from_matrix(turned)
    assert pose is not None
    assert pose.yaw_deg == pytest.approx(40.0)
    assert pose.pitch_deg == pytest.approx(0.0)


def test_roll_is_rotation_in_the_image_plane() -> None:
    s, c = math.sin(math.radians(15)), math.cos(math.radians(15))
    tilted = np.array([[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
    pose = head_pose_from_matrix(tilted)
    assert pose is not None
    assert pose.roll_deg == pytest.approx(15.0)
    assert pose.yaw_deg == pytest.approx(0.0)
    assert pose.pitch_deg == pytest.approx(0.0)


@pytest.mark.parametrize("yaw", [-60.0, -20.0, 0.0, 35.0])
@pytest.mark.parametrize("pitch", [-45.0, -10.0, 0.0, 25.0])
@pytest.mark.parametrize("roll", [-20.0, 0.0, 12.0])
def test_head_pose_round_trip(yaw: float, pitch: float, roll: float) -> None:
    pose = head_pose_from_matrix(rotation_matrix(yaw, pitch, roll))
    assert pose is not None
    assert pose.yaw_deg == pytest.approx(yaw, abs=1e-6)
    assert pose.pitch_deg == pytest.approx(pitch, abs=1e-6)
    assert pose.roll_deg == pytest.approx(roll, abs=1e-6)


def test_head_pose_ignores_scale_and_translation() -> None:
    matrix = rotation_matrix(25.0, -15.0, 4.0, scale=7.5, translation=(3.0, -2.0, -40.0))
    pose = head_pose_from_matrix(matrix)
    assert pose is not None
    assert pose.yaw_deg == pytest.approx(25.0)
    assert pose.pitch_deg == pytest.approx(-15.0)
    assert pose.roll_deg == pytest.approx(4.0)


def test_head_pose_accepts_nested_lists_and_3x3() -> None:
    nested = rotation_matrix(10.0, 5.0, 0.0).tolist()
    assert head_pose_from_matrix(nested) is not None
    assert head_pose_from_matrix(rotation_matrix(10.0, 5.0, 0.0)[:3, :3]) is not None


@pytest.mark.parametrize(
    "matrix",
    [
        np.zeros((4, 4)),
        np.eye(2),
        np.zeros(16),
        np.full((4, 4), np.nan),
        [[1, 0], [0, 1]],
    ],
)
def test_unusable_transforms_give_no_head_pose(matrix: object) -> None:
    assert head_pose_from_matrix(matrix) is None


# --- pose mapping -------------------------------------------------------------------


def test_map_pose_keeps_nose_and_shoulders() -> None:
    pose = synthetic_pose(center_x=0.4, nose_y=0.3, shoulder_width=0.25, visibility=0.8)
    assert map_pose(raw_pose(pose)) == pose


def test_map_pose_without_visibility_counts_as_visible() -> None:
    raw = [RawLandmark(0.5, 0.5) for _ in range(33)]
    mapped = map_pose(raw)
    assert mapped is not None
    assert mapped.nose.visibility == 1.0


def test_map_pose_refuses_short_or_broken_lists() -> None:
    assert map_pose([RawLandmark(0.5, 0.5)] * 5) is None
    broken = raw_pose(synthetic_pose())
    broken[NOSE] = RawLandmark(0.5, math.inf)
    assert map_pose(broken) is None


# --- image statistics ---------------------------------------------------------------


def test_region_stats_of_a_flat_image() -> None:
    stats = region_stats(_grey(value=51), None)
    assert stats is not None
    assert stats.brightness == pytest.approx(0.2)
    assert stats.sharpness == pytest.approx(0.0)


def test_brightness_weights_the_bgr_channels() -> None:
    blue = np.zeros((20, 20, 3), dtype=np.uint8)
    blue[:, :, 0] = 255
    stats = region_stats(blue, None)
    assert stats is not None
    assert stats.brightness == pytest.approx(0.114, abs=1e-3)


def test_sharpness_separates_detail_from_blur() -> None:
    rng = np.random.default_rng(7)
    detailed = rng.integers(0, 256, size=(80, 80, 3), dtype=np.uint8)
    smooth = np.repeat(np.linspace(0, 255, 80, dtype=np.uint8)[None, :, None], 80, axis=0)
    smooth = np.repeat(smooth, 3, axis=2)
    sharp = region_stats(detailed, None)
    blurred = region_stats(smooth, None)
    assert sharp is not None
    assert blurred is not None
    assert sharp.sharpness > 1000
    assert blurred.sharpness < 5


def test_region_stats_of_a_box() -> None:
    image = _grey(value=0)
    image[50:, :] = 255
    top = region_stats(image, Box(0.0, 0.0, 1.0, 0.4))
    bottom = region_stats(image, Box(0.0, 0.6, 1.0, 1.0))
    assert top is not None
    assert bottom is not None
    assert top.brightness == pytest.approx(0.0)
    assert bottom.brightness == pytest.approx(1.0)


def test_region_stats_of_an_empty_or_tiny_region_is_none() -> None:
    assert region_stats(_grey(), Box(0.5, 0.5, 0.5, 0.5)) is None
    assert region_stats(_grey(), Box(0.5, 0.5, 0.51, 0.51)) is None
    assert region_stats(np.zeros((0, 0, 3), dtype=np.uint8), None) is None


def test_region_stats_subsamples_large_frames() -> None:
    stats = region_stats(_grey(1080, 1920, value=255), None)
    assert stats is not None
    assert stats.brightness == pytest.approx(1.0)


def test_highlight_fraction_counts_near_white_pixels() -> None:
    image = _grey(value=120)
    image[:, :50] = 255
    assert highlight_fraction(image, Box(0.0, 0.0, 1.0, 1.0)) == pytest.approx(0.5)
    assert highlight_fraction(image, Box(0.0, 0.0, 0.4, 1.0)) == pytest.approx(1.0)
    assert highlight_fraction(image, Box(0.6, 0.0, 1.0, 1.0)) == pytest.approx(0.0)
    assert highlight_fraction(image, Box(0.5, 0.5, 0.5, 0.5)) is None


def test_a_saturated_colour_is_not_a_specular_highlight() -> None:
    red = np.zeros((20, 20, 3), dtype=np.uint8)
    red[:, :, 2] = 255
    assert highlight_fraction(red, Box(0.0, 0.0, 1.0, 1.0)) == 0.0


def test_eye_region_surrounds_the_eye() -> None:
    face = synthetic_face(center=Point(0.5, 0.5), width=0.4)
    region = eye_region(face.right_eye, 1.0)
    xs = [p.x for p in face.right_eye]
    ys = [p.y for p in face.right_eye]
    assert region.x_min < min(xs) < max(xs) < region.x_max
    assert region.y_min < min(ys) < max(ys) < region.y_max
    assert region.width < 2 * (max(xs) - min(xs))
    clamped = eye_region(synthetic_face(center=Point(0.0, 0.0), width=0.4).right_eye, 1.0)
    assert clamped.x_min == 0.0
    assert clamped.y_min == 0.0


# --- full result --------------------------------------------------------------------


def test_build_result_maps_faces_and_measures_the_image() -> None:
    face = synthetic_face(center=Point(0.5, 0.5), width=0.4)
    image = _grey(100, 200, value=120)
    result = build_result(
        _frame(image),
        RawFaceResult(
            face_landmarks=[raw_face(face)],
            face_blendshapes=[
                [RawCategory("eyeBlinkLeft", 0.1), RawCategory("eyeBlinkRight", 0.3)]
            ],
            facial_transformation_matrixes=[rotation_matrix(12.0, -8.0, 0.0)],
        ),
        None,
    )
    assert result.camera_slot == 2
    assert result.ts == 1.5
    assert (result.width, result.height) == (200, 100)
    assert result.aspect == pytest.approx(2.0)
    assert result.poses is None
    assert result.frame_stats is not None
    assert result.frame_stats.brightness == pytest.approx(120 / 255)
    (mapped,) = result.faces
    assert mapped.right_eye == face.right_eye
    assert mapped.blink_right == pytest.approx(0.3)
    assert mapped.head_pose is not None
    assert mapped.head_pose.yaw_deg == pytest.approx(12.0)
    assert mapped.stats is not None
    assert mapped.stats.brightness == pytest.approx(120 / 255)
    assert mapped.glare_right == 0.0
    assert mapped.glare_left == 0.0


def test_build_result_measures_glare_per_eye() -> None:
    face = synthetic_face(center=Point(0.5, 0.5), width=0.4)
    image = _grey(100, 100, value=120)
    image[30:55, 30:50] = 255  # a reflection over the right eye (image left)
    result = build_result(_frame(image), RawFaceResult([raw_face(face)]), None)
    (mapped,) = result.faces
    assert mapped.glare_right is not None
    assert mapped.glare_right > 0.9
    assert mapped.glare_left == 0.0


def test_build_result_tolerates_missing_blendshapes_and_transforms() -> None:
    faces = [raw_face(synthetic_face(center=Point(0.3, 0.5))), raw_face(synthetic_face())]
    result = build_result(
        _frame(_grey()),
        RawFaceResult(faces, face_blendshapes=[[RawCategory("eyeBlinkLeft", 0.5)]]),
        None,
    )
    first, second = result.faces
    assert first.blink_left == pytest.approx(0.5)
    assert second.blink_left is None
    assert first.head_pose is None


def test_build_result_drops_unusable_faces_and_poses() -> None:
    result = build_result(
        _frame(_grey()),
        RawFaceResult([raw_face(synthetic_face())[:50], raw_face(synthetic_face())]),
        RawPoseResult([[RawLandmark(0.5, 0.5)], raw_pose(synthetic_pose())]),
    )
    assert len(result.faces) == 1
    assert result.poses is not None
    assert len(result.poses) == 1


def test_build_result_with_no_detections() -> None:
    result = build_result(_frame(_grey()), RawFaceResult([]), RawPoseResult([]))
    assert result.faces == ()
    assert result.poses == ()
    assert result.frame_stats is not None


def test_observations_hold_no_mesh_or_embedding() -> None:
    result = build_result(_frame(_grey()), RawFaceResult([raw_face(synthetic_face())]), None)
    (mapped,) = result.faces
    points = 6 + 6 + 8 + len(mapped.anchors)
    assert points < 30  # of the 478 mesh points; nothing that identifies a person
    assert set(FaceObservation.__dataclass_fields__) == {
        "box",
        "right_eye",
        "left_eye",
        "mouth",
        "anchors",
        "head_pose",
        "blink_right",
        "blink_left",
        "confidence",
        "stats",
        "glare_right",
        "glare_left",
    }
