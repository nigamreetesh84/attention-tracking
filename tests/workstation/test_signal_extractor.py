"""`SignalExtractor`: one `LandmarkResult` in, one `SignalFrame` out."""

from pathlib import Path

import ag_workstation.signals
import pytest
from ag_shared.config import Calibration, CameraConfig, CameraKind, CameraRole, RoiPoint
from ag_shared.ppe import PpeProfile, PpeProfileKind, SignalSetting, builtin_profile
from ag_shared.signals import (
    BODY_SIGNALS,
    EYE_SIGNALS,
    HEAD_POSE_SIGNALS,
    YAWN_SIGNALS,
    SignalFrame,
    SignalName,
)
from ag_shared.thresholds import Thresholds
from ag_workstation.landmarks import (
    FaceObservation,
    HeadPose,
    LandmarkResult,
    Point,
    PoseObservation,
    RegionStats,
    synthetic_face,
    synthetic_pose,
    synthetic_result,
)
from ag_workstation.landmarks.fake import GOOD_STATS
from ag_workstation.signals import ExtractionParams, SignalExtractor

S = SignalName
CAMERA = CameraConfig(slot=1, role=CameraRole.PRIMARY, kind=CameraKind.USB, uri="0")
ROI = tuple(RoiPoint(x=x, y=y) for x, y in ((0.3, 0.1), (0.9, 0.1), (0.9, 0.9), (0.3, 0.9)))
DARK = RegionStats(brightness=0.02, sharpness=100.0)


def _extractor(
    kind: PpeProfileKind = PpeProfileKind.OPEN_FACE,
    *,
    camera: CameraConfig = CAMERA,
    thresholds: Thresholds | None = None,
    params: ExtractionParams | None = None,
) -> SignalExtractor:
    return SignalExtractor(camera, builtin_profile(kind), thresholds or Thresholds(), params)


def _t(i: int) -> float:
    return i / 10


def _res(
    ts: float,
    *faces: FaceObservation,
    poses: tuple[PoseObservation, ...] | None = None,
    frame_stats: RegionStats | None = GOOD_STATS,
) -> LandmarkResult:
    return synthetic_result(ts, faces, poses=poses, frame_stats=frame_stats)


def _val(frame: SignalFrame, name: SignalName) -> float:
    value = frame.value(name)
    assert value is not None, f"{name} is missing or invalid"
    return value


def _is_invalid(frame: SignalFrame, name: SignalName) -> bool:
    reading = frame.reading(name)
    assert reading is not None, f"{name} was not computed"
    return not reading.valid


def _warm(extractor: SignalExtractor, frames: int = 60, ear: float = 0.30) -> SignalFrame:
    """Feed `frames` frames (10 fps from ts 0) of one steady face."""
    frame = None
    for i in range(frames):
        frame = extractor.extract(_res(_t(i), synthetic_face(ear=ear)))
    assert frame is not None
    return frame


# --- one attentive frame ----------------------------------------------------------


def test_an_open_attentive_face() -> None:
    frame = _extractor().extract(_res(0.0, synthetic_face()))
    assert frame.camera_slot == 1
    assert frame.ts == 0.0
    assert frame.flag(S.FACE_PRESENT) is True
    assert _val(frame, S.QUALITY) == pytest.approx(1.0)
    assert _val(frame, S.EAR) == pytest.approx(1.0)
    assert frame.flag(S.EYES_CLOSED) is False
    assert _val(frame, S.MAR) == pytest.approx(0.05)
    assert frame.flag(S.YAWN) is False
    assert _val(frame, S.HEAD_YAW) == 0.0
    assert _val(frame, S.HEAD_PITCH) == 0.0
    assert _val(frame, S.HEAD_ROLL) == 0.0
    assert frame.flag(S.HEAD_NOD) is False
    assert frame.flag(S.HEAD_DROPPED) is False
    # Windows are not filled yet.
    assert _is_invalid(frame, S.PERCLOS)
    assert _is_invalid(frame, S.BLINK_RATE)


