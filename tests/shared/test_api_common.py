"""Wire primitives: UTC timestamps, secrets, lenient enums, unknown-field stripping."""

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated

import pytest
from ag_shared.api.common import (
    ApiModel,
    UtcDatetime,
    WireSecret,
    drop_unknown_enum_keys,
    lenient_enum,
    strip_unknown_fields,
)
from ag_shared.config import CameraConfig, EngineConfig
from ag_shared.ppe import PpeProfileKind, builtin_profile
from pydantic import BeforeValidator, Field, ValidationError


class _Color(StrEnum):
    RED = "red"
    GREY = "grey"


class _Stamp(ApiModel):
    at: UtcDatetime


class _Cred(ApiModel):
    secret: WireSecret


class _Painted(ApiModel):
    color: Annotated[_Color, BeforeValidator(lenient_enum(_Color, _Color.GREY))]
    counts: Annotated[dict[_Color, int], BeforeValidator(drop_unknown_enum_keys(_Color))] = Field(
        default_factory=dict
    )


def test_timestamps_are_utc_with_millisecond_precision() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    stamp = _Stamp(at=datetime(2026, 11, 20, 17, 30, 0, 123999, tzinfo=ist))
    assert stamp.at == datetime(2026, 11, 20, 12, 0, 0, 123000, tzinfo=UTC)
    assert stamp.at.tzinfo is UTC
    assert json.loads(stamp.model_dump_json()) == {"at": "2026-11-20T12:00:00.123Z"}
    assert _Stamp.model_validate_json('{"at": "2026-11-20T12:00:00.123Z"}') == stamp


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _Stamp(at=datetime(2026, 11, 20, 12, 0))
    with pytest.raises(ValidationError):
        _Stamp.model_validate_json('{"at": "2026-11-20T12:00:00"}')


def test_secrets_are_masked_except_in_json() -> None:
    cred = _Cred.model_validate({"secret": "s3cr3t-value"})
    assert "s3cr3t" not in repr(cred)
    assert "s3cr3t" not in str(cred.model_dump())
    assert json.loads(cred.model_dump_json()) == {"secret": "s3cr3t-value"}
    with pytest.raises(ValidationError):
        _Cred.model_validate({"secret": ""})


def test_unknown_fields_are_ignored() -> None:
    assert _Stamp.model_validate({"at": "2026-11-20T12:00:00Z", "future": 1}).at.year == 2026


def test_unknown_enum_value_maps_to_default_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ag_shared.api"):
        painted = _Painted.model_validate({"color": "ultraviolet"})
    assert painted.color is _Color.GREY
    assert "ultraviolet" in caplog.text
    assert _Painted.model_validate({"color": "red"}).color is _Color.RED
    with pytest.raises(ValidationError):
        _Painted.model_validate({"color": 3})  # not a string: still refused


def test_unknown_enum_keys_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ag_shared.api"):
        painted = _Painted.model_validate({"color": "red", "counts": {"red": 1, "blue": 2}})
    assert painted.counts == {_Color.RED: 1}
    assert "blue" in caplog.text


def test_strip_unknown_fields_reaches_nested_strict_models() -> None:
    raw = {
        "cameras": [
            {
                "slot": 1,
                "role": "primary",
                "kind": "usb",
                "uri": "0",
                "roi": [{"x": 0.1, "y": 0.1, "z": 0}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.9}],
                "calibration": {"neutral_yaw_deg": 2.0, "infrared": True},
                "zoom": 2,
            }
        ],
        "profile": {
            "kind": "open_face",
            "signals": {
                **builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json")["signals"],
                "pupil_dilation": {"enabled": True},
            },
            "thresholds": {"absent_seconds": 12, "new_key": 1},
        },
        "thresholds": {"absent_seconds": 11, "future_threshold": 3},
        "shifts": [],
        "brand_new": {"a": 1},
    }
    with pytest.raises(ValidationError):
        EngineConfig.model_validate(raw)
    config = EngineConfig.model_validate(strip_unknown_fields(EngineConfig, raw))
    assert config.thresholds.absent_seconds == 11
    assert config.cameras[0].calibration.neutral_yaw_deg == 2.0
    assert config.profile.thresholds is not None
    assert config.profile.thresholds.absent_seconds == 12


def test_strip_unknown_fields_leaves_non_mappings_and_odd_keys() -> None:
    assert strip_unknown_fields(CameraConfig, [1, 2]) == [1, 2]
    assert strip_unknown_fields(CameraConfig, {1: "x", "slot": 1}) == {"slot": 1}
