"""Engine supervisor: keeps the engine child process alive.

The engine runs as a child behind `ChildLauncher` / `ChildProcess`. The child
bumps a heartbeat counter; the supervisor polls it. A child that exited, or
whose counter has not moved for `heartbeat_timeout_s`, is killed and
relaunched after a capped backoff, so monitoring is back within
`restart_deadline_s` (5 s, scope 4.4) of the failure being detectable. Each
failure raises an `engine_restart` local alert (rate-limited while flapping).

All timing comes from the injected clock; `poll()` does the work and is
driven by the service host's tick (or `run_forever` for a dedicated thread).
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from ag_shared.api import LocalAlertKind, LocalAlertPayload
from ag_shared.health import EngineStatus
from pydantic import JsonValue

from ag_workstation.clock import Clock

logger = logging.getLogger(__name__)

RESTART_DEADLINE_S: Final = 5.0


class ChildProcess(Protocol):
    """A running engine child."""

    @property
    def pid(self) -> int | None:
        """OS process id, if any."""
        ...

    @property
    def exitcode(self) -> int | None:
        """Exit code once it has ended, else None."""
        ...

    def is_alive(self) -> bool:
        """Whether the process is still running."""
        ...

    def heartbeat(self) -> int:
        """A counter the child increments while healthy."""
        ...

    def kill(self) -> None:
        """Stop it hard and reap it. Must not raise."""
        ...


class ChildLauncher(Protocol):
    """Starts engine children."""

    def launch(self) -> ChildProcess:
        """Start a new child. May raise; the supervisor retries with backoff."""
        ...


class FailureReason(StrEnum):
    """Why the engine was restarted (alert detail)."""

    EXITED = "exited"
    HUNG = "hung"
    STARTUP_TIMEOUT = "startup_timeout"
    LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Timing. Defaults keep detection + backoff + poll within 5 s."""

    poll_interval_s: float = 0.25
    heartbeat_timeout_s: float = 2.0
    """Running child with a frozen counter for this long is hung."""
    startup_timeout_s: float = 15.0
    """First heartbeat must arrive within this (model loading is slow)."""
    backoff_initial_s: float = 0.5
    """Delay before the second consecutive restart; the first is immediate."""
    backoff_max_s: float = 2.5
    stable_after_s: float = 30.0
    """Running this long without failing resets the backoff."""
    alert_interval_s: float = 60.0
    """At most one `engine_restart` alert per interval; the rest are counted."""

    def __post_init__(self) -> None:
        if min(self.poll_interval_s, self.heartbeat_timeout_s, self.startup_timeout_s) <= 0:
            raise ValueError("intervals must be positive")
        if self.backoff_initial_s < 0 or self.backoff_max_s < self.backoff_initial_s:
            raise ValueError("backoff must be 0 <= initial <= max")
        worst = self.heartbeat_timeout_s + self.backoff_max_s + self.poll_interval_s
        if worst > RESTART_DEADLINE_S:
            raise ValueError(f"hang detection + backoff exceeds {RESTART_DEADLINE_S} s")


@dataclass(frozen=True, slots=True)
class SupervisorHealth:
    """What the supervisor knows about the engine process."""

    status: EngineStatus
    restarts: int
    last_error: str | None
    consecutive_failures: int
    pid: int | None


class SupervisorHealthSource(Protocol):
    """Read supervisor health."""

    def health(self) -> SupervisorHealth:
        """A snapshot; never raises."""
        ...


AlertCallback = Callable[[LocalAlertPayload], None]


