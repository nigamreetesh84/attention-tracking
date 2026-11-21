"""IPC transport: framed byte messages between the service and its clients.

`PipeTransport` uses `multiprocessing.connection`: a named pipe (AF_PIPE) on
Windows, a Unix domain socket elsewhere. Both ends prove knowledge of a shared
`authkey` (HMAC challenge, mutual) before any message is read. Only
`send_bytes` and `recv_bytes` are used, never the pickling `send`/`recv`.

The server side accepts raw connections and runs the challenge separately
(`IpcListener.authenticate`, 2.R): `multiprocessing`'s own `Listener.accept`
runs it on the accepting thread with no deadline, so one local process that
connects and stays silent blocked every later connection. Here the
challenge runs on the connection's own thread and every read in it has a
deadline (`HANDSHAKE_TIMEOUT_S`).
"""

import contextlib
import os
import stat
import sys
import tempfile
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import (
    Client,
    Connection,
    Listener,
    answer_challenge,
    deliver_challenge,
)
from pathlib import Path
from typing import Final, Protocol, cast

from ag_workstation.service.protocol import MAX_MESSAGE_BYTES

MIN_AUTHKEY_BYTES: Final = 16
HANDSHAKE_TIMEOUT_S: Final = 1.0
"""Longest a connecting client may take to answer the auth challenge."""
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
        """Wait for the next connection (not yet authenticated)."""
        ...

    def authenticate(self, conn: IpcConnection, timeout_s: float) -> None:
        """Run the auth handshake on `conn` within `timeout_s`; raises on failure."""
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


class _Deadline:
    """A connection whose reads fail once `deadline` (monotonic) has passed."""

    def __init__(self, conn: IpcConnection, deadline: float) -> None:
        self._conn = conn
        self._deadline = deadline

    def send_bytes(self, buf: bytes) -> None:
        self._conn.send_bytes(buf)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0 or not self._conn.poll(remaining):
            raise AuthenticationError("IPC handshake timed out")
        return self._conn.recv_bytes(maxlength)


class _HandshakeListener:
    """`Listener` without its built-in handshake; `authenticate` runs it with a deadline."""

    def __init__(self, listener: Listener, authkey: bytes) -> None:
        self._listener = listener
        self._authkey = authkey

    def accept(self) -> IpcConnection:
        conn: IpcConnection = self._listener.accept()
        return conn

    def authenticate(self, conn: IpcConnection, timeout_s: float) -> None:
        # The same mutual challenge `Listener.accept` runs, in the same order.
        # Both functions only call send_bytes/recv_bytes, which _Deadline has.
        wrapped = cast(Connection, _Deadline(conn, time.monotonic() + timeout_s))
        deliver_challenge(wrapped, self._authkey)
        answer_challenge(wrapped, self._authkey)

    def close(self) -> None:
        self._listener.close()


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
        # No authkey here: the handshake runs in `authenticate`, off the accept thread.
        return _HandshakeListener(Listener(self.address, family=self.family), self._authkey)

    def connect(self) -> IpcConnection:
        """Connect and authenticate."""
        return Client(self.address, family=self.family, authkey=self._authkey)


__all__ = [
    "HANDSHAKE_TIMEOUT_S",
    "MAX_MESSAGE_BYTES",
    "MIN_AUTHKEY_BYTES",
    "IpcConnection",
    "IpcListener",
    "IpcTransport",
    "PipeTransport",
    "default_address",
]