@pytest.mark.parametrize("kind", list(PpeProfileKind))
def test_only_the_signals_enabled_by_the_profile_are_computed(kind: PpeProfileKind) -> None:
    extractor = _extractor(kind)
    frame = extractor.extract(_res(0.0, synthetic_face(), poses=(synthetic_pose(),)))
    assert set(frame.signals) == builtin_profile(kind).enabled_signals
    empty = extractor.extract(_res(0.1))
    assert set(empty.signals) == builtin_profile(kind).enabled_signals


def test_a_custom_profile_can_drop_single_derived_signals() -> None:
    signals = {name: SignalSetting(enabled=name not in BODY_SIGNALS) for name in SignalName}
    signals[S.PERCLOS] = SignalSetting(enabled=False)
    signals[S.HEAD_NOD] = SignalSetting(enabled=False)
    profile = PpeProfile(kind=PpeProfileKind.CUSTOM, signals=signals)
    frame = SignalExtractor(CAMERA, profile, Thresholds()).extract(_res(0.0, synthetic_face()))
    assert S.PERCLOS not in frame.signals
    assert S.HEAD_NOD not in frame.signals
    assert S.BLINK_RATE in frame.signals
    assert S.HEAD_DROPPED in frame.signals


def test_a_result_for_another_camera_is_refused() -> None:
    with pytest.raises(ValueError, match="camera"):
        _extractor().extract(synthetic_result(0.0, (), camera_slot=2))


def test_wide_frames_measure_the_same_ratios() -> None:
    face = synthetic_face(ear=0.3, mar=0.4, aspect=16 / 9)
    result = synthetic_result(0.0, (face,), width=1280, height=720)
    frame = _extractor().extract(result)
    assert _val(frame, S.EAR) == pytest.approx(1.0)
    assert _val(frame, S.MAR) == pytest.approx(0.4)


# --- presence and quality ---------------------------------------------------------


def test_no_face_in_a_good_frame() -> None:
    frame = _extractor().extract(_res(0.0))
    assert frame.flag(S.FACE_PRESENT) is False
    assert _val(frame, S.QUALITY) == pytest.approx(1.0)  # the frame itself is fine
    for name in EYE_SIGNALS | YAWN_SIGNALS | HEAD_POSE_SIGNALS:
        assert _is_invalid(frame, name)


def test_no_face_in_a_dark_frame_is_low_quality() -> None:
    frame = _extractor().extract(_res(0.0, frame_stats=DARK))
    assert frame.flag(S.FACE_PRESENT) is False
    assert _val(frame, S.QUALITY) < Thresholds().quality_min


def test_overexposed_and_blurred_frames_are_low_quality() -> None:
    extractor = _extractor()
    washed_out = extractor.extract(_res(0.0, frame_stats=RegionStats(0.99, 100.0)))
    blurred = extractor.extract(_res(0.1, frame_stats=RegionStats(0.5, 1.0)))
    assert _val(washed_out, S.QUALITY) < 0.5
    assert _val(blurred, S.QUALITY) < 0.5


def test_quality_is_invalid_when_nothing_can_be_measured() -> None:
    frame = _extractor().extract(_res(0.0, frame_stats=None))
    assert frame.flag(S.FACE_PRESENT) is False
    assert _is_invalid(frame, S.QUALITY)


def test_face_quality_uses_the_face_region_before_the_frame() -> None:
    lit_face = synthetic_face(stats=RegionStats(0.5, 100.0))
    frame = _extractor().extract(_res(0.0, lit_face, frame_stats=DARK))
    assert _val(frame, S.QUALITY) == pytest.approx(1.0)
    no_stats = synthetic_face(stats=None)
    fallback = _extractor().extract(_res(0.0, no_stats, frame_stats=DARK))
    assert _val(fallback, S.QUALITY) < 0.5
    bare = _extractor().extract(_res(0.0, no_stats, frame_stats=None))
    assert _val(bare, S.QUALITY) == pytest.approx(1.0)  # size is all there is


