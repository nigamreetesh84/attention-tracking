"""Deterministic fakes of the engine-facing interfaces (`ag_workstation.engine_api`).

For tests and CI only; production code never imports this package.
"""

from ag_workstation.fakes.engine import (
    FAKE_ENGINE_VERSION,
    BurstRequest,
    Delayed,
    FakeStationEngine,
    ScriptStep,
)
from ag_workstation.fakes.evidence import BurstBuilder, RecordingEvidenceSink, fake_jpeg

__all__ = [
    "FAKE_ENGINE_VERSION",
    "BurstBuilder",
    "BurstRequest",
    "Delayed",
    "FakeStationEngine",
    "RecordingEvidenceSink",
    "ScriptStep",
    "fake_jpeg",
]