class EngineSupervisor:
    """Launches, watches and relaunches the engine child. Thread-safe."""

    def __init__(
        self,
        launcher: ChildLauncher,
        clock: Clock,
        *,
        on_alert: AlertCallback | None = None,
        config: SupervisorConfig | None = None,
    ) -> None:
        self._launcher = launcher
        self._clock = clock
        self._on_alert = on_alert
        self.config = config or SupervisorConfig()
        self._lock = threading.RLock()
        self._child: ChildProcess | None = None
        self._status = EngineStatus.DOWN
        self._started_at = 0.0
        self._last_beat = 0
        self._last_beat_at = 0.0
        self._seen_beat = False
        self._restart_at: float | None = None
        self._restarts = 0
        self._failures = 0
        self._last_error: str | None = None
        self._last_alert_at: float | None = None
        self._suppressed_alerts = 0
        self._active = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Launch the first child (failures are retried by `poll`)."""
        with self._lock:
            if self._active:
                return
            self._active = True
            self._launch(first=True)

    def stop(self) -> None:
        """Kill the child and stop supervising."""
        with self._lock:
            self._active = False
            self._restart_at = None
            child, self._child = self._child, None
            self._status = EngineStatus.DOWN
        if child is not None:
            _kill(child)

    def run_forever(self, stop: threading.Event) -> None:
        """Poll every `poll_interval_s` until `stop` is set (dedicated thread)."""
        while not stop.wait(self.config.poll_interval_s):
            self.poll()

    # -- the watch ----------------------------------------------------------

    def poll(self) -> None:
        """Check the child and restart it when due. Never raises."""
        try:
            with self._lock:
                self._poll()
        except Exception as exc:  # the supervisor itself must keep running
            logger.exception("supervisor poll failed: %s", type(exc).__name__)

    def _poll(self) -> None:
        if not self._active:
            return
        now = self._clock.monotonic()
        child = self._child
        if child is None:
            if self._restart_at is not None and now >= self._restart_at:
                self._launch(first=False)
            return
        if not child.is_alive():
            self._fail(FailureReason.EXITED, f"exit code {child.exitcode}")
            return
        beat = child.heartbeat()
        if beat != self._last_beat:
            self._last_beat, self._last_beat_at = beat, now
            if not self._seen_beat:
                self._seen_beat = True
                self._status = EngineStatus.RUNNING
        if self._seen_beat:
            if now - self._last_beat_at > self.config.heartbeat_timeout_s:
                self._fail(FailureReason.HUNG, "heartbeat stopped")
                return
            if self._failures and now - self._started_at >= self.config.stable_after_s:
                self._failures = 0
        elif now - self._started_at > self.config.startup_timeout_s:
            self._fail(FailureReason.STARTUP_TIMEOUT, "no heartbeat after start")

    def _launch(self, *, first: bool) -> None:
        self._restart_at = None
        if not first:
            self._restarts += 1
        try:
            child = self._launcher.launch()
        except Exception as exc:  # retried with backoff
            self._child = None
            self._fail(FailureReason.LAUNCH_FAILED, type(exc).__name__)
            return
        now = self._clock.monotonic()
        self._child = child
        self._started_at = self._last_beat_at = now
        self._last_beat = child.heartbeat()
        self._seen_beat = False
        self._status = EngineStatus.STARTING
        logger.info("engine child started (pid %s)", child.pid)

    def _fail(self, reason: FailureReason, error: str) -> None:
        child, self._child = self._child, None
        if child is not None:
            _kill(child)
        self._failures += 1
        self._last_error = reason.value
        delay = self.backoff_delay(self._failures)
        self._restart_at = self._clock.monotonic() + delay
        self._status = EngineStatus.RESTARTING
        logger.warning("engine %s (%s); restart in %.1f s", reason.value, error, delay)
        self._alert(reason)
        if delay == 0 and self._active:
            self._launch(first=False)

    def backoff_delay(self, failures: int) -> float:
        """Delay before the restart after `failures` consecutive failures."""
        if failures <= 1:
            return 0.0
        cfg = self.config
        return float(min(cfg.backoff_max_s, cfg.backoff_initial_s * 2 ** (failures - 2)))

    def _alert(self, reason: FailureReason) -> None:
        if self._on_alert is None:
            return
        now = self._clock.monotonic()
        last = self._last_alert_at
        if last is not None and now - last < self.config.alert_interval_s:
            self._suppressed_alerts += 1
            return
        detail: dict[str, JsonValue] = {
            "reason": reason.value,
            "restarts": self._restarts + 1,
            "consecutive_failures": self._failures,
            "suppressed": self._suppressed_alerts,
        }
        try:
            self._on_alert(LocalAlertPayload(kind=LocalAlertKind.ENGINE_RESTART, detail=detail))
        except Exception as exc:  # an alert failure must not stop the restart
            logger.warning("engine_restart alert failed: %s", type(exc).__name__)
            return
        self._last_alert_at = now
        self._suppressed_alerts = 0

    # -- health -------------------------------------------------------------

    def health(self) -> SupervisorHealth:
        """Status, restart count and last failure reason."""
        with self._lock:
            return SupervisorHealth(
                status=self._status,
                restarts=self._restarts,
                last_error=self._last_error,
                consecutive_failures=self._failures,
                pid=None if self._child is None else self._child.pid,
            )


def _kill(child: ChildProcess) -> None:
    try:
        child.kill()
    except Exception as exc:  # kill() must not raise, but be safe
        logger.warning("engine child kill failed: %s", type(exc).__name__)
