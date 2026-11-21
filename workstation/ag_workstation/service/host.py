"""The portable service host: what the Windows service starts and stops.

It owns the engine supervisor and the housekeeper and drives both from one
tick thread. The IPC server runs inside the engine child (next to the
engine), so the host itself serves nothing; the UI and the scan flow connect
to the child through `IpcClient`.
"""

import logging
import threading
from collections.abc import Callable, Mapping
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


class BackgroundService(Protocol):
    """A component with its own thread (the sync client, PLAN 2.9)."""

    def start(self) -> None:
        """Start its thread; must return at once."""
        ...

    def stop(self, timeout: float = 2.0) -> None:
        """Stop within about `timeout` seconds."""
        ...


class ServiceHost:
    """Starts the supervisor and ticks it and the housekeeper until stopped.

    `sync` runs on its own thread: the tick thread never waits for the
    network, so a hung server cannot delay engine supervision.
    """

    def __init__(
        self,
        supervisor: EngineSupervisor,
        housekeeper: Housekeeper | None = None,
        *,
        tick_s: float = TICK_S,
        sync: BackgroundService | None = None,
        closers: tuple[Callable[[], None], ...] = (),
    ) -> None:
        self.supervisor = supervisor
        self.housekeeper = housekeeper
        self.sync = sync
        self._closers = closers
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
        if self.sync is not None:
            try:
                self.sync.start()
            except Exception as exc:  # sync is optional; supervision goes on
                logger.error("sync did not start (%s)", type(exc).__name__)
        logger.info("service host started")

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            self.tick()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop sync and ticking, kill the engine child, close resources."""
        if self.sync is not None:
            try:
                self.sync.stop(timeout)
            except Exception as exc:
                logger.warning("sync did not stop cleanly (%s)", type(exc).__name__)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.supervisor.stop()
        for close in reversed(self._closers):
            try:
                close()
            except Exception as exc:
                logger.warning("closing a service resource failed (%s)", type(exc).__name__)
        logger.info("service host stopped")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until `stop` is called; True if it was."""
        return self._stop.wait(timeout)


AUTHKEY_ENV: Final = "AG_IPC_AUTHKEY"
"""Development override of the IPC authkey (hex); production uses the DPAPI key
file `AG_DATA_DIR/keys/ipc.key` (`service.ipc_key`, 2.R)."""


def default_service_host(env: Mapping[str, str] | None = None) -> ServiceHost:
    """The production host: the real engine child (`ProcessLauncher.real`) on
    the default pipe. Raises `ValueError` without a usable IPC authkey
    (`AG_IPC_AUTHKEY`, else the DPAPI key file under `AG_DATA_DIR` on Windows;
    a configuration error at service start, never on the scan path).

    With `AG_DATA_DIR` and a data key: engine restart alerts go to the local
    queue (this process is the queue's single sender, so it recovers records
    a crashed sender left `sending`), housekeeping runs, and the sync client
    runs on its own thread when `AG_SERVER_URL` is set (PLAN 2.9). Without
    them alerts are only logged.
    """
    import os

    from ag_workstation.clock import SystemClock
    from ag_workstation.corrected_time import stamping_clock
    from ag_workstation.queue import EventQueue
    from ag_workstation.service.child import ProcessLauncher
    from ag_workstation.service.engine_factory import DATA_DIR_ENV, key_provider_from_env
    from ag_workstation.service.ipc_client import IpcClient
    from ag_workstation.service.ipc_key import ipc_authkey
    from ag_workstation.service.supervisor import AlertCallback
    from ag_workstation.service.transport import PipeTransport, default_address
    from ag_workstation.sync.wiring import service_components

    env = dict(os.environ) if env is None else env
    data_raw = env.get(DATA_DIR_ENV, "").strip()
    data_dir = Path(data_raw) if data_raw else None
    authkey = ipc_authkey(env, data_dir)
    if authkey is None:
        raise ValueError(f"no IPC authkey: set {AUTHKEY_ENV} or a usable key file")
    clock = SystemClock()
    on_alert: AlertCallback | None = None
    keys = key_provider_from_env(env, data_dir)
    queue: EventQueue | None = None
    if data_dir is not None and keys is not None:
        try:
            queue = EventQueue.open(data_dir / "queue.db", keys, clock)
            # Alerts carry corrected UTC (contract 6); the supervisor times on the raw clock.
            on_alert = QueueLocalAlertSink(queue, stamping_clock(clock, data_dir))
        except Exception as exc:
            logger.error("local queue unavailable (%s); alerts are logged", type(exc).__name__)
    launcher = ProcessLauncher.real(PipeTransport(default_address(), authkey))
    supervisor = EngineSupervisor(launcher, clock, on_alert=on_alert)
    if queue is None or data_dir is None or keys is None:
        logger.warning("no local data: sync and housekeeping are off")
        return ServiceHost(supervisor)
    # A second client: status reads for the heartbeat never share a
    # connection with anything on the scan path.
    state = IpcClient(PipeTransport(default_address(), authkey), clock, timeout_s=0.5)
    parts = service_components(
        env,
        data_dir=data_dir,
        keys=keys,
        queue=queue,
        clock=clock,
        supervisor=supervisor,
        state=state,
        engine_health=state.health,
    )
    return ServiceHost(
        supervisor,
        parts.housekeeper,
        sync=parts.sync,
        closers=(queue.close, state.close, *parts.closers),
    )
