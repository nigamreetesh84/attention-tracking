"""Config bundle: validation, unknown-field tolerance and per-profile threshold resolution."""

import logging
from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest
from ag_shared.api import ConfigBundle
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.site import DeploymentMode, SiteSettings
from ag_shared.thresholds import Thresholds
from pydantic import ValidationError

Json = dict[str, Any]  # Any: free-form JSON test fixtures

SHIFT_A = UUID("0193a0b0-0000-7000-8000-00000000a001")
SHIFT_B = UUID("0193a0b0-0000-7000-8000-00000000a002")
MASK = UUID("0193a0b0-0000-7000-8000-00000000b001")
SUN = UUID("0193a0b0-0000-7000-8000-00000000b002")
WORKER = UUID("0193a0b0-0000-7000-8000-00000000c001")
PIN_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


def _profile(kind: PpeProfileKind) -> Json:
    return builtin_profile(kind).model_dump(mode="json")


def _thresholds(absent: float) -> Json:
    return {"absent_seconds": absent}


def _bundle(**extra: object) -> Json:
    bundle: Json = {
        "version": 5,
        "generated_at": "2026-11-20T12:00:00Z",
        "site": {"timezone": "Asia/Kolkata", "settings": {}},
        "workstation": {
            "id": str(WORKER),
            "name": "Line 1 / WS 3",
            "line_id": str(WORKER),
            "camera_count": 2,
        },
        "cameras": [
            {"slot": 1, "role": "primary", "kind": "usb", "uri": "0"},
            {"slot": 2, "role": "secondary", "kind": "rtsp", "uri": "rtsp://u:p@cam/1"},
        ],
        "ppe_profile": _profile(PpeProfileKind.OPEN_FACE),
        "thresholds": _thresholds(10),
        "shift_thresholds": [{"shift_id": str(SHIFT_A), "thresholds": _thresholds(11)}],
        "profile_thresholds": [
            {
                "ppe_profile_id": str(MASK),
                "ppe_profile": _profile(PpeProfileKind.MASK),
                "thresholds": _thresholds(20),
                "shift_thresholds": [{"shift_id": str(SHIFT_A), "thresholds": _thresholds(21)}],
            }
        ],
        "shifts": [
            {
                "id": str(SHIFT_A),
                "name": "Morning",
                "start_time": "06:00",
                "end_time": "14:00",
                "days_of_week": [0, 1, 2, 3, 4],
            },
            {
                "id": str(SHIFT_B),
                "name": "Night",
                "start_time": "22:00",
                "end_time": "06:00",
                "days_of_week": [0, 1, 2, 3, 4],
            },
        ],
        "roster": [
            {
                "user_id": str(WORKER),
                "employee_id": "EMP0042",
                "display_name": "A. Worker",
                "pin_hash": PIN_HASH,
                "has_consent": True,
                "ppe_profile_override": str(MASK),
                "preferred_language": "te",
                "is_active": True,
            }
        ],
        "consent_policy": {"version": 2, "language": "en", "body": "Policy text"},
        "capture": {
            "pre_frames": 2,
            "post_frames": 2,
            "frame_interval_ms": 500,
            "jpeg_quality": 80,
        },
        "retention_local": {"max_age_days": 30, "max_disk_mb": 2048},
        "target_app_version": "1.0.0",
    }
    bundle.update(extra)
    return bundle


def test_full_bundle_is_valid_and_round_trips() -> None:
    bundle = ConfigBundle.model_validate(_bundle())
    assert bundle.site.settings == SiteSettings()
    assert bundle.roster[0].has_consent
    assert ConfigBundle.model_validate_json(bundle.model_dump_json()) == bundle


def test_bundle_without_the_new_field_is_still_valid() -> None:
    raw = _bundle()
    del raw["profile_thresholds"]
    assert ConfigBundle.model_validate(raw).profile_thresholds == ()


def test_unknown_fields_at_any_depth_are_ignored() -> None:
    raw = _bundle(new_top_level={"x": 1})
    raw["site"]["settings"]["future_setting"] = True
    raw["cameras"][0]["zoom"] = 3
    raw["ppe_profile"]["signals"]["heart_rate"] = {"enabled": True}
    raw["ppe_profile"]["new_flag"] = 1
    raw["thresholds"]["future_threshold_s"] = 9
    raw["shift_thresholds"][0]["thresholds"]["other"] = 1
    raw["profile_thresholds"][0]["ppe_profile"]["signals"]["heart_rate"] = {"enabled": True}
    raw["profile_thresholds"][0]["note"] = "x"
    raw["shifts"][0]["breaks"] = [
        {"name": "Tea", "start_time": "10:00", "end_time": "10:15", "paid": True}
    ]
    raw["roster"][0]["badge"] = "none"
    raw["capture"]["hdr"] = True
    assert ConfigBundle.model_validate(raw) == ConfigBundle.model_validate(
        _bundle(
            shifts=[
                {
                    **_bundle()["shifts"][0],
                    "breaks": [{"name": "Tea", "start_time": "10:00", "end_time": "10:15"}],
                },
                _bundle()["shifts"][1],
            ]
        )
    )