@pytest.mark.parametrize(
    "face",
    [
        synthetic_face(width=0.02),  # too small
        synthetic_face(stats=DARK),
        synthetic_face(confidence=0.2),
    ],
)
def test_an_unusable_face_is_present_but_not_measured(face: FaceObservation) -> None:
    frame = _extractor().extract(_res(0.0, face))
    assert frame.flag(S.FACE_PRESENT) is True
    assert _val(frame, S.QUALITY) < Thresholds().quality_min
    for name in EYE_SIGNALS | YAWN_SIGNALS | HEAD_POSE_SIGNALS:
        assert _is_invalid(frame, name)


def test_quality_min_comes_from_the_thresholds() -> None:
    face = synthetic_face(confidence=0.6)
    strict = _extractor(thresholds=Thresholds(quality_min=0.7)).extract(_res(0.0, face))
    lenient = _extractor(thresholds=Thresholds(quality_min=0.5)).extract(_res(0.0, face))
    assert _is_invalid(strict, S.EAR)
    assert not _is_invalid(lenient, S.EAR)
    assert _val(lenient, S.QUALITY) == pytest.approx(0.6)


def test_unstable_detection_lowers_quality() -> None:
    steady = _extractor()
    jumpy = _extractor()
    steady_frame = jumpy_frame = None
    for i in range(10):
        steady_frame = steady.extract(_res(_t(i), synthetic_face(width=0.2)))
        jumpy_frame = jumpy.extract(_res(_t(i), synthetic_face(width=0.12 if i % 2 else 0.3)))
    assert steady_frame is not None
    assert jumpy_frame is not None
    assert _val(steady_frame, S.QUALITY) == pytest.approx(1.0)
    assert _val(jumpy_frame, S.QUALITY) < 0.5


def test_stability_is_not_judged_across_a_long_gap() -> None:
    extractor = _extractor()
    extractor.extract(_res(0.0, synthetic_face(width=0.12)))
    frame = extractor.extract(_res(5.0, synthetic_face(width=0.3)))
    assert _val(frame, S.QUALITY) == pytest.approx(1.0)


# --- ROI ---------------------------------------------------------------------------


def test_the_largest_face_inside_the_roi_supplies_the_signals() -> None:
    camera = CAMERA.model_copy(update={"roi": ROI})
    passer_by = synthetic_face(center=Point(0.15, 0.5), width=0.25, head_pose=HeadPose(50, 0, 0))
    worker = synthetic_face(center=Point(0.6, 0.5), width=0.15, head_pose=HeadPose(5, 0, 0))
    frame = _extractor(camera=camera).extract(_res(0.0, passer_by, worker))
    assert frame.flag(S.FACE_PRESENT) is True
    assert _val(frame, S.HEAD_YAW) == pytest.approx(5.0)


def test_a_face_outside_the_roi_is_no_face() -> None:
    camera = CAMERA.model_copy(update={"roi": ROI})
    passer_by = synthetic_face(center=Point(0.15, 0.5), width=0.25)
    frame = _extractor(camera=camera).extract(_res(0.0, passer_by))
    assert frame.flag(S.FACE_PRESENT) is False
    assert _is_invalid(frame, S.EAR)
    # Without an ROI the same face counts.
    assert _extractor().extract(_res(0.0, passer_by)).flag(S.FACE_PRESENT) is True


# --- eyes ---------------------------------------------------------------------------


def test_closed_eyes_after_an_open_baseline() -> None:
    extractor = _extractor()
    _warm(extractor, ear=0.30)
    frame = extractor.extract(_res(6.0, synthetic_face(ear=0.09)))
    assert _val(frame, S.EAR) == pytest.approx(0.3)
    assert frame.flag(S.EYES_CLOSED) is True


