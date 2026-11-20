"""Heartbeat request, response and commands (`api-contract.md` section 4.1).

Commands are flat objects `{id, type, ...payload fields}`. They are repeated
until the client acknowledges their `id` in a later heartbeat
(`acked_commands`), so handling must be idempotent by id. A command type the
client does not know is parsed as `UnknownCommand` instead of failing the
whole response.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BeforeValidator, Discriminator, Field, Tag, model_validator

from ag_shared.api.common import (
    ApiModel,
    CameraSlot,
    Confidence,
    NonNegativeInt,
    UtcDatetime,
    drop_unknown_enum_keys,
    lenient_enum,
    logger,
)
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState

WireState = Annotated[
    AttentionState, BeforeValidator(lenient_enum(AttentionState, AttentionState.UNKNOWN))
]
"""An attention state; unknown values become Unknown."""

WireCameraStatus = Annotated[
    CameraStatus, BeforeValidator(lenient_enum(CameraStatus, CameraStatus.OFFLINE))
]
"""Unknown camera status values count as offline (raises the degraded alert)."""

WireEngineStatus = Annotated[
    EngineStatus, BeforeValidator(lenient_enum(EngineStatus, EngineStatus.UNKNOWN))
]

StateCounts = Annotated[
    dict[AttentionState, NonNegativeInt],
    BeforeValidator(drop_unknown_enum_keys(AttentionState)),
]


class CameraHeartbeat(ApiModel):
    """One camera in a heartbeat or health sample."""

    slot: CameraSlot
    status: WireCameraStatus
    fps: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    quality: Confidence | None = None
    """Recent mean frame quality; None when the camera delivers no frames."""


class RejectedConfig(ApiModel):
    """A config bundle the client refused; the previous one stays active."""

    version: NonNegativeInt
    reason: Annotated[str, Field(max_length=500)]


class HeartbeatRequest(ApiModel):
    """`POST /heartbeat`, every 2 s while online."""

    seq: NonNegativeInt
    sent_at: UtcDatetime
    state: WireState
    confidence: Confidence | None = None
    low_confidence: bool = False
    worker_user_id: UUID | None = None
    session_id: UUID | None = None
    cameras: tuple[CameraHeartbeat, ...] = ()
    engine_status: WireEngineStatus
    queue_depth: NonNegativeInt = 0
    oldest_unsent_at: UtcDatetime | None = None
    disk_free_mb: NonNegativeInt | None = None
    clock_offset_ms: int = 0
    """Smoothed server-minus-device offset."""
    applied_config_version: NonNegativeInt | None = None
    """None before the first bundle has been applied."""
    shift_counters: StateCounts = Field(default_factory=dict)
    """Scans (or episodes) per state in the current shift; unknown keys dropped."""
    acked_commands: tuple[UUID, ...] = ()
    """Ids of commands handled since the previous heartbeat."""
    rejected_config: RejectedConfig | None = None
    """Set while the latest pulled bundle was refused (contract 4.2)."""


class CommandType(StrEnum):
    """Known heartbeat command types."""

    ASSIGN_WORKER = "assign_worker"
    END_SESSION = "end_session"
    LIVE_STILL = "live_still"
    LIVE_VIDEO = "live_video"
    PULL_CONFIG = "pull_config"
    ROTATE_SECRET = "rotate_secret"  # noqa: S105 - a command name, not a credential


class _CommandBase(ApiModel):
    id: UUID
    """Idempotency key; acknowledge it in `acked_commands`."""


class AssignWorkerCommand(_CommandBase):
    """Start a `supervisor_assigned` session."""

    type: Literal["assign_worker"] = "assign_worker"
    assignment_id: UUID
    user_id: UUID
    valid_until: UtcDatetime


class EndSessionCommand(_CommandBase):
    """End the worker session."""

    type: Literal["end_session"] = "end_session"
    session_id: UUID


class LiveStillCommand(_CommandBase):
    """Post one frame to `/live/{request_id}/frame`; never store it."""

    type: Literal["live_still"] = "live_still"
    request_id: UUID
    camera_slot: CameraSlot
    expires_at: UtcDatetime


class LiveVideoCommand(_CommandBase):
    """Open the live stream, only if the site setting allows it."""

    type: Literal["live_video"] = "live_video"
    session_id: UUID
    camera_slot: CameraSlot
    expires_at: UtcDatetime


class PullConfigCommand(_CommandBase):
    """Fetch the config bundle now."""

    type: Literal["pull_config"] = "pull_config"
    version: NonNegativeInt


class RotateSecretCommand(_CommandBase):
    """Call `/secret/rotate`."""

    type: Literal["rotate_secret"] = "rotate_secret"


class UnknownCommand(_CommandBase):
    """A command type this client does not know. It is logged and not executed."""

    type: str

    @model_validator(mode="after")
    def _log(self) -> Self:
        logger.warning("unknown command type %r (id %s) ignored", self.type[:40], self.id)
        return self


_KNOWN_COMMANDS = frozenset(CommandType)


def _command_tag(value: object) -> str:
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    if isinstance(kind, str) and kind in _KNOWN_COMMANDS and not isinstance(value, UnknownCommand):
        return kind
    return "unknown"


Command = Annotated[
    Annotated[AssignWorkerCommand, Tag("assign_worker")]
    | Annotated[EndSessionCommand, Tag("end_session")]
    | Annotated[LiveStillCommand, Tag("live_still")]
    | Annotated[LiveVideoCommand, Tag("live_video")]
    | Annotated[PullConfigCommand, Tag("pull_config")]
    | Annotated[RotateSecretCommand, Tag("rotate_secret")]
    | Annotated[UnknownCommand, Tag("unknown")],
    Discriminator(_command_tag),
]


class HeartbeatResponse(ApiModel):
    """Server time, the current config version and pending commands."""

    server_time: UtcDatetime
    config_version: NonNegativeInt
    commands: tuple[Command, ...] = ()