def test_unknown_deployment_mode_maps_to_on_premise() -> None:
    raw = _bundle()
    raw["site"]["settings"]["deployment_mode"] = "edge"
    assert (
        ConfigBundle.model_validate(raw).site.settings.deployment_mode is DeploymentMode.ON_PREMISE
    )


def _mutate(path: str, value: object) -> Json:
    raw = _bundle()
    target: Any = raw  # Any: walking a JSON tree
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[int(key)] if key.isdigit() else target[key]
    last = keys[-1]
    if last.isdigit():
        target[int(last)] = value
    else:
        target[last] = value
    return raw


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("ppe_profile.kind", "balaclava"),
        ("cameras.0.kind", "carrier_pigeon"),
        ("cameras.1.role", "primary"),
        ("site.settings.blocking_mode", True),
        ("site.settings.fail_open", False),
        ("site.settings.retention_audit_days", 30),
        ("shift_thresholds.0.shift_id", "0193a0b0-0000-7000-8000-00000000dead"),
        (
            "profile_thresholds.0.shift_thresholds.0.shift_id",
            "0193a0b0-0000-7000-8000-00000000dead",
        ),
        ("roster.0.employee_id", "EMP-0042"),
        ("roster.0.employee_id", "A" * 17),
        ("roster.0.pin_hash", "plaintext1234"),
        ("thresholds.gap_tolerance_s", 5),
        ("cameras", []),
    ],
)
def test_invalid_bundles_are_refused(path: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ConfigBundle.model_validate(_mutate(path, value))


def test_duplicates_are_refused() -> None:
    raw = _bundle()
    raw["profile_thresholds"].append(deepcopy(raw["profile_thresholds"][0]))
    with pytest.raises(ValidationError, match="ppe_profile_id"):
        ConfigBundle.model_validate(raw)
    raw = _bundle()
    raw["roster"].append({**raw["roster"][0], "employee_id": "emp0042", "user_id": str(SUN)})
    with pytest.raises(ValidationError, match="employee_id"):
        ConfigBundle.model_validate(raw)
    raw = _bundle()
    raw["roster"].append({**raw["roster"][0], "employee_id": "EMP7"})
    with pytest.raises(ValidationError, match="user_id"):
        ConfigBundle.model_validate(raw)


def test_pin_hash_stays_out_of_repr() -> None:
    bundle = ConfigBundle.model_validate(_bundle())
    assert "argon2id" not in repr(bundle.roster[0])
    assert bundle.roster_entry("emp0042") == bundle.roster[0]
    assert bundle.roster_entry("nobody") is None


@pytest.mark.parametrize(
    ("profile_id", "shift_id", "absent", "kind"),
    [
        (MASK, SHIFT_A, 21, PpeProfileKind.MASK),  # override + its shift entry
        (MASK, SHIFT_B, 20, PpeProfileKind.MASK),  # override, no shift entry
        (MASK, None, 20, PpeProfileKind.MASK),  # override, no shift
        (None, SHIFT_A, 11, PpeProfileKind.OPEN_FACE),  # station + shift entry
        (None, SHIFT_B, 10, PpeProfileKind.OPEN_FACE),  # station, no shift entry
        (None, None, 10, PpeProfileKind.OPEN_FACE),
        (SUN, SHIFT_A, 11, PpeProfileKind.OPEN_FACE),  # override not carried: station
    ],
)
def test_resolution_fallback_order(
    profile_id: UUID | None, shift_id: UUID | None, absent: float, kind: PpeProfileKind
) -> None:
    resolved = ConfigBundle.model_validate(_bundle()).resolve(profile_id, shift_id)
    assert resolved.thresholds.absent_seconds == absent
    assert resolved.profile.kind is kind
    assert resolved.ppe_profile_id == (profile_id if profile_id == MASK else None)


def test_missing_override_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    bundle = ConfigBundle.model_validate(_bundle())
    with caplog.at_level(logging.WARNING, logger="ag_shared.api"):
        bundle.resolve(SUN)
    assert str(SUN) in caplog.text


def test_engine_config_for_station_and_override() -> None:
    bundle = ConfigBundle.model_validate(_bundle())
    station = bundle.engine_config()
    assert station.profile.kind is PpeProfileKind.OPEN_FACE
    assert station.thresholds_for(SHIFT_A).absent_seconds == 11
    assert station.config_version == 5
    assert station.timezone == "Asia/Kolkata"
    assert station.primary_camera.slot == 1
    masked = bundle.engine_config(MASK)
    assert masked.profile.kind is PpeProfileKind.MASK
    assert masked.thresholds == Thresholds(absent_seconds=20)
    assert masked.thresholds_for(SHIFT_A).absent_seconds == 21
    assert masked.cameras == station.cameras


def test_camera_uri_is_not_in_repr() -> None:
    assert "u:p@" not in repr(ConfigBundle.model_validate(_bundle()))
