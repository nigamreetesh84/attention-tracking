"""Device credentials (`api-contract.md` section 3).

The workstation id and device secret come from enrollment and are needed to
get access tokens. They sit behind `CredentialStore`:

- `InMemoryCredentialStore` for tests;
- `SealedFileCredentialStore`: a sealed file (`AG_DATA_DIR/service/device.agk`)
  encrypted with the **service-only key** (`service.service_key`, task 3.0),
  which DPAPI protects in the service account's user scope. The kiosk UI
  process holds only the queue data key and cannot open it. Before 3.0 the
  file was `AG_DATA_DIR/device.agk` sealed with the queue data key; `legacy`
  moves it once (read with the data key, written with the service key, old
  file deleted);
- `UnavailableCredentialStore` when the service key cannot be produced: the
  sync client cannot authenticate and flags it; nothing else stops.

Access tokens are never persisted: they live 15 minutes and are fetched again.
"""

import contextlib
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from pydantic import SecretStr

from ag_workstation.queue.keys import KeyProvider, KeyUnavailableError
from ag_workstation.sync.sealed_file import SealedFile

logger = logging.getLogger(__name__)

CREDENTIALS_LABEL: Final = "device-credentials"


@dataclass(frozen=True, slots=True)
class DeviceCredentials:
    """What enrollment gave this workstation."""

    workstation_id: UUID
    device_secret: SecretStr = field(repr=False)


class CredentialStore(Protocol):
    """Where the device credentials are kept."""

    def load(self) -> DeviceCredentials | None:
        """The stored credentials; None if not enrolled. May raise on a broken store."""
        ...

    def save(self, credentials: DeviceCredentials) -> None:
        """Replace the stored credentials atomically. May raise."""
        ...


class InMemoryCredentialStore:
    """Credentials in memory (tests). Thread-safe."""

    def __init__(self, credentials: DeviceCredentials | None = None) -> None:
        self._lock = threading.Lock()
        self._credentials = credentials
        self.fail_saves = 0
        """Make the next N saves raise `OSError` (fault injection)."""

    def load(self) -> DeviceCredentials | None:
        """The credentials last saved."""
        with self._lock:
            return self._credentials

    def save(self, credentials: DeviceCredentials) -> None:
        """Keep them (or fail if told to)."""
        with self._lock:
            if self.fail_saves:
                self.fail_saves -= 1
                raise OSError("credential store unavailable")
            self._credentials = credentials


class SealedFileCredentialStore:
    """Credentials in a sealed file (see the module docstring). Thread-safe."""

    def __init__(
        self,
        path: Path,
        keys: KeyProvider,
        *,
        legacy: tuple[Path, KeyProvider] | None = None,
    ) -> None:
        """`keys` is the service key. `legacy` is the pre-3.0 file and the queue
        data key it was sealed with; it is read only while `path` does not exist."""
        self._file = SealedFile(path, keys, CREDENTIALS_LABEL)
        self._legacy = SealedFile(legacy[0], legacy[1], CREDENTIALS_LABEL) if legacy else None
        self._lock = threading.Lock()

    def load(self) -> DeviceCredentials | None:
        """Read and authenticate the file; None if there is none.

        Migration (3.0): with no file but a legacy one, the legacy file is read
        with the data key, re-sealed with the service key and deleted. If the
        new file cannot be written the credentials are still returned and the
        legacy file stays for the next try.
        """
        with self._lock:
            raw = self._file.read()
            if raw is None and self._legacy is not None:
                raw = self._migrate(self._legacy)
        if raw is None:
            return None
        return _decode(raw)

    def _migrate(self, legacy: SealedFile) -> bytes | None:
        raw = legacy.read()
        if raw is None:
            return None
        try:
            self._file.write(raw)
        except Exception as exc:
            logger.error("device credentials not moved to the service key (%s)", type(exc).__name__)
            return raw
        with contextlib.suppress(FileNotFoundError):
            legacy.path.unlink()
        logger.warning("device credentials re-sealed under the service key")
        return raw

    def save(self, credentials: DeviceCredentials) -> None:
        """Seal and write atomically."""
        body = json.dumps(
            {
                "workstation_id": str(credentials.workstation_id),
                "device_secret": credentials.device_secret.get_secret_value(),
            }
        ).encode()
        with self._lock:
            self._file.write(body)


def _decode(raw: bytes) -> DeviceCredentials:
    body = json.loads(raw)
    return DeviceCredentials(
        workstation_id=UUID(body["workstation_id"]),
        device_secret=SecretStr(body["device_secret"]),
    )


class UnavailableCredentialStore:
    """The service key cannot be produced: every call raises `KeyUnavailableError`.

    The sync client catches it at its boundary (`credential_store_failed`,
    `service_key_unavailable`) and keeps the queue buffered."""

    def load(self) -> DeviceCredentials | None:
        """Refuse."""
        raise KeyUnavailableError("the service key is unavailable")

    def save(self, credentials: DeviceCredentials) -> None:
        """Refuse."""
        raise KeyUnavailableError("the service key is unavailable")
