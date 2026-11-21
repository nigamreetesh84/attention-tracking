"""Workstation sync client (PLAN 2.9, `api-contract.md` sections 3-7).

Runs in the service parent as the local queue's single sender:

- `DeviceApi`: headers, access tokens (refresh once on 401), enrollment,
  secret rotation, over a `SyncTransport` (`HttpxTransport` in production).
- `SyncClient`: heartbeat and commands, config pull and atomic apply, event
  batches with per-item results, evidence uploads, clock offset, backoff.
- `SyncRunner`: the background thread; never touches the scan path.
- `ConfigStore` / `load_bundle` / `BundleFileWatcher`: the last good bundle,
  sealed on disk, read by the engine child and the UI.
- `CommandInbox` / `CommandPump`: session commands handed to the UI process.
- `CredentialStore`: device credentials (sealed file; tests use memory).
"""

from ag_workstation.sync.api import (
    ApiError,
    CredentialSaveError,
    DeviceApi,
    Exchange,
    NotEnrolledError,
    problem_code,
)
from ag_workstation.sync.client import SyncClient, SyncConfig
from ag_workstation.sync.clock_offset import ClockOffsetEstimator, CorrectedClock
from ag_workstation.sync.commands import CommandInbox, CommandPump
from ag_workstation.sync.config_store import (
    ApplyResult,
    BundleFileWatcher,
    ConfigStore,
    bundle_path,
    load_bundle,
)
from ag_workstation.sync.credentials import (
    CredentialStore,
    DeviceCredentials,
    InMemoryCredentialStore,
    SealedFileCredentialStore,
    UnavailableCredentialStore,
)
from ag_workstation.sync.runner import SyncRunner
from ag_workstation.sync.status import StationStatus, StatusSource, SyncFlag, SyncHealth
from ag_workstation.sync.transport import (
    HttpResponse,
    HttpxTransport,
    SyncTransport,
    TransportError,
    tls_context,
)

__all__ = [
    "ApiError",
    "ApplyResult",
    "BundleFileWatcher",
    "ClockOffsetEstimator",
    "CommandInbox",
    "CommandPump",
    "ConfigStore",
    "CorrectedClock",
    "CredentialSaveError",
    "CredentialStore",
    "DeviceApi",
    "DeviceCredentials",
    "Exchange",
    "HttpResponse",
    "HttpxTransport",
    "InMemoryCredentialStore",
    "NotEnrolledError",
    "SealedFileCredentialStore",
    "StationStatus",
    "StatusSource",
    "SyncClient",
    "SyncConfig",
    "SyncFlag",
    "SyncHealth",
    "SyncRunner",
    "SyncTransport",
    "TransportError",
    "UnavailableCredentialStore",
    "bundle_path",
    "load_bundle",
    "problem_code",
    "tls_context",
]
