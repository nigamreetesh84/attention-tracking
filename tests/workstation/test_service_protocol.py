"""IPC message validation and engine-type round trips."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState
from ag_workstation.engine_api import (
    CameraHealthSummary,
    CameraState,
    EngineHealth,
    StateBucket,
    StateEpisode,
    StationState,
)
from ag_workstation.service import protocol as p
from pydantic import JsonValue

T0 = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def test_station_state_round_trip() -> None:
    state = StationState(
        AttentionState.DISTRACTED,
        0.6,
        True,
        T0,
        12.5,
        "1.2.3",
        4,
        signals={"yaw": 31.0, "perclos": None},
        camera_states=(CameraState(2, CameraStatus.ONLINE, AttentionState.DISTRACTED, 0.7, True),),
    )
    data = p.encode(p.ok(1, p.StationStateMsg.of(state)))
    reply = p.decode_response(data)
    assert reply.result is not None
    assert p.StationStateMsg.model_validate(reply.result).to_state() == state


def test_engine_health_round_trip() -> None:
    health = EngineHealth(
        EngineStatus.DEGRADED,
        (CameraHealthSummary(1, CameraStatus.OFFLINE),),
        restarts=3,
        engine_version="e",
        config_version=2,
        last_error="OSError",
    )
    assert p.EngineHealthMsg.of(health).to_health() == health


def test_event_builders_and_parsing() -> None:
    episode = StateEpisode(
        UUID(int=1), AttentionState.SLEEPING, T0, 0.8, False, "e", 1, ended_at=T0 + timedelta(1)
    )
    closed = p.decode_event(p.encode(p.episode_event(episode)))
    assert closed.event is p.EventKind.EPISODE_CLOSED
    parsed = p.parse_event_data(closed)
    assert isinstance(parsed, p.EpisodeMsg) and parsed.ended_at == episode.ended_at
    opened = p.episode_event(
        StateEpisode(UUID(int=2), AttentionState.ABSENT, T0, None, False, "e", 1)
    )
    assert opened.event is p.EventKind.EPISODE_OPENED
    bucket = p.bucket_event(StateBucket(T0, {AttentionState.ATTENTIVE: 59.0}))
    assert isinstance(p.parse_event_data(bucket), p.BucketMsg)


@pytest.mark.parametrize(
    "data",
    [
        {"state": "attentive"},
        {
            "state": "attentive",
            "confidence": None,
            "low_confidence": False,
            "at": "2026-03-01T08:00:00",
            "ts": 1,
            "engine_version": "e",
            "config_version": 1,
        },
    ],
)
def test_bad_event_data_is_a_protocol_error(data: dict[str, object]) -> None:
    event = p.Event.model_validate({"event": "state_changed", "data": data})
    with pytest.raises(p.ProtocolError):
        p.parse_event_data(event)


def test_non_utc_episode_end_is_rejected() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    data: dict[str, JsonValue] = {
        "id": "x",
        "state": "sleeping",
        "started_at": T0.isoformat(),
        "ended_at": T0.astimezone(ist).isoformat(),
        "confidence": None,
        "low_confidence": False,
        "engine_version": "e",
        "config_version": 1,
    }
    with pytest.raises(p.ProtocolError):
        p.parse_event_data(p.Event(event=p.EventKind.EPISODE_CLOSED, data=data))


def test_size_limits() -> None:
    with pytest.raises(p.ProtocolError):
        p.decode_request(b" " * (p.MAX_MESSAGE_BYTES + 1))
    huge = p.Event(event=p.EventKind.STATE_CHANGED, data={"x": "y" * p.MAX_MESSAGE_BYTES})
    with pytest.raises(p.ProtocolError):
        p.encode(huge)


def test_unknown_fields_are_ignored_and_ids_bounded() -> None:
    req = p.decode_request(b'{"v": 1, "id": 2, "op": "health", "extra": [1, 2]}')
    assert req.op is p.Op.HEALTH
    with pytest.raises(p.ProtocolError):
        p.decode_request(b'{"v": 1, "id": 9007199254740993, "op": "ping"}')
    with pytest.raises(p.ProtocolError):
        p.decode_request(b'{"v": 1, "id": true, "op": "ping"}')
