"""A fake engine factory for the service child process (tests and development only;
the service default is `service.engine_factory.real_engine_factory`)."""

import time
from typing import Final

from ag_workstation.clock import SystemClock
from ag_workstation.engine_api import EngineEventListener
from ag_workstation.fakes.engine import FakeStationEngine


def fake_engine_factory(listener: EngineEventListener) -> FakeStationEngine:
    """A `FakeStationEngine` on the system clock, publishing to `listener`.

    Referenced by path (`ag_workstation.fakes.service:fake_engine_factory`)
    so a spawned child can import it.
    """
    return FakeStationEngine(SystemClock(), listener=listener)


STALL_AFTER_S: Final = 0.5
"""How long a `stalling_engine_factory` engine stays alive (read at creation)."""


class StallingStationEngine(FakeStationEngine):
    """A fake engine whose tick "dies" `STALL_AFTER_S` after creation: it keeps
    answering (Unknown would be the real engine's answer) but `alive()` turns
    False, as `EngineAdapter.alive` does for a dead tick thread."""

    def __init__(self, listener: EngineEventListener, stall_after_s: float) -> None:
        super().__init__(SystemClock(), listener=listener)
        self._dies_at = time.monotonic() + stall_after_s

    def alive(self) -> bool:
        """False once the simulated tick thread has stopped."""
        return time.monotonic() < self._dies_at


def stalling_engine_factory(listener: EngineEventListener) -> StallingStationEngine:
    """A child whose engine stalls (2.11 fix tests: the supervisor must restart it)."""
    return StallingStationEngine(listener, STALL_AFTER_S)
