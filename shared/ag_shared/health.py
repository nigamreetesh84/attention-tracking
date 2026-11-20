"""Status enums reported in heartbeats and health samples.

`CameraStatus` was defined in `ag_workstation.capture.types` (task 1.3); it
lives here now because the heartbeat payload carries it, and the capture
layer re-exports it unchanged.
"""

from enum import StrEnum


class CameraStatus(StrEnum):
    """Status of one camera as reported in health and heartbeats."""

    STARTING = "starting"
    """Not yet delivered a frame and not yet failed."""
    ONLINE = "online"
    OFFLINE = "offline"
    """Failed, hung or stale; reconnecting with backoff."""
    STOPPED = "stopped"
    """Stopped on request or at the end of a finite stream."""


class EngineStatus(StrEnum):
    """Status of the detection engine process (PLAN 1.8, 2.3)."""

    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    """Running, but at least one camera is offline or below quality."""
    RESTARTING = "restarting"
    DOWN = "down"
    """Not answering; the station state is Unknown."""
    UNKNOWN = "unknown"
    """The reporter could not tell (also the default for unknown wire values)."""