def test_ear_just_above_and_below_the_closed_ratio() -> None:
    ratio = ExtractionParams().ear_closed_ratio
    for ear, closed in ((0.30 * (ratio + 0.02), False), (0.30 * (ratio - 0.02), True)):
        extractor = _extractor()
        _warm(extractor, ear=0.30)
        frame = extractor.extract(_res(6.0, synthetic_face(ear=ear)))
        assert frame.flag(S.EYES_CLOSED) is closed


def test_blink_blendshape_can_close_the_eyes() -> None:
    blinking = synthetic_face(ear=0.30, blink=0.9)
    assert _extractor().extract(_res(0.0, blinking)).flag(S.EYES_CLOSED) is True
    relaxed = synthetic_face(ear=0.30, blink=0.2)
    assert _extractor().extract(_res(0.0, relaxed)).flag(S.EYES_CLOSED) is False
    ear_only = _extractor(params=ExtractionParams(use_blink_blendshape=False))
    assert ear_only.extract(_res(0.0, blinking)).flag(S.EYES_CLOSED) is False


def test_perclos_over_the_configured_window() -> None:
    extractor = _extractor(thresholds=Thresholds(perclos_window_s=20.0))
    _warm(extractor, ear=0.30)
    frame = None
    for i in range(60, 400):
        ear = 0.09 if i % 10 < 4 else 0.30
        frame = extractor.extract(_res(_t(i), synthetic_face(ear=ear)))
    assert frame is not None
    assert _val(frame, S.PERCLOS) == pytest.approx(0.4, abs=0.01)


def test_blink_rate_through_the_extractor() -> None:
    extractor = _extractor()
    frame = None
    for i in range(601):
        ear = 0.09 if i >= 100 and i % 50 == 0 else 0.30
        frame = extractor.extract(_res(_t(i), synthetic_face(ear=ear)))
    assert frame is not None
    assert _val(frame, S.BLINK_RATE) == pytest.approx(10.0)


def test_degenerate_eye_landmarks_are_invalid() -> None:
    p = Point(0.5, 0.5)
    face = synthetic_face()
    collapsed = FaceObservation(
        box=face.box,
        right_eye=(p, p, p, p, p, p),
        left_eye=(p, p, p, p, p, p),
        mouth=face.mouth,
        anchors=face.anchors,
        head_pose=face.head_pose,
    )
    frame = _extractor().extract(_res(0.0, collapsed))
    assert _is_invalid(frame, S.EAR)
    assert _is_invalid(frame, S.EYES_CLOSED)
    assert not _is_invalid(frame, S.MAR)


# --- glare --------------------------------------------------------------------------


def test_glare_on_both_eyes_invalidates_eye_signals_for_clear_glasses() -> None:
    extractor = _extractor(PpeProfileKind.CLEAR_GLASSES)
    frame = extractor.extract(_res(0.0, synthetic_face(ear=0.09, glare=(0.3, 0.4))))
    assert _is_invalid(frame, S.EAR)
    assert _is_invalid(frame, S.EYES_CLOSED)
    # Head pose still works, so the estimator can fall back to it.
    assert _val(frame, S.HEAD_PITCH) == 0.0


def test_glare_is_ignored_by_profiles_without_glare_tolerance() -> None:
    extractor = _extractor(PpeProfileKind.OPEN_FACE)
    frame = extractor.extract(_res(0.0, synthetic_face(glare=(0.3, 0.4))))
    assert not _is_invalid(frame, S.EAR)
    assert frame.flag(S.EYES_CLOSED) is False


def test_glare_below_the_limit_is_tolerated() -> None:
    extractor = _extractor(PpeProfileKind.CLEAR_GLASSES)
    frame = extractor.extract(_res(0.0, synthetic_face(glare=(0.01, 0.02))))
    assert frame.flag(S.EYES_CLOSED) is False


def test_with_glare_on_one_eye_the_other_eye_is_used() -> None:
    extractor = _extractor(PpeProfileKind.CLEAR_GLASSES)
    _warm(extractor, ear=0.30)
    # Right eye (glared) looks open, left eye is closed.
    face = synthetic_face(ear=0.30, left_ear=0.09, glare=(0.5, 0.0))
    frame = extractor.extract(_res(6.0, face))
    assert _val(frame, S.EAR) == pytest.approx(0.3)
    assert frame.flag(S.EYES_CLOSED) is True


