"""Engine factories for the service child process (tests, and until PLAN 1.8 lands)."""

from ag_workstation.clock import SystemClock
from ag_workstation.engine_api import EngineEventListener
from ag_workstation.fakes.engine import FakeStationEngine


def fake_engine_factory(listener: EngineEventListener) -> FakeStationEngine:
    """A `FakeStationEngine` on the system clock, publishing to `listener`.

    Referenced by path (`ag_workstation.fakes.service:fake_engine_factory`)
    so a spawned child can import it.
    """
    return FakeStationEngine(SystemClock(), listener=listener)
