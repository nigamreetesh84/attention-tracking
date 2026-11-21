"""Device credentials (`api-contract.md` section 3).

The workstation id and device secret come from enrollment and are needed to
get access tokens. They sit behind `CredentialStore`:

- `InMemoryCredentialStore` for tests;
- `SealedFileCredentialStore`: a sealed file under the data directory,
  encrypted with the queue's data key, which DPAPI protects on Windows.
  The contract asks for the secret "stored with DPAPI"; this reaches DPAPI
  through the data key rather than by a separate `CryptProtectData` call
  (recorded for the architect in PROGRESS.md, task 2.9).

Access tokens are never persisted: they live 15 minutes and are fetched again.
"""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from pydantic import SecretStr

from ag_workstation.queue.keys import KeyProvider
from ag_workstation.sync.sealed_file import SealedFile

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

    def __init__(self, path: Path, keys: KeyProvider) -> None:
        self._file = SealedFile(path, keys, CREDENTIALS_LABEL)
        self._lock = threading.Lock()

    def load(self) -> DeviceCredentials | None:
        """Read and authenticate the file; None if there is none."""
        with self._lock:
            raw = self._file.read()
        if raw is None:
            return None
        body = json.loads(raw)
        return DeviceCredentials(
            workstation_id=UUID(body["workstation_id"]),
            device_secret=SecretStr(body["device_secret"]),
        )

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
