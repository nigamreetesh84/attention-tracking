"""IPC transport: framed byte messages between the service and its clients.

`PipeTransport` uses `multiprocessing.connection`: a named pipe (AF_PIPE) on
Windows, a Unix domain socket elsewhere. Both ends prove knowledge of a shared
`authkey` (HMAC challenge) before any message is read. Only `send_bytes` and
`recv_bytes` are used, never the pickling `send`/`recv`.
"""

import contextlib
import os
import stat
import sys
import tempfile
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Final, Protocol

from ag_workstation.service.protocol import MAX_MESSAGE_BYTES

MIN_AUTHKEY_BYTES: Final = 16
_PIPE_PREFIX: Final = "\\\\.\\pipe\\"


class IpcConnection(Protocol):
    """One open, authenticated connection."""

    def send_bytes(self, buf: bytes) -> None:
        """Send one framed message."""
        ...

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        """Receive one framed message (blocks). Raises OSError/EOFError on failure."""
        ...

    def poll(self, timeout: float | None = 0.0) -> bool:
        """Whether a message is ready within `timeout` seconds."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...


class IpcListener(Protocol):
    """A bound server endpoint."""

    def accept(self) -> IpcConnection:
        """Wait for, authenticate and return the next connection."""
        ...

    def close(self) -> None:
        """Stop listening."""
        ...


class IpcTransport(Protocol):
    """Creates listeners (server side) and connections (client side)."""

    def listen(self) -> IpcListener:
        """Bind the endpoint."""
        ...

    def connect(self) -> IpcConnection:
        """Open an authenticated connection. Raises OSError when nobody listens."""
        ...


def default_address(name: str = "engine") -> str:
    """The well-known endpoint: a named pipe on Windows, a socket path elsewhere."""
    if sys.platform == "win32":
        return f"{_PIPE_PREFIX}AttentionGuard-{name}"
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return str(Path(base) / f"attentionguard-{name}.sock")


class PipeTransport:
    """`multiprocessing.connection` with an authkey. Picklable (plain fields)."""

    def __init__(self, address: str, authkey: bytes) -> None:
        if len(authkey) < MIN_AUTHKEY_BYTES:
            raise ValueError(f"authkey must be at least {MIN_AUTHKEY_BYTES} bytes")
        self.address = address
        self._authkey = authkey
        self.family = "AF_PIPE" if address.startswith(_PIPE_PREFIX) else "AF_UNIX"

    def __repr__(self) -> str:
        # The authkey stays out of logs.
        return f"PipeTransport({self.address!r})"

    def listen(self) -> IpcListener:
        """Bind; a socket file left by a crashed engine is removed first."""
        if self.family == "AF_UNIX":
            path = Path(self.address)
            with contextlib.suppress(FileNotFoundError):
                if stat.S_ISSOCK(path.lstat().st_mode):
                    path.unlink()
        return Listener(self.address, family=self.family, authkey=self._authkey)

    def connect(self) -> IpcConnection:
        """Connect and authenticate."""
        return Client(self.address, family=self.family, authkey=self._authkey)


__all__ = [
    "MAX_MESSAGE_BYTES",
    "MIN_AUTHKEY_BYTES",
    "IpcConnection",
    "IpcListener",
    "IpcTransport",
    "PipeTransport",
    "default_address",
]
