"""Assembling the sync client in the service parent.

`build_sync_service` takes everything as arguments (tests pass a fake
transport); `sync_service_from_env` reads the service environment:

- `AG_SERVER_URL`: the server's base URL (https). Unset: no sync, the
  station works offline (logged once).
- `AG_SERVER_CA`: optional CA bundle for the site's server certificate.

Files under the data directory: `queue.db` (opened by the caller),
`evidence/` (opened here, before the engine child starts, because opening
sweeps temporary files), `config/bundle.agc` (the last good bundle),
`service/device.agk` (device credentials), `commands.db` (session
commands for the UI) and `clock_offset.json` (the server clock offset for
the UI and the engine child, `corrected_time`; plain, it is not secret).

Keys (task 3.0): the queue, the bundle and the inbox use the queue data key
the UI also holds; evidence files and the device credentials use the
service-only key (`service.service_key`). Without the service key the
service still runs: no evidence store (images withheld, nothing uploaded),
the sync client cannot authenticate (`service_key_unavailable`), and
`ServiceComponents.degraded` names it. On the first start with both keys,
pre-3.0 evidence files and `device.agk` are re-sealed under the service key.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import metadata
from pathlib import Path
from typing import Final, Protocol

from ag_shared.api import EventType, LocalAlertPayload

from ag_workstation.clock import Clock
from ag_workstation.corrected_time import OffsetPublisher, offset_path, stamping_clock
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
from ag_workstation.service.monitoring_alerts import CHECK_INTERVAL_S, MonitoringAlerter
from ag_workstation.service.service_key import (
    SERVICE_DIR,
    SERVICE_KEY_UNAVAILABLE,
    service_key_provider,
    usable,
)
from ag_workstation.service.supervisor import SupervisorHealthSource
from ag_workstation.sync.api import DeviceApi
from ag_workstation.sync.client import SyncClient, SyncEvidence
from ag_workstation.sync.clock_offset import ClockOffsetEstimator
from ag_workstation.sync.commands import INBOX_FILE, CommandInbox
from ag_workstation.sync.config_store import ConfigStore, bundle_path
from ag_workstation.sync.credentials import (
    CredentialStore,
    SealedFileCredentialStore,
    UnavailableCredentialStore,
)
from ag_workstation.sync.runner import SyncRunner
from ag_workstation.sync.status import StationStatus
from ag_workstation.sync.transport import HttpxTransport, SyncTransport

logger = logging.getLogger(__name__)

SERVER_URL_ENV: Final = "AG_SERVER_URL"
CA_BUNDLE_ENV: Final = "AG_SERVER_CA"
CREDENTIALS_FILE: Final = (SERVICE_DIR / "device.agk").as_posix()
"""Sealed with the service-only key (3.0); ACL: the service account only."""
LEGACY_CREDENTIALS_FILE: Final = "device.agk"
"""Before 3.0: sealed with the queue data key; moved once by `SealedFileCredentialStore`."""
HEALTH_SAMPLE_S: Final = 60.0

__all__ = [
    "CA_BUNDLE_ENV",
    "CREDENTIALS_FILE",
    "HEALTH_SAMPLE_S",
    "LEGACY_CREDENTIALS_FILE",
    "SERVER_URL_ENV",
    "HealthSampler",
    "ServiceComponents",
    "ServiceStatusSource",
    "SyncService",
    "app_version",
    "build_sync_service",
    "credential_store",
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


def credential_store(
    data_dir: Path, keys: KeyProvider, service_keys: KeyProvider | None
) -> CredentialStore:
    """The device credentials, sealed with the service key; a refusing store without it.

    `keys` (the queue data key) is used only to move a pre-3.0 file once."""
    if service_keys is None:
        return UnavailableCredentialStore()
    return SealedFileCredentialStore(
        data_dir / CREDENTIALS_FILE,
        service_keys,
        legacy=(data_dir / LEGACY_CREDENTIALS_FILE, keys),
    )


def build_sync_service(
    *,
    data_dir: Path,
    keys: KeyProvider,
    queue: EventQueue,
    clock: Clock,
    transport: SyncTransport,
    service_keys: KeyProvider | None = None,
    credentials: CredentialStore | None = None,
    evidence: SyncEvidence | None = None,
    status: ServiceStatusSource | None = None,
    offset: ClockOffsetEstimator | None = None,
) -> SyncService:
    """Wire a `SyncService` (not started).

    `service_keys` seals the device credentials (None: the service key is
    unavailable and the client cannot authenticate); `credentials` replaces
    the store (tests)."""
    offset = offset or ClockOffsetEstimator()
    credentials = credentials or credential_store(data_dir, keys, service_keys)
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
        on_offset=OffsetPublisher(offset_path(data_dir), clock).publish,
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
    service_keys: KeyProvider | None = None,
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
            service_keys=service_keys,
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
    degraded: tuple[str, ...] = ()
    """Problems found at start (`service_key_unavailable`); the station still works."""


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
    transport: SyncTransport | None = None,
    credentials: CredentialStore | None = None,
    service_keys: KeyProvider | None = None,
) -> ServiceComponents:
    """Sync (if configured), housekeeping (dead letters, evidence age and
    quota, old commands), the one-minute health sample and the camera /
    monitoring alerts (scope 4.4). Never raises.

    The evidence store is opened here, before the engine child starts,
    because opening it sweeps temporary files. `transport` (and
    `credentials`) replace the environment's server settings (tests, the
    fail-open suite).

    `service_keys` is the service-only key (None: `AG_SERVICE_KEY`, else the
    DPAPI file on Windows). Opening the evidence store here also re-seals
    pre-3.0 files once (`legacy_keys`), before the engine child writes.
    """
    closers: list[Callable[[], None]] = []
    degraded: list[str] = []
    service_keys = usable(service_keys or service_key_provider(env, data_dir))
    evidence: EvidenceStore | None = None
    if service_keys is None:
        degraded.append(SERVICE_KEY_UNAVAILABLE)
        logger.error("no service key: evidence withheld, sync cannot authenticate")
    else:
        try:
            evidence = EvidenceStore.open(
                data_dir / "evidence", service_keys, clock, legacy_keys=keys
            )
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
    status = ServiceStatusSource(reporter, state)
    sync: SyncService | None
    if transport is not None:
        try:
            sync = build_sync_service(
                data_dir=data_dir,
                keys=keys,
                queue=queue,
                clock=clock,
                transport=transport,
                service_keys=service_keys,
                credentials=credentials,
                evidence=evidence,
                status=status,
            )
        except Exception as exc:
            logger.error("sync disabled (%s)", type(exc).__name__)
            sync = None
    else:
        sync = sync_service_from_env(
            env,
            data_dir=data_dir,
            keys=keys,
            queue=queue,
            clock=clock,
            service_keys=service_keys,
            evidence=evidence,
            status=status,
        )
    keeper = standard_housekeeping(clock, queue=queue, evidence=evidence)
    offset = (lambda: sync.client.health().clock_offset_ms) if sync is not None else (lambda: 0)
    # Records of this process carry corrected UTC too (contract 6); timing stays raw.
    utc = stamping_clock(clock, data_dir)
    keeper.add(
        "health_sample",
        HEALTH_SAMPLE_S,
        HealthSampler(reporter, queue, utc, offset=offset),
        run_first=False,
    )

    def queue_alert(payload: LocalAlertPayload) -> None:
        queue.enqueue(EventType.LOCAL_ALERT, payload, utc.now())

    keeper.add(
        "monitoring_alerts",
        CHECK_INTERVAL_S,
        MonitoringAlerter(clock, reporter.snapshot, queue_alert),
    )
    if sync is not None:
        keeper.add("purge_commands", HOUSEKEEPING_INTERVAL_S, sync.purge)
    return ServiceComponents(
        sync=sync, housekeeper=keeper, closers=closers, degraded=tuple(degraded)
    )
