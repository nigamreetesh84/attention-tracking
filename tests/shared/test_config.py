"""Camera, capture, shift and engine config models (detection-engine.md 7, 8)."""

from datetime import time
from typing import Any
from uuid import UUID

import pytest
from ag_shared.config import (
    Calibration,
    CameraConfig,
    CameraKind,
    CameraRole,
    CaptureConfig,
    EngineConfig,
    RoiPoint,
    Shift,
    ShiftBreak,
    ShiftThresholds,
)
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState
from ag_shared.thresholds import Thresholds
from pydantic import ValidationError

SHIFT_A = UUID("00000000-0000-7000-8000-00000000000a")
SHIFT_B = UUID("00000000-0000-7000-8000-00000000000b")

# Any: these are raw JSON-like payloads, deliberately untyped so tests can
# break them in ways the typed constructors would not allow.
Json = dict[str, Any]


def _camera(slot: int = 1, role: str = "primary", **extra: object) -> Json:
    return {"slot": slot, "role": role, "kind": "usb", "uri": f"usb:{slot - 1}", **extra}


def _shift(shift_id: UUID = SHIFT_A, **extra: object) -> Json:
    return {
        "id": str(shift_id),
        "name": "Morning",
        "start_time": "06:00:00",
        "end_time": "14:00:00",
        "days_of_week": [0, 1, 2, 3, 4],
        "breaks": [{"name": "Tea", "start_time": "10:00:00", "end_time": "10:15:00"}],
        **extra,
    }


def _engine(**extra: object) -> Json:
    return {
        "cameras": [_camera(1, "primary"), _camera(2, "secondary")],
        "profile": builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json"),
        **extra,
    }


# --- camera -----------------------------------------------------------------


def test_camera_enums_match_the_schema() -> None:
    assert {r.value for r in CameraRole} == {"primary", "secondary"}
    assert {k.value for k in CameraKind} == {"usb", "rtsp", "onvif", "builtin"}


def test_camera_defaults() -> None:
    cam = CameraConfig.model_validate(_camera())
    assert cam.roi is None  # whole frame
    assert cam.calibration == Calibration()
    assert cam.calibration.neutral_yaw_deg == 0.0
    assert cam.is_enabled is True


@pytest.mark.parametrize("slot", [0, 5, -1, 1.5, "one"])
def test_camera_slot_must_be_1_to_4(slot: object) -> None:
    with pytest.raises(ValidationError):
        CameraConfig.model_validate(_camera() | {"slot": slot})


@pytest.mark.parametrize(
    "change",
    [{"role": "tertiary"}, {"kind": "thermal"}, {"uri": ""}, {"uri": None}, {"fps": 15}],
)
def test_camera_invalid_values_are_rejected(change: Json) -> None:
    with pytest.raises(ValidationError):
        CameraConfig.model_validate(_camera() | change)


def test_camera_uri_is_kept_out_of_repr() -> None:
    """URIs may hold credentials and repr() ends up in logs and tracebacks."""
    cam = CameraConfig.model_validate(_camera(kind="rtsp", uri="rtsp://admin:hunter2@10.0.0.9/1"))
    assert "hunter2" not in repr(cam)
    assert "hunter2" not in str(cam)
    assert cam.uri.endswith("@10.0.0.9/1")


def test_roi_is_a_normalized_polygon() -> None:
    cam = CameraConfig.model_validate(
        _camera(roi=[{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 1.0}])
    )
    assert cam.roi is not None
    assert cam.roi[2] == RoiPoint(x=0.5, y=1.0)


@pytest.mark.parametrize(
    "roi",
    [
        [],
        [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}],  # not a polygon
        [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 1.2, "y": 0.5}],
        [{"x": -0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.5}],
        [{"x": 320, "y": 240}, {"x": 640, "y": 240}, {"x": 640, "y": 480}],  # pixels
    ],
)
def test_roi_invalid_polygons_are_rejected(roi: object) -> None:
    with pytest.raises(ValidationError):
        CameraConfig.model_validate(_camera(roi=roi))