def test_glare_frames_count_as_invalid_in_perclos() -> None:
    extractor = _extractor(PpeProfileKind.CLEAR_GLASSES)
    some = most = None
    for i in range(301):  # 30% glare frames: PERCLOS over the rest
        glare = (0.4, 0.4) if i % 10 < 3 else None
        ear = 0.09 if i % 10 == 5 and i > 60 else 0.30
        some = extractor.extract(_res(_t(i), synthetic_face(ear=ear, glare=glare)))
    for i in range(301, 700):  # then 70% glare frames
        glare = (0.4, 0.4) if i % 10 < 7 else None
        most = extractor.extract(_res(_t(i), synthetic_face(glare=glare)))
    assert some is not None
    assert most is not None
    assert 0.0 < _val(some, S.PERCLOS) < 0.2
    assert _is_invalid(most, S.PERCLOS)


# --- mouth --------------------------------------------------------------------------


def test_a_long_open_mouth_is_a_yawn() -> None:
    extractor = _extractor()
    frames = [extractor.extract(_res(_t(i), synthetic_face(mar=0.8))) for i in range(17)]
    assert _val(frames[0], S.MAR) == pytest.approx(0.8)
    assert frames[15].flag(S.YAWN) is False  # 1.5 s
    assert frames[16].flag(S.YAWN) is True  # more than 1.5 s
    closed = extractor.extract(_res(1.7, synthetic_face(mar=0.05)))
    assert closed.flag(S.YAWN) is False


def test_talking_is_not_a_yawn() -> None:
    extractor = _extractor()
    for i in range(100):
        frame = extractor.extract(_res(_t(i), synthetic_face(mar=0.3)))
        assert frame.flag(S.YAWN) is False


def test_yawn_is_invalid_while_the_mouth_cannot_be_measured() -> None:
    extractor = _extractor()
    extractor.extract(_res(0.0, synthetic_face(mar=0.8)))
    frame = extractor.extract(_res(0.1))
    assert _is_invalid(frame, S.MAR)
    assert _is_invalid(frame, S.YAWN)


@pytest.mark.parametrize(
    "kind", [PpeProfileKind.MASK, PpeProfileKind.SUNGLASSES, PpeProfileKind.HELMET_SHIELD]
)
def test_yawn_is_not_computed_for_covered_mouth_profiles(kind: PpeProfileKind) -> None:
    frame = _extractor(kind).extract(_res(0.0, synthetic_face(mar=0.9)))
    assert S.MAR not in frame.signals
    assert S.YAWN not in frame.signals


# --- head pose ----------------------------------------------------------------------


def test_head_pose_is_relative_to_the_calibrated_neutral() -> None:
    calibration = Calibration(neutral_yaw_deg=10.0, neutral_pitch_deg=-5.0, neutral_roll_deg=2.0)
    camera = CAMERA.model_copy(update={"calibration": calibration})
    face = synthetic_face(head_pose=HeadPose(40.0, -35.0, 5.0))
    frame = _extractor(camera=camera).extract(_res(0.0, face))
    assert _val(frame, S.HEAD_YAW) == pytest.approx(30.0)
    assert _val(frame, S.HEAD_PITCH) == pytest.approx(-30.0)
    assert _val(frame, S.HEAD_ROLL) == pytest.approx(3.0)
    assert frame.flag(S.HEAD_DROPPED) is True


def test_head_dropped_boundary() -> None:
    drop = ExtractionParams().head_drop_deg
    at = _extractor().extract(_res(0.0, synthetic_face(head_pose=HeadPose(0, -drop, 0))))
    below = _extractor().extract(_res(0.0, synthetic_face(head_pose=HeadPose(0, -drop - 0.1, 0))))
    up = _extractor().extract(_res(0.0, synthetic_face(head_pose=HeadPose(0, drop + 10, 0))))
    assert at.flag(S.HEAD_DROPPED) is False
    assert below.flag(S.HEAD_DROPPED) is True
    assert up.flag(S.HEAD_DROPPED) is False


