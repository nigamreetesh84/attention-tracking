"""The service-only key (task 3.0, phase-2 review H4 option (a)).

The kiosk UI process holds the queue data key (`keys/data.key`): rule 1
keeps queue writes in the UI, so it must. Everything the UI does not need is
sealed with a second key that only the service account can unwrap:

- evidence image files (`evidence/images/*.agev`, `evidence.files`);
- the device credentials (`service/device.agk`, `sync.credentials`).

Decision:

- **Generation:** 32 random bytes, created race-safe by whichever service
  process needs it first (`DpapiKeyProvider`: temporary file hard-linked into
  place, the loser reads the winner's key).
- **Storage (Windows):** `AG_DATA_DIR/service/service.key`, wrapped with DPAPI
  in **user scope of the service account** (no `CRYPTPROTECT_LOCAL_MACHINE`),
  with its own magic and entropy, so it cannot be swapped with the data key or
  the IPC key file. User scope means the kiosk account cannot unwrap it even
  if it could read the file; the file ACL is a second barrier (installer,
  phase 7.1/7.4: `AG_DATA_DIR/service/` and `AG_DATA_DIR/evidence/` full
  control for the service account and Administrators, nothing for the kiosk
  account or Users). The service parent and its engine child must run as the
  same account (they do: the child inherits the parent's token). Re-installing
  the service under another account loses the key: stored evidence is then
  removed as corrupt and the station must be re-enrolled.
- **Development:** `AG_SERVICE_KEY` (hex, exactly 32 bytes) overrides the file;
  off Windows it is the only source. It is refused if it equals `AG_DATA_KEY`.
- **Fail-open:** no key gives None (or a provider that raises
  `KeyUnavailableError`). Evidence is then withheld (metadata only), the sync
  client cannot authenticate and flags `service_key_unavailable`; scans,
  detection and the queue are untouched.

Only service-side modules (`service.host`, `service.engine_factory`,
`sync.wiring`) may use this module; `tests/workstation/test_service_key.py`
pins that the UI never imports it.
"""

import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from ag_workstation.queue.keys import (
    DATA_KEY_BYTES,
    DpapiKeyProvider,
    InMemoryKeyProvider,
    KeyProvider,
    Win32Crypt,
    _load_win32crypt,
)

logger = logging.getLogger(__name__)

SERVICE_KEY_ENV: Final = "AG_SERVICE_KEY"
DATA_KEY_ENV: Final = "AG_DATA_KEY"
SERVICE_DIR: Final = Path("service")
"""Service-only files under `AG_DATA_DIR` (ACL: the service account only)."""
SERVICE_KEY_FILE: Final = SERVICE_DIR / "service.key"
SERVICE_KEY_MAGIC: Final = b"AGV1"
SERVICE_KEY_ENTROPY: Final = b"AttentionGuard service-only key v1"
SERVICE_KEY_DESCRIPTION: Final = "AttentionGuard service key"
SERVICE_KEY_UNAVAILABLE: Final = "service_key_unavailable"
"""The degraded-storage name used in health and logs."""


def service_key_provider(
    env: Mapping[str, str],
    data_dir: Path | None,
    *,
    platform: str | None = None,
    crypt_loader: Callable[[], Win32Crypt] = _load_win32crypt,
) -> KeyProvider | None:
    """The service key source: `AG_SERVICE_KEY`, else the DPAPI file on Windows, else None.

    The DPAPI provider is lazy: it unwraps (or creates) the key on first use
    and raises `KeyUnavailableError` then if it cannot; use `usable` to probe.
    Never raises itself; the reason for a missing key is logged (never the key).
    """
    raw = env.get(SERVICE_KEY_ENV, "").strip()
    if raw:
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            logger.error("%s is not hex; no service key", SERVICE_KEY_ENV)
            return None
        if len(key) != DATA_KEY_BYTES:
            logger.error("%s must be %d bytes; no service key", SERVICE_KEY_ENV, DATA_KEY_BYTES)
            return None
        if raw.lower() == env.get(DATA_KEY_ENV, "").strip().lower():
            # The point of this key is that the UI's key cannot open what it seals.
            logger.error("%s equals %s; no service key", SERVICE_KEY_ENV, DATA_KEY_ENV)
            return None
        return InMemoryKeyProvider(key)
    if (platform or sys.platform) != "win32" or data_dir is None:
        return None
    return DpapiKeyProvider(
        data_dir / SERVICE_KEY_FILE,
        machine_scope=False,
        crypt_loader=crypt_loader,
        magic=SERVICE_KEY_MAGIC,
        entropy=SERVICE_KEY_ENTROPY,
        description=SERVICE_KEY_DESCRIPTION,
        kind="service",
    )


def usable(keys: KeyProvider | None) -> KeyProvider | None:
    """`keys` if the key can be produced now, else None (logged, never raised)."""
    if keys is None:
        return None
    try:
        keys.data_key()
    except Exception as exc:
        logger.error("service key unavailable (%s)", type(exc).__name__)
        return None
    return keys
