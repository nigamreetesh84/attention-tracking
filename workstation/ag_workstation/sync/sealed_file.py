"""Small sealed files written atomically (config bundle, device credentials).

File layout: one ASCII line, `AGS1:` then the base64 of
`nonce | AES-256-GCM(plaintext)`, sealed with the
existing `PayloadCipher` (the DPAPI-protected data key of the local queue)
and a per-purpose associated-data label, so a file copied to another
purpose's path fails authentication. Writes go to a temporary file (fsync)
and are renamed into place, so a reader sees the old file or the new one,
never a torn one.

The files are text on purpose: binary file writes on the workstation are
reserved for evidence images and the key file (see the AST guard in
`tests/workstation/test_consent_gate.py`); these files never hold images.
"""

import base64
import binascii
import contextlib
import os
import secrets
from pathlib import Path
from typing import Final

from ag_workstation.queue.crypto import NONCE_BYTES, PayloadCipher, PayloadIntegrityError
from ag_workstation.queue.keys import KeyProvider

MAGIC: Final = "AGS1:"
TMP_PREFIX: Final = ".tmp-"


class SealedFile:
    """One sealed file at `path`, bound to `label`."""

    def __init__(self, path: Path, keys: KeyProvider, label: str) -> None:
        self._path = path
        self._cipher = PayloadCipher(keys)
        self._ad = f"ag-sealed-file/v1\x1f{label}".encode()

    @property
    def path(self) -> Path:
        """Where the file lives."""
        return self._path

    def exists(self) -> bool:
        """Whether a file is in place."""
        return self._path.is_file()

    def read(self) -> bytes | None:
        """The plaintext, or None if there is no file.

        Raises `PayloadIntegrityError` if the file does not authenticate.
        """
        try:
            text = self._path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as exc:
            raise PayloadIntegrityError("sealed file is not valid") from exc
        if not text.startswith(MAGIC):
            raise PayloadIntegrityError("sealed file has no valid header")
        try:
            blob = base64.b64decode(text[len(MAGIC) :].strip(), validate=True)
        except binascii.Error as exc:
            raise PayloadIntegrityError("sealed file is not valid base64") from exc
        if len(blob) < NONCE_BYTES:
            raise PayloadIntegrityError("sealed file is too short")
        return self._cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], self._ad)

    def write(self, plaintext: bytes) -> None:
        """Seal and replace the file atomically. Raises `OSError`; the old file then stays."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        nonce, sealed = self._cipher.encrypt(plaintext, self._ad)
        tmp = self._path.parent / f"{TMP_PREFIX}{self._path.name}.{secrets.token_hex(4)}"
        try:
            line = MAGIC + base64.b64encode(nonce + sealed).decode("ascii") + "\n"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        _sync_dir(self._path.parent)


def _sync_dir(directory: Path) -> None:
    # Makes the rename durable on POSIX; NTFS journals the rename itself.
    if os.name == "nt":  # pragma: no cover - Windows only
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
