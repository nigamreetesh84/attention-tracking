"""The last good config bundle on disk (`api-contract.md` section 4.2).

The sync client (service parent) validates every pulled bundle and, only if
it is valid, replaces the stored one atomically. An invalid bundle is
refused, the previous one stays active (fail-open) and the refusal is
reported in the heartbeat as `rejected_config`.

The bundle is stored sealed (camera URIs may carry credentials), in a file
the other processes read at start and watch for changes: the engine child
builds its station config from it, and the UI takes its roster from it
(`load_bundle`, `BundleFileWatcher`).
"""

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from ag_shared.api import ConfigBundle, RejectedConfig
from pydantic import ValidationError

from ag_workstation.queue.crypto import PayloadIntegrityError
from ag_workstation.queue.keys import KeyProvider
from ag_workstation.sync.sealed_file import SealedFile

logger = logging.getLogger(__name__)

BUNDLE_LABEL: Final = "config-bundle"
BUNDLE_FILE: Final = "config/bundle.agc"
"""Path of the bundle file under the data directory."""
MAX_REASON_CHARS: Final = 500


def bundle_path(data_dir: Path) -> Path:
    """The bundle file under `data_dir`."""
    return data_dir / BUNDLE_FILE


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of `ConfigStore.apply`."""

    applied: bool
    bundle: ConfigBundle | None
    """The bundle now active (the previous one after a refusal)."""
    rejected: RejectedConfig | None = None
    """Set when the pulled bundle was refused."""


def load_bundle(path: Path, keys: KeyProvider) -> ConfigBundle | None:
    """The stored bundle, or None if there is none or it cannot be read (logged, never raised)."""
    try:
        raw = SealedFile(path, keys, BUNDLE_LABEL).read()
        return None if raw is None else ConfigBundle.model_validate_json(raw)
    except (OSError, PayloadIntegrityError, ValidationError, ValueError) as exc:
        logger.error("stored config bundle not loaded (%s)", type(exc).__name__)
        return None


class ConfigStore:
    """Validates and atomically installs config bundles. Thread-safe."""

    def __init__(
        self, path: Path, keys: KeyProvider, *, workstation_id: UUID | None = None
    ) -> None:
        self._file = SealedFile(path, keys, BUNDLE_LABEL)
        self._keys = keys
        self._workstation_id = workstation_id
        self._lock = threading.Lock()
        self._bundle = load_bundle(path, keys)

    @property
    def path(self) -> Path:
        """The bundle file."""
        return self._file.path

    def current(self) -> ConfigBundle | None:
        """The active bundle (None before the first one)."""
        with self._lock:
            return self._bundle

    @property
    def applied_version(self) -> int | None:
        """Version of the active bundle."""
        bundle = self.current()
        return None if bundle is None else bundle.version

    def set_workstation_id(self, workstation_id: UUID | None) -> None:
        """Bundles for any other workstation are refused from now on."""
        with self._lock:
            self._workstation_id = workstation_id

    def apply(self, raw: bytes, *, advertised_version: int | None = None) -> ApplyResult:
        """Validate `raw` (a `GET /config` body) and install it if valid.

        `advertised_version` (from the heartbeat or `pull_config`) names the
        refused version when the body is too broken to carry one. Never raises.
        """
        try:
            bundle = ConfigBundle.model_validate_json(raw)
            if self._workstation_id is not None and bundle.workstation.id != self._workstation_id:
                raise ValueError("bundle is for another workstation")
        except (ValidationError, ValueError) as exc:
            return self._refuse(raw, advertised_version, _reason(exc))
        try:
            with self._lock:
                self._file.write(raw)
                self._bundle = bundle
        except OSError as exc:
            # Could not persist: keep running the previous bundle so every
            # process agrees on what is active.
            logger.error("config bundle %d not stored (%s)", bundle.version, type(exc).__name__)
            return self._refuse(raw, bundle.version, f"not stored: {type(exc).__name__}")
        logger.info("config bundle %d applied", bundle.version)
        return ApplyResult(applied=True, bundle=bundle)

    def _refuse(self, raw: bytes, advertised: int | None, reason: str) -> ApplyResult:
        version = _version_of(raw)
        if version is None:
            version = advertised if advertised is not None else 0
        logger.warning("config bundle %d refused: %s", version, reason)
        return ApplyResult(
            applied=False,
            bundle=self.current(),
            rejected=RejectedConfig(version=version, reason=reason[:MAX_REASON_CHARS]),
        )


def _version_of(raw: bytes) -> int | None:
    try:
        version = json.loads(raw).get("version")
    except (ValueError, AttributeError):
        return None
    return version if isinstance(version, int) and version >= 0 else None


def _reason(exc: Exception) -> str:
    # Field locations and error types only: values could echo camera URIs.
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['type']}"
            for err in exc.errors()[:5]
        ]
        return "invalid bundle: " + "; ".join(parts)
    return str(exc)  # only our own checks raise a plain ValueError


class BundleFileWatcher:
    """Notices a new bundle file (for processes that only read it)."""

    def __init__(self, path: Path, keys: KeyProvider) -> None:
        self._path = path
        self._keys = keys
        self._stamp: tuple[int, int] | None = None

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def poll(self) -> ConfigBundle | None:
        """The bundle if the file changed since the last poll (or on the first poll)."""
        stamp = self._stat()
        if stamp is None or stamp == self._stamp:
            return None
        bundle = load_bundle(self._path, self._keys)
        if bundle is not None:
            self._stamp = stamp
        return bundle
