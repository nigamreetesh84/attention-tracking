"""Assembling the sync client in the service parent.

`build_sync_service` takes everything as arguments (tests pass a fake
transport); `sync_service_from_env` reads the service environment:

- `AG_SERVER_URL`: the server's base URL (https). Unset: no sync, the
  station works offline (logged once).
- `AG_SERVER_CA`: optional CA bundle for the site's server certificate.

Files under the data directory: `queue.db` (opened by the caller),
`evidence/` (opened here, before the engine child starts, because opening
sweeps temporary files), `config/bundle.agc` (the last good bundle),
`device.agk` (sealed device credentials) and `commands.db` (session
commands for the UI).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import metadata
from pathlib import Path
from typing import Final, Protocol

from ag_shared.api import EventType

from ag_workstation.clock import Clock
from ag_workstation.engine_api import EngineHealth, StationState
from ag_workstation.evidence.store import EvidenceStore
from ag_workstation.queue.keys import KeyProvider
from ag_workstation.queue.store import EventQueue
from ag_workstation.service.health import (
    HOUSEKEEPING_INTERVAL_S,
    HealthReporter,
    Housekeeper,
    disk_free_mb_hook,
    standard_housekeeping,
)
from ag_workstation.service.supervisor import SupervisorHealthSource
from ag_workstation.sync.api import DeviceApi
from ag_workstation.sync.client import SyncClient, SyncEvidence
from ag_workstation.sync.clock_offset import ClockOffsetEstimator
from ag_workstation.sync.commands import INBOX_FILE, CommandInbox
from ag_workstation.sync.config_store import ConfigStore, bundle_path
from ag_workstation.sync.credentials import CredentialStore, SealedFileCredentialStore
from ag_workstation.sync.runner import SyncRunner
from ag_workstation.sync.status import StationStatus
from ag_workstation.sync.transport import HttpxTransport, SyncTransport

logger = logging.getLogger(__name__)

SERVER_URL_ENV: Final = "AG_SERVER_URL"
CA_BUNDLE_ENV: Final = "AG_SERVER_CA"
CREDENTIALS_FILE: Final = "device.agk"
HEALTH_SAMPLE_S: Final = 60.0

__all__ = [
    "CA_BUNDLE_ENV",
    "CREDENTIALS_FILE",
    "HEALTH_SAMPLE_S",
    "SERVER_URL_ENV",
    "HealthSampler",
    "ServiceComponents",
    "ServiceStatusSource",
    "SyncService",
    "app_version",
    "build_sync_service",
    "service_components",
    "sync_service_from_env",
]


def app_version() -> str:
    """The installed package version (`X-AG-App-Version`)."""
    try:
        return metadata.version("attentionguard")
    except metadata.PackageNotFoundError:  # pragma: no cover - always installed in practice
        return "0.0.0"


class CurrentStateSource(Protocol):
    """`IpcClient.current_state` (never raises, Unknown on failure)."""

    def current_state(self) -> StationState:
        """The station's current state."""
        ...


class ServiceStatusSource:
    """Heartbeat fields from the service's health reporter and the engine child.

    Worker and session ids and shift counters are not available in the
    parent yet (no IPC op carries them); they are sent empty (PROGRESS 2.9).
    """

    def __init__(self, reporter: HealthReporter, state: CurrentStateSource | None) -> None:
        self._reporter = reporter
        self._state = state

    def station_status(self) -> StationStatus:
        """Read the health snapshot and, if the engine is up, its current state."""
        snap = self._reporter.snapshot()
        if self._state is None:
            return StationStatus(
                cameras=snap.camera_heartbeats(),
                engine_status=snap.engine_status,
                disk_free_mb=snap.disk_free_mb,
            )
        current = self._state.current_state()
        return StationStatus(
            state=current.state,
            confidence=current.confidence,
            low_confidence=current.low_confidence,
            cameras=snap.camera_heartbeats(),
            engine_status=snap.engine_status,
            disk_free_mb=snap.disk_free_mb,
        )


class HealthSampler:
    """Queues the one-minute `health` item (contract 4.3) from the health reporter."""

    def __init__(
        self,
        reporter: HealthReporter,
        queue: EventQueue,
        clock: Clock,
        *,
        offset: Callable[[], int] = lambda: 0,
    ) -> None:
        self._reporter = reporter
        self._queue = queue
        self._clock = clock
        self._offset = offset
        self._version = app_version()

    def __call__(self) -> None:
        """Take one sample and queue it."""
        payload = self._reporter.snapshot().to_payload(
            app_version=self._version, clock_offset_ms=self._offset()
        )
        self._queue.enqueue(EventType.HEALTH, payload, self._clock.now())