def test_missing_head_pose_invalidates_the_head_signals() -> None:
    frame = _extractor().extract(_res(0.0, synthetic_face(head_pose=None)))
    for name in HEAD_POSE_SIGNALS:
        assert _is_invalid(frame, name)
    assert not _is_invalid(frame, S.EAR)


def test_a_nod_is_flagged_on_the_recovery_frame() -> None:
    extractor = _extractor()
    pitches = [0.0] * 20 + [-20.0] * 8 + [0.0] * 20
    frames = [
        extractor.extract(_res(_t(i), synthetic_face(head_pose=HeadPose(0, pitch, 0))))
        for i, pitch in enumerate(pitches)
    ]
    nods = [i for i, frame in enumerate(frames) if frame.flag(S.HEAD_NOD)]
    assert nods == [28]


# --- posture and motion -------------------------------------------------------------


def test_posture_from_the_pose_model() -> None:
    extractor = _extractor(PpeProfileKind.SUNGLASSES)
    upright = synthetic_pose(head_height_ratio=ExtractionParams().posture_upright_ratio)
    slumped = synthetic_pose(head_height_ratio=0.0)
    first = extractor.extract(_res(0.0, synthetic_face(), poses=(upright,)))
    second = extractor.extract(_res(0.1, synthetic_face(), poses=(slumped,)))
    assert _val(first, S.POSTURE) == pytest.approx(0.0, abs=1e-9)
    assert _val(second, S.POSTURE) == 1.0


def test_posture_is_invalid_without_a_usable_pose() -> None:
    extractor = _extractor(PpeProfileKind.SUNGLASSES)
    not_run = extractor.extract(_res(0.0, synthetic_face(), poses=None))
    nobody = extractor.extract(_res(0.1, synthetic_face(), poses=()))
    hidden = extractor.extract(_res(0.2, synthetic_face(), poses=(synthetic_pose(visibility=0.1),)))
    for frame in (not_run, nobody, hidden):
        assert _is_invalid(frame, S.POSTURE)


def test_motion_is_zero_when_still_and_positive_when_moving() -> None:
    still = _extractor(PpeProfileKind.SUNGLASSES)
    moving = _extractor(PpeProfileKind.SUNGLASSES)
    still_frame = moving_frame = None
    for i in range(30):
        still_frame = still.extract(_res(_t(i), synthetic_face(), poses=(synthetic_pose(),)))
        x = 0.4 + 0.005 * i
        moving_frame = moving.extract(
            _res(
                _t(i),
                synthetic_face(center=Point(x, 0.5)),
                poses=(synthetic_pose(center_x=x),),
            )
        )
    assert still_frame is not None
    assert moving_frame is not None
    assert _val(still_frame, S.MOTION) == 0.0
    assert _val(moving_frame, S.MOTION) > 0.1
    assert not _extractor(PpeProfileKind.SUNGLASSES).extract(_res(0.0)).signals[S.MOTION].valid


def test_body_signals_work_without_a_face() -> None:
    # Head down on the bench: no face is found, the pose model still sees the body.
    extractor = _extractor(PpeProfileKind.HELMET_SHIELD)
    frame = None
    for i in range(30):
        frame = extractor.extract(_res(_t(i), poses=(synthetic_pose(head_height_ratio=0.0),)))
    assert frame is not None
    assert frame.flag(S.FACE_PRESENT) is False
    assert _val(frame, S.POSTURE) == 1.0
    assert _val(frame, S.MOTION) == 0.0


def test_body_signals_are_invalid_in_an_unusable_frame() -> None:
    extractor = _extractor(PpeProfileKind.SUNGLASSES)
    frame = None
    for i in range(30):
        frame = extractor.extract(_res(_t(i), poses=(synthetic_pose(),), frame_stats=DARK))
    assert frame is not None
    assert _is_invalid(frame, S.POSTURE)
    assert _is_invalid(frame, S.MOTION)


