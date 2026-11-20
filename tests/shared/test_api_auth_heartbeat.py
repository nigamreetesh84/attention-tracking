"""Enrollment, token and heartbeat payloads, including commands."""

import json
from uuid import UUID

import pytest
from ag_shared.api import (
    AssignWorkerCommand,
    EndSessionCommand,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    LiveStillCommand,
    LiveVideoCommand,
    PullConfigCommand,
    RotateSecretCommand,
    RotateSecretRequest,
    RotateSecretResponse,
    TokenRequest,
    TokenResponse,
    UnknownCommand,
)
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState
from pydantic import ValidationError

WS = UUID("0193a0b0-0000-7000-8000-000000000001")
CMD = UUID("0193a0b0-0000-7000-8000-0000000000c1")
T = "2026-11-20T12:00:00.000Z"


def test_enroll_round_trip_keeps_secrets_out_of_repr() -> None:
    req = EnrollRequest.model_validate(
        {"enrollment_token": "tok-123", "hostname": "WS-01", "hardware_fingerprint": "fp"}
    )
    assert "tok-123" not in repr(req)
    sent = json.loads(req.model_dump_json())
    assert sent["enrollment_token"] == "tok-123"  # noqa: S105 - test value
    resp = EnrollResponse.model_validate(
        {"workstation_id": str(WS), "device_secret": "x" * 43, "server_time": T, "extra": 1}
    )
    assert resp.device_secret.get_secret_value() == "x" * 43
    assert EnrollResponse.model_validate_json(resp.model_dump_json()) == resp


def test_token_and_rotation_payloads() -> None:
    req = TokenRequest.model_validate({"workstation_id": str(WS), "device_secret": "abc"})
    assert req.device_secret.get_secret_value() == "abc"
    tok = TokenResponse.model_validate({"access_token": "jwt", "expires_in": 900})
    assert tok.server_time is None
    with pytest.raises(ValidationError):
        TokenResponse.model_validate({"access_token": "jwt", "expires_in": 0})
    assert RotateSecretRequest.model_validate({"anything": 1}) == RotateSecretRequest()
    rot = RotateSecretResponse.model_validate({"device_secret": "new"})
    assert rot.previous_valid_until is None


def _heartbeat(**extra: object) -> dict[str, object]:
    return {
        "seq": 7,
        "sent_at": T,
        "state": "attentive",
        "confidence": 0.9,
        "cameras": [{"slot": 1, "status": "online", "fps": 12.5, "quality": 0.8}],
        "engine_status": "running",
        **extra,
    }


def test_heartbeat_request_defaults_and_round_trip() -> None:
    hb = HeartbeatRequest.model_validate(_heartbeat())
    assert hb.low_confidence is False
    assert hb.applied_config_version is None
    assert hb.shift_counters == {}
    assert hb.acked_commands == ()
    assert HeartbeatRequest.model_validate_json(hb.model_dump_json()) == hb


def test_heartbeat_maps_unknown_enum_values_to_safe_defaults() -> None:
    hb = HeartbeatRequest.model_validate(
        _heartbeat(
            state="daydreaming",
            engine_status="hibernating",
            cameras=[{"slot": 2, "status": "melted"}],
            shift_counters={"attentive": 10, "daydreaming": 3},
            future_field={"x": 1},
        )
    )
    assert hb.state is AttentionState.UNKNOWN
    assert hb.engine_status is EngineStatus.UNKNOWN
    assert hb.cameras[0].status is CameraStatus.OFFLINE
    assert hb.shift_counters == {AttentionState.ATTENTIVE: 10}


@pytest.mark.parametrize(
    "bad",
    [{"seq": -1}, {"confidence": 1.5}, {"cameras": [{"slot": 5, "status": "online"}]}],
)
def test_heartbeat_rejects_out_of_range_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HeartbeatRequest.model_validate(_heartbeat(**bad))


def test_heartbeat_response_parses_every_command_type() -> None:
    commands: list[dict[str, object]] = [
        {"type": "assign_worker", "assignment_id": str(WS), "user_id": str(WS), "valid_until": T},
        {"type": "end_session", "session_id": str(WS)},
        {"type": "live_still", "request_id": str(WS), "camera_slot": 1, "expires_at": T},
        {"type": "live_video", "session_id": str(WS), "camera_slot": 2, "expires_at": T},
        {"type": "pull_config", "version": 4},
        {"type": "rotate_secret"},
        {"type": "self_destruct", "when": "now"},
    ]
    resp = HeartbeatResponse.model_validate(
        {
            "server_time": T,
            "config_version": 4,
            "commands": [{"id": str(CMD), **c} for c in commands],
        }
    )
    kinds = [type(c) for c in resp.commands]
    assert kinds == [
        AssignWorkerCommand,
        EndSessionCommand,
        LiveStillCommand,
        LiveVideoCommand,
        PullConfigCommand,
        RotateSecretCommand,
        UnknownCommand,
    ]
    assert all(c.id == CMD for c in resp.commands)
    assert HeartbeatResponse.model_validate_json(resp.model_dump_json()) == resp


def test_a_malformed_known_command_is_an_error() -> None:
    with pytest.raises(ValidationError):
        HeartbeatResponse.model_validate(
            {
                "server_time": T,
                "config_version": 1,
                "commands": [{"id": str(CMD), "type": "live_still", "camera_slot": 9}],
            }
        )


def test_commands_built_in_code_keep_their_type() -> None:
    resp = HeartbeatResponse(
        server_time=T,  # type: ignore[arg-type]  # parsed from ISO text
        config_version=1,
        commands=(PullConfigCommand(id=CMD, version=2), UnknownCommand(id=CMD, type="later")),
    )
    assert isinstance(resp.commands[0], PullConfigCommand)
    assert isinstance(resp.commands[1], UnknownCommand)