@dataclass(slots=True)
class SyncService:
    """The sync client, its thread and what to close."""

    client: SyncClient
    runner: SyncRunner
    inbox: CommandInbox
    config_store: ConfigStore
    closers: list[Callable[[], None]] = field(default_factory=list)

    def start(self) -> None:
        """Start the sync thread."""
        self.runner.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the thread (bounded) and close files."""
        self.runner.stop(timeout)
        for close in reversed(self.closers):
            try:
                close()
            except Exception as exc:
                logger.warning("closing sync resource failed (%s)", type(exc).__name__)

    def purge(self, retention: timedelta = timedelta(days=7)) -> int:
        """Housekeeping: drop old acknowledged commands."""
        return self.inbox.purge(retention)


def build_sync_service(
    *,
    data_dir: Path,
    keys: KeyProvider,
    queue: EventQueue,
    clock: Clock,
    transport: SyncTransport,
    credentials: CredentialStore | None = None,
    evidence: SyncEvidence | None = None,
    status: ServiceStatusSource | None = None,
    offset: ClockOffsetEstimator | None = None,
) -> SyncService:
    """Wire a `SyncService` (not started)."""
    offset = offset or ClockOffsetEstimator()
    credentials = credentials or SealedFileCredentialStore(data_dir / CREDENTIALS_FILE, keys)
    api = DeviceApi(transport, credentials, clock, offset, app_version=app_version())
    store = ConfigStore(bundle_path(data_dir), keys)
    try:
        enrolled = credentials.load()
    except Exception as exc:
        logger.error("device credentials unreadable (%s)", type(exc).__name__)
        enrolled = None
    if enrolled is not None:
        store.set_workstation_id(enrolled.workstation_id)
    inbox = CommandInbox.open(data_dir / INBOX_FILE, clock)
    client = SyncClient(
        api=api,
        queue=queue,
        config_store=store,
        clock=clock,
        offset=offset,
        evidence=evidence,
        inbox=inbox,
        status=status,
    )
    return SyncService(
        client=client,
        runner=SyncRunner(client),
        inbox=inbox,
        config_store=store,
        closers=[transport.close, inbox.close],
    )


def sync_service_from_env(
    env: Mapping[str, str],
    *,
    data_dir: Path,
    keys: KeyProvider,
    queue: EventQueue,
    clock: Clock,
    evidence: SyncEvidence | None = None,
    status: ServiceStatusSource | None = None,
) -> SyncService | None:
    """The production sync service, or None when `AG_SERVER_URL` is unset or invalid.

    Never raises: sync is optional; the station works offline without it.
    """
    url = env.get(SERVER_URL_ENV, "").strip()
    if not url:
        logger.warning("%s is not set; the station runs offline", SERVER_URL_ENV)
        return None
    ca_raw = env.get(CA_BUNDLE_ENV, "").strip()
    try:
        transport = HttpxTransport(url, ca_bundle=Path(ca_raw) if ca_raw else None)
    except (ValueError, OSError) as exc:
        logger.error("sync disabled: server settings invalid (%s)", type(exc).__name__)
        return None
    try:
        return build_sync_service(
            data_dir=data_dir,
            keys=keys,
            queue=queue,
            clock=clock,
            transport=transport,
            evidence=evidence,
            status=status,
        )
    except Exception as exc:
        logger.error("sync disabled (%s)", type(exc).__name__)
        transport.close()
        return None


@dataclass(slots=True)
class ServiceComponents:
    """What the service parent runs besides the supervisor."""

    sync: SyncService | None
    housekeeper: Housekeeper
    closers: list[Callable[[], None]] = field(default_factory=list)


def service_components(
    env: Mapping[str, str],
    *,
    data_dir: Path,
    keys: KeyProvider,
    queue: EventQueue,
    clock: Clock,
    supervisor: SupervisorHealthSource,
    state: CurrentStateSource | None = None,
    engine_health: Callable[[], EngineHealth] | None = None,
) -> ServiceComponents:
    """Sync (if configured), housekeeping (dead letters, evidence age and
    quota, old commands) and the one-minute health sample. Never raises.

    The evidence store is opened here, before the engine child starts,
    because opening it sweeps temporary files.
    """
    closers: list[Callable[[], None]] = []
    evidence: EvidenceStore | None = None
    try:
        evidence = EvidenceStore.open(data_dir / "evidence", keys, clock)
        closers.append(evidence.close)
    except Exception as exc:
        logger.error("evidence store unavailable to the service (%s)", type(exc).__name__)
    reporter = HealthReporter(
        clock,
        supervisor,
        engine_health=engine_health,
        queue_stats=queue.stats,
        disk_free_mb=disk_free_mb_hook(data_dir),
    )
    sync = sync_service_from_env(
        env,
        data_dir=data_dir,
        keys=keys,
        queue=queue,
        clock=clock,
        evidence=evidence,
        status=ServiceStatusSource(reporter, state),
    )
    keeper = standard_housekeeping(clock, queue=queue, evidence=evidence)
    offset = (lambda: sync.client.health().clock_offset_ms) if sync is not None else (lambda: 0)
    keeper.add(
        "health_sample",
        HEALTH_SAMPLE_S,
        HealthSampler(reporter, queue, clock, offset=offset),
        run_first=False,
    )
    if sync is not None:
        keeper.add("purge_commands", HOUSEKEEPING_INTERVAL_S, sync.purge)
    return ServiceComponents(sync=sync, housekeeper=keeper, closers=closers)
