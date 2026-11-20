"""Service health reporting and periodic housekeeping.

`HealthReporter.snapshot()` merges the supervisor's view of the engine
process, the engine's own health (cameras), queue stats and free disk into one
object the heartbeat (PLAN 2.9) and the health sample read. Every hook is
optional and guarded: a failing hook leaves its fields empty.

`Housekeeper` runs named jobs on intervals from the service tick, e.g.
dead-letter purge and evidence age purge (`standard_housekeeping`).
"""

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Protocol, TypeVar

from ag_shared.api import CameraHeartbeat, HealthPayload
from ag_shared.health import EngineStatus

from ag_workstation.clock import Clock
from ag_workstation.engine_api import CameraHealthSummary, EngineHealth
from ag_workstation.queue.store import DEAD_LETTER_RETENTION, QueueStats
from ag_workstation.service.supervisor import SupervisorHealthSource

logger = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL_S: Final = 3600.0
_MIB: Final = 1024 * 1024
_T = TypeVar("_T")
_LIVE: Final = frozenset({EngineStatus.STARTING, EngineStatus.RUNNING, EngineStatus.DEGRADED})


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """One health reading."""

    taken_at: datetime
    engine_status: EngineStatus
    engine_restarts: int
    engine_last_error: str | None
    cameras: tuple[CameraHealthSummary, ...] = ()
    queue: QueueStats | None = None
    disk_free_mb: int | None = None

    @property
    def monitoring_available(self) -> bool:
        """At least one camera online and the engine up."""
        return EngineHealth(self.engine_status, self.cameras).monitoring_available

    def camera_heartbeats(self) -> tuple[CameraHeartbeat, ...]:
        """Cameras in wire form."""
        return tuple(c.to_heartbeat() for c in self.cameras)

    def to_payload(self, *, app_version: str = "", clock_offset_ms: int = 0) -> HealthPayload:
        """The `health` queue item payload (cpu/mem are filled by 2.9)."""
        return HealthPayload(
            disk_free_mb=self.disk_free_mb,
            cameras=self.camera_heartbeats(),
            engine_status=self.engine_status,
            engine_restarts=self.engine_restarts,
            queue_depth=0 if self.queue is None else self.queue.depth,
            dead_letter_count=0 if self.queue is None else self.queue.dead_letter_count,
            clock_offset_ms=clock_offset_ms,
            app_version=app_version,
        )


def _guard(name: str, hook: Callable[[], _T] | None) -> _T | None:
    if hook is None:
        return None
    try:
        return hook()
    except Exception as exc:  # a broken hook blanks its field, nothing more
        logger.warning("health hook %s failed: %s", name, type(exc).__name__)
        return None


class HealthReporter:
    """Builds `HealthSnapshot`s from the supervisor and optional hooks."""

    def __init__(
        self,
        clock: Clock,
        supervisor: SupervisorHealthSource,
        *,
        engine_health: Callable[[], EngineHealth] | None = None,
        queue_stats: Callable[[], QueueStats] | None = None,
        disk_free_mb: Callable[[], int | None] | None = None,
    ) -> None:
        self._clock = clock
        self._supervisor = supervisor
        self._engine_health = engine_health
        self._queue_stats = queue_stats
        self._disk_free_mb = disk_free_mb

    def snapshot(self) -> HealthSnapshot:
        """Read everything now. Never raises."""
        sup = self._supervisor.health()
        status = sup.status
        cameras: tuple[CameraHealthSummary, ...] = ()
        if status is EngineStatus.RUNNING and self._engine_health is not None:
            engine = _guard("engine", self._engine_health)
            if engine is not None and engine.status in _LIVE:
                status, cameras = engine.status, engine.cameras
            else:
                # Process alive and beating, but the engine does not answer.
                status = EngineStatus.UNKNOWN
        return HealthSnapshot(
            taken_at=self._clock.now(),
            engine_status=status,
            engine_restarts=sup.restarts,
            engine_last_error=sup.last_error,
            cameras=cameras,
            queue=_guard("queue", self._queue_stats),
            disk_free_mb=_guard("disk", self._disk_free_mb),
        )


def disk_free_mb_hook(path: Path) -> Callable[[], int | None]:
    """A hook reporting free MiB on the volume holding `path`."""

    def free() -> int | None:
        return shutil.disk_usage(path).free // _MIB

    return free


@dataclass(slots=True)
class _Job:
    name: str
    interval_s: float
    run: Callable[[], object]
    due: float


class Housekeeper:
    """Runs named jobs every `interval_s` from `tick()`; a failing job is logged and retried."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._jobs: list[_Job] = []
        self.failures: dict[str, int] = {}

    def add(
        self, name: str, interval_s: float, run: Callable[[], object], *, run_first: bool = True
    ) -> None:
        """Register a job; with `run_first` it runs on the next tick."""
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        now = self._clock.monotonic()
        self._jobs.append(_Job(name, interval_s, run, now if run_first else now + interval_s))

    @property
    def jobs(self) -> tuple[str, ...]:
        """Registered job names."""
        return tuple(j.name for j in self._jobs)

    def tick(self) -> list[str]:
        """Run the jobs that are due; returns their names. Never raises."""
        now = self._clock.monotonic()
        ran: list[str] = []
        for job in self._jobs:
            if now < job.due:
                continue
            job.due = now + job.interval_s
            try:
                job.run()
            except Exception as exc:  # housekeeping must never stop the service
                self.failures[job.name] = self.failures.get(job.name, 0) + 1
                logger.warning("housekeeping %s failed: %s", job.name, type(exc).__name__)
                continue
            ran.append(job.name)
        return ran


class DeadLetterPurger(Protocol):
    """The part of `EventQueue` housekeeping needs."""

    def purge_dead_letters(self, retention: timedelta = DEAD_LETTER_RETENTION) -> int:
        """Delete old dead letters."""
        ...


class EvidenceHousekeeping(Protocol):
    """The part of `EvidenceStore` housekeeping needs."""

    def housekeeping(self) -> object:
        """Age purge then quota enforcement."""
        ...


def standard_housekeeping(
    clock: Clock,
    *,
    queue: DeadLetterPurger | None = None,
    evidence: EvidenceHousekeeping | None = None,
    interval_s: float = HOUSEKEEPING_INTERVAL_S,
) -> Housekeeper:
    """Dead-letter purge and evidence age purge (plus quota) every `interval_s`."""
    keeper = Housekeeper(clock)
    if queue is not None:
        keeper.add("purge_dead_letters", interval_s, queue.purge_dead_letters)
    if evidence is not None:
        keeper.add("evidence_housekeeping", interval_s, evidence.housekeeping)
    return keeper