def test_the_pose_inside_the_roi_is_used() -> None:
    camera = CAMERA.model_copy(update={"roi": ROI})
    extractor = _extractor(PpeProfileKind.SUNGLASSES, camera=camera)
    outside = synthetic_pose(center_x=0.1, shoulder_width=0.4, head_height_ratio=0.0)
    inside = synthetic_pose(center_x=0.6, shoulder_width=0.2, head_height_ratio=0.6)
    frame = extractor.extract(_res(0.0, poses=(outside, inside)))
    assert _val(frame, S.POSTURE) == pytest.approx(0.0, abs=1e-9)
    assert _is_invalid(extractor.extract(_res(0.1, poses=(outside,))), S.POSTURE)


# --- lifecycle ----------------------------------------------------------------------


def _fill_perclos(extractor: SignalExtractor) -> SignalFrame:
    frame = None
    for i in range(200):
        frame = extractor.extract(_res(_t(i), synthetic_face()))
    assert frame is not None
    return frame


def test_time_going_backwards_restarts_the_windows() -> None:
    extractor = _extractor()
    assert not _is_invalid(_fill_perclos(extractor), S.PERCLOS)
    restarted = extractor.extract(_res(0.0, synthetic_face()))
    assert _is_invalid(restarted, S.PERCLOS)
    assert _is_invalid(restarted, S.BLINK_RATE)


def test_reset_session_restarts_windows_and_the_eye_baseline() -> None:
    extractor = _extractor()
    _fill_perclos(extractor)  # baseline 0.30
    extractor.reset_session()
    # A new worker with narrower eyes is not read against the old baseline.
    frame = extractor.extract(_res(20.0, synthetic_face(ear=0.19)))
    assert _val(frame, S.EAR) == pytest.approx(1.0)
    assert frame.flag(S.EYES_CLOSED) is False
    assert _is_invalid(frame, S.PERCLOS)


def test_without_a_reset_the_baseline_carries_over() -> None:
    extractor = _extractor()
    _fill_perclos(extractor)
    frame = extractor.extract(_res(20.0, synthetic_face(ear=0.19)))
    assert frame.flag(S.EYES_CLOSED) is True


def test_configure_changes_profile_and_camera_but_keeps_the_windows() -> None:
    extractor = _extractor()
    _fill_perclos(extractor)
    calibration = Calibration(neutral_yaw_deg=10.0)
    extractor.configure(
        camera=CAMERA.model_copy(update={"calibration": calibration}),
        profile=builtin_profile(PpeProfileKind.MASK),
        thresholds=Thresholds(quality_min=0.4),
    )
    frame = extractor.extract(_res(20.0, synthetic_face()))
    assert S.YAWN not in frame.signals
    assert _val(frame, S.HEAD_YAW) == pytest.approx(-10.0)
    assert not _is_invalid(frame, S.PERCLOS)
    with pytest.raises(ValueError, match="slot"):
        extractor.configure(camera=CAMERA.model_copy(update={"slot": 2}))


# --- privacy and layering -----------------------------------------------------------


def test_a_signal_frame_holds_only_named_numbers() -> None:
    frame = _extractor(PpeProfileKind.HELMET_SHIELD).extract(
        _res(0.0, synthetic_face(), poses=(synthetic_pose(),))
    )
    dumped = frame.model_dump()
    assert set(dumped) == {"camera_slot", "ts", "signals"}
    assert all(set(reading) == {"value", "valid"} for reading in dumped["signals"].values())
    assert set(dumped["signals"]) <= set(SignalName)


def test_the_signal_layer_imports_no_vision_library() -> None:
    package = Path(ag_workstation.signals.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for library in ("mediapipe", "cv2", "numpy"):
            assert f"import {library}" not in text, f"{source.name} imports {library}"
            assert f"from {library}" not in text, f"{source.name} imports {library}"
