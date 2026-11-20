"""The service IPC message format.

Every message is one small UTF-8 JSON object, framed by the transport and
validated on both ends. No pickling: a peer can send only data, never code.

Request   {"v": 1, "id": 7, "op": "scan_state"}
Response  {"v": 1, "id": 7, "ok": true, "result": {...}}
          {"v": 1, "id": 7, "ok": false, "error": "engine_unavailable"}
Event     {"v": 1, "event": "state_changed", "data": {...}}

A response to a request that could not be parsed carries `id: null`.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal, Self, TypeVar

from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from ag_workstation.engine_api import (
    CameraHealthSummary,
    CameraState,
    EngineHealth,
    StateBucket,
    StateEpisode,
    StationState,
)

PROTOCOL_VERSION: Final = 1
MAX_MESSAGE_BYTES: Final = 64 * 1024
"""Larger frames are refused by the transport (a state answer is ~1 KiB)."""

_MAX_ID: Final = 2**53


class ProtocolError(ValueError):
    """A message that is not valid JSON or does not match its schema."""


class Op(StrEnum):
    """Request operations."""

    SCAN_STATE = "scan_state"
    CURRENT_STATE = "current_state"
    HEALTH = "health"
    PING = "ping"
    SUBSCRIBE = "subscribe"
    """Turn this connection into an event channel."""


class ErrorCode(StrEnum):
    """Why a request failed (no free text crosses the wire)."""

    UNAVAILABLE = "engine_unavailable"
    TIMEOUT = "engine_timeout"
    ENGINE_ERROR = "engine_error"
    BAD_REQUEST = "bad_request"
    INTERNAL = "internal"


class EventKind(StrEnum):
    """Events pushed to subscribers."""

    STATE_CHANGED = "state_changed"
    EPISODE_OPENED = "episode_opened"
    EPISODE_CLOSED = "episode_closed"
    BUCKET_CLOSED = "bucket_closed"


class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


Unit = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Slot = Annotated[int, Field(ge=1, le=4)]
Version = Annotated[str, Field(max_length=64)]
MsgId = Annotated[StrictInt, Field(ge=0, le=_MAX_ID)]


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC")
    return value


class Request(_Msg):
    """A client request."""

    v: Literal[1] = PROTOCOL_VERSION
    id: MsgId
    op: Op


class Response(_Msg):
    """A server answer: `result` when ok, else `error`."""

    v: Literal[1] = PROTOCOL_VERSION
    id: MsgId | None
    ok: bool
    result: dict[str, JsonValue] | None = None
    error: ErrorCode | None = None

    @model_validator(mode="after")
    def _result_or_error(self) -> Self:
        if self.ok and self.result is None:
            raise ValueError("ok response without result")
        if not self.ok and self.error is None:
            raise ValueError("failed response without error")
        return self


class Event(_Msg):
    """A pushed event."""

    v: Literal[1] = PROTOCOL_VERSION
    event: EventKind
    data: dict[str, JsonValue]


class CameraStateMsg(_Msg):
    """`CameraState` on the wire."""

    slot: Slot
    status: CameraStatus
    state: AttentionState
    quality: Unit | None = None
    usable: bool = False

    @classmethod
    def of(cls, cam: CameraState) -> Self:
        """From the engine type."""
        return cls(
            slot=cam.slot,
            status=cam.status,
            state=cam.state,
            quality=cam.quality,
            usable=cam.usable,
        )

    def to_camera(self) -> CameraState:
        """To the engine type."""
        return CameraState(self.slot, self.status, self.state, self.quality, self.usable)


Signals = Annotated[
    dict[Annotated[str, Field(max_length=64)], Annotated[float, Field(allow_inf_nan=False)] | None],
    Field(max_length=64),
]


class StationStateMsg(_Msg):
    """`StationState` on the wire."""

    state: AttentionState
    confidence: Unit | None
    low_confidence: bool
    at: AwareDatetime
    ts: Annotated[float, Field(allow_inf_nan=False)]
    engine_version: Version
    config_version: Annotated[int, Field(ge=0)]
    signals: Signals = Field(default_factory=dict)
    camera_states: Annotated[tuple[CameraStateMsg, ...], Field(max_length=4)] = ()

    _check_at = field_validator("at")(_utc)

    @classmethod
    def of(cls, s: StationState) -> Self:
        """From the engine type."""
        return cls(
            state=s.state,
            confidence=s.confidence,
            low_confidence=s.low_confidence,
            at=s.at,
            ts=s.ts,
            engine_version=s.engine_version,
            config_version=s.config_version,
            signals=dict(s.signals),
            camera_states=tuple(CameraStateMsg.of(c) for c in s.camera_states),
        )

    def to_state(self) -> StationState:
        """To the engine type."""
        return StationState(
            state=self.state,
            confidence=self.confidence,
            low_confidence=self.low_confidence,
            at=self.at,
            ts=self.ts,
            engine_version=self.engine_version,
            config_version=self.config_version,
            signals=self.signals,
            camera_states=tuple(c.to_camera() for c in self.camera_states),
        )


class CameraHealthMsg(_Msg):
    """`CameraHealthSummary` on the wire."""

    slot: Slot
    status: CameraStatus
    fps: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    quality: Unit | None = None


class EngineHealthMsg(_Msg):
    """`EngineHealth` on the wire."""

    status: EngineStatus
    cameras: Annotated[tuple[CameraHealthMsg, ...], Field(max_length=4)] = ()
    restarts: Annotated[int, Field(ge=0)] = 0
    engine_version: Version = ""
    config_version: Annotated[int, Field(ge=0)] = 0
    last_error: Annotated[str, Field(max_length=128)] | None = None

    @classmethod
    def of(cls, h: EngineHealth) -> Self:
        """From the engine type."""
        return cls(
            status=h.status,
            cameras=tuple(
                CameraHealthMsg(slot=c.slot, status=c.status, fps=c.fps, quality=c.quality)
                for c in h.cameras
            ),
            restarts=h.restarts,
            engine_version=h.engine_version,
            config_version=h.config_version,
            last_error=h.last_error,
        )

    def to_health(self) -> EngineHealth:
        """To the engine type."""
        return EngineHealth(
            status=self.status,
            cameras=tuple(
                CameraHealthSummary(c.slot, c.status, c.fps, c.quality) for c in self.cameras
            ),
            restarts=self.restarts,
            engine_version=self.engine_version,
            config_version=self.config_version,
            last_error=self.last_error,
        )


class EpisodeMsg(_Msg):
    """`StateEpisode` on the wire (no worker identity: the engine has none)."""

    id: str
    state: AttentionState
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    confidence: Unit | None
    low_confidence: bool
    engine_version: Version
    config_version: Annotated[int, Field(ge=0)]

    _check_started = field_validator("started_at")(_utc)

    @field_validator("ended_at")
    @classmethod
    def _check_ended(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @classmethod
    def of(cls, e: StateEpisode) -> Self:
        """From the engine type."""
        return cls(
            id=str(e.id),
            state=e.state,
            started_at=e.started_at,
            ended_at=e.ended_at,
            confidence=e.confidence,
            low_confidence=e.low_confidence,
            engine_version=e.engine_version,
            config_version=e.config_version,
        )


class BucketMsg(_Msg):
    """`StateBucket` on the wire."""

    bucket_start: AwareDatetime
    seconds: dict[AttentionState, Annotated[float, Field(ge=0, le=60.001)]]

    @classmethod
    def of(cls, b: StateBucket) -> Self:
        """From the engine type."""
        return cls(bucket_start=b.bucket_start, seconds=dict(b.seconds))


def _dump(model: BaseModel) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = model.model_dump(mode="json")
    return data


def encode(message: Request | Response | Event) -> bytes:
    """Serialize a message for the transport."""
    data = message.model_dump_json().encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message too large")
    return data


_M = TypeVar("_M", bound=_Msg)


def _decode(kind: type[_M], data: bytes) -> _M:
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message too large")
    try:
        return kind.model_validate_json(data)
    except (ValidationError, ValueError) as exc:
        # The validation text may echo peer data; keep only the type.
        raise ProtocolError(f"malformed {kind.__name__.lower()}") from exc


def decode_request(data: bytes) -> Request:
    """Parse and validate a request."""
    return _decode(Request, data)


def decode_response(data: bytes) -> Response:
    """Parse and validate a response."""
    return _decode(Response, data)


def decode_event(data: bytes) -> Event:
    """Parse and validate an event envelope (its `data` is checked by `parse_event_data`)."""
    return _decode(Event, data)


def ok(request_id: int, result: BaseModel | Mapping[str, JsonValue]) -> Response:
    """A successful response."""
    body = _dump(result) if isinstance(result, BaseModel) else dict(result)
    return Response(id=request_id, ok=True, result=body)


def failed(request_id: int | None, error: ErrorCode) -> Response:
    """A failed response."""
    return Response(id=request_id, ok=False, error=error)


def state_event(state: StationState) -> Event:
    """`state_changed` event."""
    return Event(event=EventKind.STATE_CHANGED, data=_dump(StationStateMsg.of(state)))


def episode_event(episode: StateEpisode) -> Event:
    """`episode_opened` or `episode_closed`, by whether it has ended."""
    kind = EventKind.EPISODE_OPENED if episode.ended_at is None else EventKind.EPISODE_CLOSED
    return Event(event=kind, data=_dump(EpisodeMsg.of(episode)))


def bucket_event(bucket: StateBucket) -> Event:
    """`bucket_closed` event."""
    return Event(event=EventKind.BUCKET_CLOSED, data=_dump(BucketMsg.of(bucket)))


EventData = StationStateMsg | EpisodeMsg | BucketMsg

_EVENT_MODELS: Final[dict[EventKind, type[EventData]]] = {
    EventKind.STATE_CHANGED: StationStateMsg,
    EventKind.EPISODE_OPENED: EpisodeMsg,
    EventKind.EPISODE_CLOSED: EpisodeMsg,
    EventKind.BUCKET_CLOSED: BucketMsg,
}


def parse_event_data(event: Event) -> EventData:
    """Validate an event's `data` against the model for its kind."""
    try:
        return _EVENT_MODELS[event.event].model_validate(event.data)
    except (ValidationError, ValueError) as exc:
        raise ProtocolError(f"malformed {event.event.value} data") from exc
