"""The IPC authkey shared by the service's engine child and the UI (2.R, closes the 2.3 open item).

Decision:

- **Generation:** 32 random bytes (`secrets`), created by whichever process
  needs it first (normally the service at start). Creation is race-safe
  across processes (`DpapiKeyProvider`: write a temporary file, hard-link it
  into place, the loser reads the winner's key).
- **Storage (Windows):** `AG_DATA_DIR/keys/ipc.key`, wrapped with DPAPI in
  machine scope (the service account and the kiosk account must both unwrap
  it), with its own magic and entropy, so it cannot be swapped with the queue
  data key file. It is a separate key from the data key: the IPC key only
  lets a process talk to the engine child, never read stored data. The file
  ACL (installer, phase 7.1/7.4) is the real limit: read for the service
  account and the kiosk UI account, nobody else.
- **Development:** `AG_IPC_AUTHKEY` (hex, at least 16 bytes) overrides the
  file everywhere; off Windows it is the only source.
- A key that cannot be produced gives None: the UI then runs fail-open
  ("Monitoring unavailable"), and the service refuses to start its host
  (a configuration error, never on the scan path).
"""

import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from ag_workstation.queue.keys import DpapiKeyProvider, Win32Crypt, _load_win32crypt
from ag_workstation.service.transport import MIN_AUTHKEY_BYTES

logger = logging.getLogger(__name__)

AUTHKEY_ENV: Final = "AG_IPC_AUTHKEY"
IPC_KEY_FILE: Final = Path("keys") / "ipc.key"
IPC_KEY_MAGIC: Final = b"AGI1"
IPC_KEY_ENTROPY: Final = b"AttentionGuard IPC authkey v1"
IPC_KEY_DESCRIPTION: Final = "AttentionGuard IPC key"


def ipc_authkey(
    env: Mapping[str, str],
    data_dir: Path | None,
    *,
    platform: str = sys.platform,
    crypt_loader: Callable[[], Win32Crypt] = _load_win32crypt,
) -> bytes | None:
    """The authkey: `AG_IPC_AUTHKEY`, else the DPAPI key file on Windows, else None.

    Never raises; the reason for a missing key is logged (never the key)."""
    raw = env.get(AUTHKEY_ENV, "").strip()
    if raw:
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            logger.error("%s is not hex; no IPC key", AUTHKEY_ENV)
            return None
        if len(key) < MIN_AUTHKEY_BYTES:
            logger.error("%s is shorter than %d bytes; no IPC key", AUTHKEY_ENV, MIN_AUTHKEY_BYTES)
            return None
        return key
    if platform != "win32" or data_dir is None:
        return None
    provider = DpapiKeyProvider(
        data_dir / IPC_KEY_FILE,
        crypt_loader=crypt_loader,
        magic=IPC_KEY_MAGIC,
        entropy=IPC_KEY_ENTROPY,
        description=IPC_KEY_DESCRIPTION,
        kind="IPC",
    )
    try:
        return provider.data_key()
    except Exception as exc:
        logger.error("IPC key unavailable (%s)", type(exc).__name__)
        return None
