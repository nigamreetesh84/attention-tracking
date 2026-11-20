"""Detection engine logic after signal extraction (`docs/detection-engine.md`).

Per-camera estimation, multi-camera fusion, the station state machine and
the runtime that wires them to the capture and landmark layers. Everything
except the runtime's threads is pure Python over typed data with timestamps
taken from the frames, so it runs deterministically in CI and in replays.
"""

from ag_workstation.engine.calendar import CalendarStatus, ShiftCalendar
from ag_workstation.engine.estimator import CameraEstimator, EstimatorParams
from ag_workstation.engine.fusion import DegradedTracker, fuse
from ag_workstation.engine.state_machine import (
    Bucket,
    Episode,
    MachineOutput,
    ScanDecision,
    StationStateMachine,
    StationStatus,
)
from ag_workstation.engine.timers import ConditionTimer, EdgeCounter, ShareWindow
from ag_workstation.engine.types import CameraCandidate, FusedState

__all__ = [
    "Bucket",
    "CalendarStatus",
    "CameraCandidate",
    "CameraEstimator",
    "ConditionTimer",
    "DegradedTracker",
    "EdgeCounter",
    "Episode",
    "EstimatorParams",
    "FusedState",
    "MachineOutput",
    "ScanDecision",
    "ShareWindow",
    "ShiftCalendar",
    "StationStateMachine",
    "StationStatus",
    "fuse",
]
