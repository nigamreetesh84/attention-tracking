"""Runs one isolated `CameraWorker` per enabled camera of a station."""

import logging
import threading
from collections.abc import Callable, Sequence

from ag_shared.config import CameraConfig

from ag_workstation.capture.backoff import Backoff
from ag_workstation.capture.factory import make_source
from ag_workstation.capture.latest import LatestFrame
from ag_workstation.capture.types import CameraHealth, CameraSource, Frame
from ag_workstation.capture.worker import CameraWorker
from ag_workstation.clock import Clock, SystemClock

log = logging.getLogger(__name__)

ConfigSourceFactory = Callable[[CameraConfig], CameraSource]


class CaptureManager:
    """Owns the capture workers of a station.

    Each camera has its own thread, backoff and frame slot, so a failing or
    hung camera reports `offline` without affecting the others. A supervisor
    thread restarts hung workers. Disabled cameras get no worker.
    """

    def __init__(
        self,
        cameras: Sequence[CameraConfig],
        *,
        source_factory: ConfigSourceFactory | None = None,
        clock: Clock | None = None,
        backoff_factory: Callable[[], Backoff] = Backoff,
        stale_after_s: float = 5.0,
        supervise_interval_s: float = 1.0,
    ) -> None:
        slots = [c.slot for c in cameras]
        if len(set(slots)) != len(slots):
            raise ValueError("camera slots must be unique")
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._source_factory = source_factory
        self._supervise_interval = supervise_interval_s
        self._workers: dict[int, CameraWorker] = {}
        for config in sorted(cameras, key=lambda c: c.slot):
            if not config.is_enabled:
                continue
            self._workers[config.slot] = CameraWorker(
                slot=config.slot,
                source_factory=self._bind(config),
                clock=self._clock,
                backoff=backoff_factory(),
                stale_after_s=stale_after_s,
            )
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None

    def _bind(self, config: CameraConfig) -> Callable[[], CameraSource]:
        def factory() -> CameraSource:
            # Looked up per attempt, so a factory error is just a failed attempt.
            if self._source_factory is not None:
                return self._source_factory(config)
            return make_source(config)

        return factory

    @property
    def slots(self) -> tuple[int, ...]:
        """Slots with a worker, ascending."""
        return tuple(self._workers)

    def frames(self, slot: int) -> LatestFrame:
        """The latest-frame slot of camera `slot` (KeyError if not captured)."""
        return self._workers[slot].frames

    def latest(self, slot: int) -> Frame | None:
        """The newest frame of camera `slot`, without consuming it."""
        return self.frames(slot).peek()

    def health(self) -> dict[int, CameraHealth]:
        """Health of every captured camera, by slot."""
        return {slot: worker.health() for slot, worker in self._workers.items()}

    def step_all(self) -> None:
        """Step every worker once (tests and single-threaded replay)."""
        for worker in self._workers.values():
            worker.step()

    def supervise(self) -> None:
        """Restart workers that are stuck in a blocking call."""
        for worker in self._workers.values():
            worker.restart_if_hung()

    def start(self) -> None:
        """Start all workers and the supervisor (no-op when running)."""
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._stop.clear()
        for worker in self._workers.values():
            worker.start()
        self._supervisor = threading.Thread(
            target=self._supervise_loop, name="capture-supervisor", daemon=True
        )
        self._supervisor.start()

    def _supervise_loop(self) -> None:
        while not self._stop.wait(self._supervise_interval):
            try:
                self.supervise()
            except Exception as exc:  # the supervisor must outlive any bug
                log.error("capture supervisor error (%s)", type(exc).__name__)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the supervisor and every worker. Never raises."""
        self._stop.set()
        if self._supervisor is not None:
            self._supervisor.join(timeout)
            self._supervisor = None
        for worker in self._workers.values():
            worker.stop(timeout)