@pytest.mark.parametrize("key", ["neutral_yaw_deg", "neutral_pitch_deg", "neutral_roll_deg"])
@pytest.mark.parametrize("value", [-91, 91, "0", float("nan")])
def test_calibration_angles_are_bounded(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Calibration.model_validate({key: value})


# --- capture ----------------------------------------------------------------


def test_capture_defaults_match_the_design_doc() -> None:
    """Section 7: ring of 3 s at 2 fps, 2 pre-frames and 2 post-frames,
    episode key frames for Sleeping and Absent only."""
    capture = CaptureConfig()
    assert capture.pre_frames == 2
    assert capture.post_frames == 2
    assert capture.frame_interval_ms == 500
    assert capture.ring_seconds == 3.0
    assert capture.ring_frames == 6
    assert set(capture.episode_keyframe_states) == {
        AttentionState.SLEEPING,
        AttentionState.ABSENT,
    }
    assert 1 <= capture.jpeg_quality <= 100


def test_capture_accepts_the_api_contract_shape() -> None:
    """api-contract.md 4.2 sends only these four keys."""
    capture = CaptureConfig.model_validate(
        {"pre_frames": 1, "post_frames": 3, "frame_interval_ms": 250, "jpeg_quality": 70}
    )
    assert capture.ring_seconds == 3.0
    assert capture.ring_frames == 12


@pytest.mark.parametrize(
    "change",
    [
        {"pre_frames": -1},
        {"pre_frames": 11},
        {"post_frames": -1},
        {"post_frames": 11},
        {"pre_frames": 1.5},
        {"frame_interval_ms": 99},
        {"frame_interval_ms": 5001},
        {"jpeg_quality": 0},
        {"jpeg_quality": 101},
        {"ring_seconds": 0},
        {"ring_seconds": 61},
        {"episode_keyframe_states": ["attentive"]},
        {"episode_keyframe_states": ["unknown"]},
        {"episode_keyframe_states": ["on_break"]},
        {"episode_keyframe_states": ["sleeping", "sleeping"]},
        {"record_video": True},
    ],
)
def test_capture_invalid_values_are_rejected(change: Json) -> None:
    with pytest.raises(ValidationError):
        CaptureConfig.model_validate(change)


def test_ring_must_hold_the_pre_frames_and_the_key_frame() -> None:
    # 3 pre-frames + key frame at 1 s spacing need 4 s of ring.
    with pytest.raises(ValidationError):
        CaptureConfig.model_validate(
            {"pre_frames": 3, "frame_interval_ms": 1000, "ring_seconds": 3.0}
        )
    assert CaptureConfig.model_validate(
        {"pre_frames": 3, "frame_interval_ms": 1000, "ring_seconds": 4.0}
    )


def test_episode_keyframes_can_be_turned_off() -> None:
    assert (
        CaptureConfig.model_validate({"episode_keyframe_states": []}).episode_keyframe_states == ()
    )


# --- shifts -----------------------------------------------------------------


def test_shift_parses_times_and_breaks() -> None:
    shift = Shift.model_validate(_shift())
    assert shift.id == SHIFT_A
    assert shift.start_time == time(6, 0)
    assert shift.breaks == (ShiftBreak(name="Tea", start_time=time(10, 0), end_time=time(10, 15)),)


def test_shift_may_cross_midnight() -> None:
    shift = Shift.model_validate(
        _shift(
            start_time="22:00:00",
            end_time="06:00:00",
            breaks=[
                {"name": "Late", "start_time": "23:50:00", "end_time": "00:10:00"},
                {"name": "Early", "start_time": "03:00:00", "end_time": "03:30:00"},
            ],
        )
    )
    assert len(shift.breaks) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"days_of_week": []},
        {"days_of_week": [7]},
        {"days_of_week": [-1]},
        {"days_of_week": [1, 1]},
        {"start_time": "06:00:00", "end_time": "06:00:00"},
        {"start_time": "06:00:00+05:30"},
        {"name": ""},
        {"id": "not-a-uuid"},
        {"breaks": [{"name": "Tea", "start_time": "10:00:00", "end_time": "10:00:00"}]},
        # Break outside the shift window.
        {"breaks": [{"name": "Tea", "start_time": "15:00:00", "end_time": "15:15:00"}]},
        {"breaks": [{"name": "Tea", "start_time": "13:50:00", "end_time": "14:10:00"}]},
        {"breaks": [{"name": "Tea", "start_time": "05:50:00", "end_time": "06:10:00"}]},
    ],
)
def test_shift_invalid_values_are_rejected(change: Json) -> None:
    with pytest.raises(ValidationError):
        Shift.model_validate(_shift() | change)


def test_break_outside_an_overnight_shift_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Shift.model_validate(
            _shift(
                start_time="22:00:00",
                end_time="06:00:00",
                breaks=[{"name": "Noon", "start_time": "12:00:00", "end_time": "12:15:00"}],
            )
        )


# --- engine config ----------------------------------------------------------


def test_engine_config_minimal_uses_defaults() -> None:
    config = EngineConfig.model_validate(_engine())
    assert config.thresholds == Thresholds()
    assert config.capture == CaptureConfig()
    assert config.shifts == ()
    assert config.shift_thresholds == ()
    assert config.scan_window_s == 5
    assert config.timezone == "UTC"
    assert config.config_version == 0
    assert config.primary_camera.slot == 1
    assert [c.slot for c in config.secondary_cameras] == [2]


