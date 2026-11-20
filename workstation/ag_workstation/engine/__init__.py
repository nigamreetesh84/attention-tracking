"""Detection engine logic after signal extraction (`docs/detection-engine.md`).

Per-camera estimation, multi-camera fusion, the station state machine and
the runtime that wires them to the capture and landmark layers. Everything
except the runtime's threads is pure Python over typed data with timestamps
taken from the frames, so it runs deterministically in CI and in replays.
"""

from ag_workstation.engine.estimator import CameraEstimator, EstimatorParams
from ag_workstation.engine.timers import ConditionTimer, EdgeCounter, ShareWindow
from ag_workstation.engine.types import CameraCandidate, FusedState

__all__ = [
    "CameraCandidate",
    "CameraEstimator",
    "ConditionTimer",
    "EdgeCounter",
    "EstimatorParams",
    "FusedState",
    "ShareWindow",
]
