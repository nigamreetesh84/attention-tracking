"""The portable service host: what the Windows service starts and stops.

It owns the engine supervisor and the housekeeper and drives both from one
tick thread. The IPC server runs inside the engine child (next to the
engine), so the host itself serves nothing; the UI and the scan flow connect
to the child through `IpcClient`.
"""

import logging
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

from ag_shared.api import EventType, LocalAlertPayload
from pydantic import BaseModel, JsonValue

from ag_workstation.clock import Clock
from ag_workstation.service.health import Housekeeper
from ag_workstation.service.supervisor import EngineSupervisor

logger = logging.getLogger(__name__)

TICK_S: Final = 0.25


class _Enqueue(Protocol):
    def enqueue(
        self,
        item_type: str,
        payload: BaseModel | dict[str, JsonValue],
        occurred_at: datetime,
    ) -> object: ...


class QueueLocalAlertSink:
    """Supervisor alert callback that queues a `local_alert` record."""

    def __init__(self, queue: _Enqueue, clock: Clock) -> None:
        self._queue = queue
        self._clock = clock

    def __call__(self, payload: LocalAlertPayload) -> None:
        """Enqueue the alert."""
        self._queue.enqueue(EventType.LOCAL_ALERT, payload, self._clock.now())


class ServiceHost:
    """Starts the supervisor and ticks it and the housekeeper until stopped."""

    def __init__(
        self,
        supervisor: EngineSupervisor,
        housekeeper: Housekeeper | None = None,
        *,
        tick_s: float = TICK_S,
    ) -> None:
        self.supervisor = supervisor
        self.housekeeper = housekeeper
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        """One round: supervisor poll, then due housekeeping. Never raises."""
        self.supervisor.poll()
        if self.housekeeper is not None:
            self.housekeeper.tick()

    @property
    def running(self) -> bool:
        """Whether the tick thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Launch the engine and start ticking."""
        if self._thread is not None:
            return
        self._stop.clear()
        self.supervisor.start()
        self._thread = threading.Thread(target=self._run, name="ag-service", daemon=True)
        self._thread.start()
        logger.info("service host started")

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            self.tick()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop ticking and kill the engine child."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.supervisor.stop()
        logger.info("service host stopped")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until `stop` is called; True if it was."""
        return self._stop.wait(timeout)


AUTHKEY_ENV: Final = "AG_IPC_AUTHKEY"
"""Hex IPC authkey shared by the service and the UI (storage and ACL: open, 2.3)."""


def default_service_host(env: Mapping[str, str] | None = None) -> ServiceHost:
    """The production host: the real engine child (`ProcessLauncher.real`) on
    the default pipe. Raises `ValueError` without a valid `AG_IPC_AUTHKEY`
    (a configuration error at service start, never on the scan path).

    Engine restart alerts go to the local queue when `AG_DATA_DIR` and a data
    key are configured (this process is the queue's sender, so it recovers
    records a crashed sender left `sending`); otherwise they are logged.
    """
    import os

    from ag_workstation.clock import SystemClock
    from ag_workstation.queue import EventQueue
    from ag_workstation.service.child import ProcessLauncher
    from ag_workstation.service.engine_factory import DATA_DIR_ENV, key_provider_from_env
    from ag_workstation.service.supervisor import AlertCallback
    from ag_workstation.service.transport import PipeTransport, default_address

    env = dict(os.environ) if env is None else env
    authkey = bytes.fromhex(env.get(AUTHKEY_ENV, "").strip())
    if len(authkey) < 16:
        raise ValueError(f"{AUTHKEY_ENV} must hold at least 16 bytes (hex)")
    clock = SystemClock()
    on_alert: AlertCallback | None = None
    data_raw = env.get(DATA_DIR_ENV, "").strip()
    keys = key_provider_from_env(env, Path(data_raw) if data_raw else None)
    if data_raw and keys is not None:
        try:
            queue = EventQueue.open(Path(data_raw) / "queue.db", keys, clock)
            on_alert = QueueLocalAlertSink(queue, clock)
        except Exception as exc:
            logger.error("local queue unavailable (%s); alerts are logged", type(exc).__name__)
    launcher = ProcessLauncher.real(PipeTransport(default_address(), authkey))
    return ServiceHost(EngineSupervisor(launcher, clock, on_alert=on_alert))
