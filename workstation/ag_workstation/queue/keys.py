"""Data keys for the local queue and evidence store.

The queue's AES-256 data key is random and never stored in the clear. On a
workstation it is wrapped with Windows DPAPI (`security-privacy.md` section 4)
and kept in a small key file in a directory whose ACL is limited to the
service account (set by the installer). Tests use `InMemoryKeyProvider`.
"""

import contextlib
import importlib
import os
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol, cast

DATA_KEY_BYTES: Final = 32


class KeyUnavailableError(RuntimeError):
    """The data key cannot be produced (missing DPAPI, unreadable or foreign key file)."""


class KeyProvider(Protocol):
    """A source of the 256-bit data key; the same key on every call."""

    def data_key(self) -> bytes:
        """The 32-byte data key."""
        ...


class InMemoryKeyProvider:
    """A key held only in memory (tests, and processes that receive the key over IPC)."""

    def __init__(self, key: bytes | None = None) -> None:
        if key is None:
            key = secrets.token_bytes(DATA_KEY_BYTES)
        if len(key) != DATA_KEY_BYTES:
            raise ValueError(f"the data key must be {DATA_KEY_BYTES} bytes")
        self._key = key

    def data_key(self) -> bytes:
        """The key given at construction (or generated then)."""
        return self._key


class Win32Crypt(Protocol):
    """The part of pywin32's `win32crypt` module this provider uses."""

    def CryptProtectData(  # noqa: N802 - pywin32 name
        self,
        data: bytes,
        description: str | None,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
        /,
    ) -> bytes:
        """Wrap `data` with DPAPI."""
        ...

    def CryptUnprotectData(  # noqa: N802 - pywin32 name
        self,
        blob: bytes,
        entropy: bytes | None,
        reserved: None,
        prompt: None,
        flags: int,
        /,
    ) -> tuple[str, bytes]:
        """Unwrap a DPAPI blob; returns (description, data)."""
        ...


def _load_win32crypt() -> Win32Crypt:
    # Lazy: pywin32 exists only on Windows. The module satisfies the protocol.
    return cast(Win32Crypt, importlib.import_module("win32crypt"))


class DpapiKeyProvider:
    """A random data key wrapped with Windows DPAPI in a key file.

    The first call creates the key if the file does not exist. Creation is
    race-safe across processes: the file is written under a temporary name
    and hard-linked into place, which fails if another process got there
    first; the loser then reads the winner's key.

    `machine_scope=True` (default) uses `CRYPTPROTECT_LOCAL_MACHINE` so the
    service and the UI, which may run as different accounts, can both unwrap
    the key; the file ACL is what limits who can read it. `win32crypt` is
    imported lazily through `crypt_loader`, so this module imports on Linux.
    """

    MAGIC: Final = b"AGK1"
    CRYPTPROTECT_UI_FORBIDDEN: Final = 0x1
    CRYPTPROTECT_LOCAL_MACHINE: Final = 0x4
    ENTROPY: Final = b"AttentionGuard queue data key v1"
    DESCRIPTION: Final = "AttentionGuard queue key"

    def __init__(
        self,
        key_path: Path | str,
        *,
        machine_scope: bool = True,
        crypt_loader: Callable[[], Win32Crypt] = _load_win32crypt,
        randbytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._path = Path(key_path)
        self._flags = self.CRYPTPROTECT_UI_FORBIDDEN | (
            self.CRYPTPROTECT_LOCAL_MACHINE if machine_scope else 0
        )
        self._crypt_loader = crypt_loader
        self._randbytes = randbytes
        self._lock = threading.Lock()
        self._key: bytes | None = None

    def data_key(self) -> bytes:
        """Load (or on first use create) and unwrap the data key."""
        with self._lock:
            if self._key is None:
                crypt = self._crypt()
                if self._path.exists():
                    self._key = self._load(crypt)
                else:
                    self._key = self._create(crypt)
            return self._key

    def _crypt(self) -> Win32Crypt:
        try:
            return self._crypt_loader()
        except ImportError as exc:
            raise KeyUnavailableError("Windows DPAPI (win32crypt) is not available") from exc

    def _load(self, crypt: Win32Crypt) -> bytes:
        raw = self._path.read_bytes()
        if not raw.startswith(self.MAGIC):
            raise KeyUnavailableError(f"{self._path} is not a queue key file")
        try:
            _, key = crypt.CryptUnprotectData(
                raw[len(self.MAGIC) :], self.ENTROPY, None, None, self._flags
            )
        except Exception as exc:  # pywin32 raises pywintypes.error; nothing else to catch by type
            raise KeyUnavailableError(f"DPAPI could not unwrap the key in {self._path}") from exc
        if not isinstance(key, bytes) or len(key) != DATA_KEY_BYTES:
            raise KeyUnavailableError(f"the key in {self._path} has the wrong length")
        return key

    def _create(self, crypt: Win32Crypt) -> bytes:
        key = self._randbytes(DATA_KEY_BYTES)
        blob = crypt.CryptProtectData(key, self.DESCRIPTION, self.ENTROPY, None, None, self._flags)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(self.MAGIC + bytes(blob))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, self._path)
            except FileExistsError:
                return self._load(crypt)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        return key