def test_scan_window_follows_the_resolved_thresholds() -> None:
    config = EngineConfig.model_validate(_engine(thresholds={"scan_window_s": 8}))
    assert config.scan_window_s == 8


def test_engine_config_orders_cameras_by_slot() -> None:
    config = EngineConfig.model_validate(
        _engine(cameras=[_camera(3, "secondary"), _camera(2, "primary"), _camera(1, "secondary")])
    )
    assert [c.slot for c in config.cameras] == [1, 2, 3]
    assert config.primary_camera.slot == 2
    assert [c.slot for c in config.secondary_cameras] == [1, 3]


@pytest.mark.parametrize(
    "cameras",
    [
        [],
        [_camera(i, "primary" if i == 1 else "secondary") for i in (1, 2, 3, 4)] + [_camera(4)],
        [_camera(1, "primary"), _camera(1, "secondary")],  # duplicate slot
        [_camera(1, "primary"), _camera(2, "primary")],  # two primaries
        [_camera(1, "secondary"), _camera(2, "secondary")],  # no primary
    ],
)
def test_engine_config_camera_set_is_validated(cameras: list[Json]) -> None:
    with pytest.raises(ValidationError):
        EngineConfig.model_validate(_engine(cameras=cameras))


def test_engine_config_accepts_one_to_four_cameras() -> None:
    for count in (1, 2, 3, 4):
        cameras = [_camera(i, "primary" if i == 1 else "secondary") for i in range(1, count + 1)]
        assert len(EngineConfig.model_validate(_engine(cameras=cameras)).cameras) == count


def test_thresholds_for_shift() -> None:
    config = EngineConfig.model_validate(
        _engine(
            shifts=[_shift(SHIFT_A), _shift(SHIFT_B, name="Night")],
            shift_thresholds=[{"shift_id": str(SHIFT_B), "thresholds": {"absent_seconds": 20}}],
        )
    )
    assert config.thresholds_for(None) == Thresholds()
    assert config.thresholds_for(SHIFT_A) == Thresholds()  # no entry: station thresholds
    assert config.thresholds_for(SHIFT_B) == Thresholds(absent_seconds=20)
    assert config.shift_thresholds == (
        ShiftThresholds(shift_id=SHIFT_B, thresholds=Thresholds(absent_seconds=20)),
    )


@pytest.mark.parametrize(
    "change",
    [
        # Thresholds for a shift that is not in the bundle.
        {"shift_thresholds": [{"shift_id": str(SHIFT_B), "thresholds": {}}]},
        {
            "shifts": [_shift(SHIFT_A)],
            "shift_thresholds": [
                {"shift_id": str(SHIFT_A), "thresholds": {}},
                {"shift_id": str(SHIFT_A), "thresholds": {}},
            ],
        },
        {"shifts": [_shift(SHIFT_A), _shift(SHIFT_A)]},
        {"thresholds": {"absent_seconds": 0}},
        {"capture": {"jpeg_quality": 500}},
        {"timezone": ""},
        {"config_version": -1},
        {"blocking_mode": True},
        {"profile": {"kind": "custom"}},
    ],
)
def test_engine_config_invalid_values_are_rejected(change: Json) -> None:
    with pytest.raises(ValidationError):
        EngineConfig.model_validate(_engine(**change))


def test_engine_config_requires_cameras_and_profile() -> None:
    for missing in ("cameras", "profile"):
        data = _engine()
        del data[missing]
        with pytest.raises(ValidationError):
            EngineConfig.model_validate(data)


def test_engine_config_json_round_trip() -> None:
    config = EngineConfig.model_validate(
        _engine(
            cameras=[
                _camera(
                    1,
                    "primary",
                    roi=[{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 0.5, "y": 1.0}],
                    calibration={"neutral_pitch_deg": -8.5, "exposure_notes": "window left"},
                ),
                _camera(2, "secondary", kind="rtsp", uri="rtsp://10.0.0.9/1", is_enabled=False),
            ],
            profile=builtin_profile(PpeProfileKind.HELMET_SHIELD).model_dump(mode="json"),
            thresholds={"sleeping_head_drop_s": 6, "yawns_for_drowsy": 4},
            shifts=[
                _shift(SHIFT_A),
                _shift(SHIFT_B, start_time="22:00:00", end_time="06:00:00", breaks=[]),
            ],
            shift_thresholds=[
                {"shift_id": str(SHIFT_B), "thresholds": {"perclos_threshold": 0.25}}
            ],
            capture={"pre_frames": 1, "episode_keyframe_states": ["sleeping"]},
            timezone="Asia/Kolkata",
            config_version=42,
        )
    )
    as_json = config.model_dump_json()
    assert EngineConfig.model_validate_json(as_json) == config
    assert EngineConfig.model_validate(config.model_dump(mode="json")) == config
    assert EngineConfig.model_validate(config.model_dump()) == config
