"""What the sync client reports and what it reads for the heartbeat."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ag_shared.api import CameraHeartbeat, RejectedConfig
from ag_shared.health import EngineStatus
from ag_shared.states import AttentionState


@dataclass(frozen=True, slots=True)
class StationStatus:
    """Live station status for one heartbeat (contract 4.1)."""

    state: AttentionState = AttentionState.UNKNOWN
    confidence: float | None = None
    low_confidence: bool = False
    worker_user_id: UUID | None = None
    session_id: UUID | None = None
    cameras: tuple[CameraHeartbeat, ...] = ()
    engine_status: EngineStatus = EngineStatus.UNKNOWN
    disk_free_mb: int | None = None
    shift_counters: dict[AttentionState, int] = field(default_factory=dict)


class StatusSource(Protocol):
    """Where the heartbeat's live fields come from."""

    def station_status(self) -> StationStatus:
        """Read now. Should be quick; an exception means "Unknown" (it is caught)."""
        ...


class SyncFlag(StrEnum):
    """Problems the sync client reports to station health."""

    NOT_ENROLLED = "not_enrolled"
    OFFLINE = "server_unreachable"
    """No usable response (timeout, connection refused, TLS)."""
    SERVER_ERROR = "server_error"
    """429 or 5xx, or a response the client could not read."""
    UPDATE_REQUIRED = "update_required"
    """426: the server no longer supports this schema; data is buffered."""
    DEVICE_REVOKED = "device_revoked"
    AUTH_FAILED = "auth_failed"
    """401 that a fresh token did not cure."""
    FORBIDDEN = "forbidden"
    """403 on a request (other than an image's consent_missing)."""
    CLOCK_DRIFT = "clock_drift"
    CONFIG_REJECTED = "config_rejected"
    CREDENTIAL_STORE_FAILED = "credential_store_failed"
    SYNC_ERROR = "sync_error"
    """An unexpected exception in the sync loop (caught at the boundary)."""


@dataclass(frozen=True, slots=True)
class SyncHealth:
    """A snapshot for the service's health reporting."""

    flags: frozenset[SyncFlag]
    last_contact_at: datetime | None
    """Device wall time of the last answer from the server."""
    clock_offset_ms: int
    applied_config_version: int | None
    rejected_config: RejectedConfig | None
    evidence_rejected: int
    """Images the server refused permanently this run (409 or other 4xx)."""
