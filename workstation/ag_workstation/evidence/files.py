"""Encrypted evidence image files (`security-privacy.md` section 4).

One file per image: `MAGIC | nonce | AES-256-GCM(jpeg)`. The associated data
binds the ciphertext to the image's identity and upload metadata (image id,
event id, camera slot, seq, offset, capture time, plaintext hash), so a file
renamed to another image id, or an index row whose metadata was edited, fails
authentication on read.

The file key is derived with HKDF from the **service-only key**
(`service.service_key`, task 3.0), which the kiosk UI process never holds, so
a kiosk-account escape cannot decrypt evidence. Files of releases before 3.0
(`LEGACY_MAGIC`, key derived from the queue data key) are re-sealed once by
the service parent (`EvidenceStore.open(legacy_keys=...)`, `reseal_legacy`);
`read` refuses them otherwise.

Writes are atomic: the ciphertext goes to a temporary name (exclusive create,
fsync), is renamed into place and the directory is synced. A crash leaves at
worst a temporary file or an unreferenced final file; both are swept by
`EvidenceStore.open`. Plaintext JPEG bytes are never written.
"""

import contextlib
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ag_workstation.queue.crypto import NONCE_BYTES, PayloadCipher, PayloadIntegrityError
from ag_workstation.queue.keys import (
    DATA_KEY_BYTES,
    InMemoryKeyProvider,
    KeyProvider,
    KeyUnavailableError,
)

MAGIC: Final = b"AGE2"
"""Sealed with the service-only key (3.0)."""
LEGACY_MAGIC: Final = b"AGE1"
"""Sealed with the queue data key (before 3.0); only `reseal_legacy` reads it."""
TAG_BYTES: Final = 16
FILE_OVERHEAD: Final = len(MAGIC) + NONCE_BYTES + TAG_BYTES
"""Bytes an encrypted file adds to its JPEG."""
FILE_SUFFIX: Final = ".agev"
TMP_PREFIX: Final = ".tmp-"
_KDF_INFO: Final = b"AttentionGuard evidence file key v2 (service key)"
_LEGACY_KDF_INFO: Final = b"AttentionGuard evidence file key v1"


def derive_evidence_key(keys: KeyProvider) -> bytes:
    """The evidence file key: HKDF-SHA256 of the service key with an evidence label."""
    return HKDF(algorithm=SHA256(), length=DATA_KEY_BYTES, salt=None, info=_KDF_INFO).derive(
        keys.data_key()
    )


def derive_legacy_evidence_key(keys: KeyProvider) -> bytes:
    """The pre-3.0 file key: HKDF-SHA256 of the queue data key (migration only)."""
    return HKDF(algorithm=SHA256(), length=DATA_KEY_BYTES, salt=None, info=_LEGACY_KDF_INFO).derive(
        keys.data_key()
    )


def image_associated_data(
    image_id: UUID,
    event_id: UUID,
    camera_slot: int,
    seq: int,
    offset_ms: int,
    captured_at: datetime,
    sha256: str,
) -> bytes:
    """The AES-GCM associated data binding an image file to its index row."""
    parts = (
        "ag-evidence/v1",
        str(image_id),
        str(event_id),
        str(camera_slot),
        str(seq),
        str(offset_ms),
        captured_at.astimezone(UTC).isoformat(),
        sha256,
    )
    return "\x1f".join(parts).encode()


class EncryptedImageFiles:
    """The directory of sealed image files, one per image id."""

    def __init__(
        self, directory: Path, keys: KeyProvider, *, legacy_keys: KeyProvider | None = None
    ) -> None:
        """`keys` is the service key. `legacy_keys` (the queue data key) is given
        only by the service parent, to re-seal pre-3.0 files."""
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cipher = PayloadCipher(InMemoryKeyProvider(derive_evidence_key(keys)))
        self._legacy_keys = legacy_keys
        self._legacy_cipher: PayloadCipher | None = None

    @property
    def directory(self) -> Path:
        """Where the files live."""
        return self._dir

    def path_for(self, image_id: UUID) -> Path:
        """The final path of an image's file."""
        return self._dir / f"{image_id}{FILE_SUFFIX}"

    def write(self, image_id: UUID, jpeg: bytes, associated_data: bytes) -> int:
        """Seal `jpeg` and move it into place atomically; returns the bytes on disk.

        Raises `OSError` (disk full, permissions); nothing is left at the
        final path in that case.
        """
        nonce, sealed = self._cipher.encrypt(jpeg, associated_data)
        blob = MAGIC + nonce + sealed
        tmp = self._dir / f"{TMP_PREFIX}{image_id}.{secrets.token_hex(4)}"
        try:
            fd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
            )
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path_for(image_id))
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        self._sync_dir()
        return len(blob)

    def read(self, image_id: UUID, associated_data: bytes) -> bytes:
        """Open an image file.

        Raises `FileNotFoundError` if it is missing and `PayloadIntegrityError`
        if it is not a valid sealed file for this metadata.
        """
        raw = self.path_for(image_id).read_bytes()
        if raw.startswith(LEGACY_MAGIC):
            raise PayloadIntegrityError("evidence file is sealed under the retired data key")
        return self._open(self._cipher, MAGIC, raw, associated_data)

    @staticmethod
    def _open(cipher: PayloadCipher, magic: bytes, raw: bytes, associated_data: bytes) -> bytes:
        if not raw.startswith(magic) or len(raw) < FILE_OVERHEAD:
            raise PayloadIntegrityError("evidence file has no valid header")
        nonce = raw[len(magic) : len(magic) + NONCE_BYTES]
        return cipher.decrypt(nonce, raw[len(magic) + NONCE_BYTES :], associated_data)

    def is_legacy(self, image_id: UUID) -> bool:
        """Whether the image's file is a pre-3.0 file (False if it is missing)."""
        try:
            with self.path_for(image_id).open("rb") as fh:
                return fh.read(len(LEGACY_MAGIC)) == LEGACY_MAGIC
        except FileNotFoundError:
            return False

    def reseal_legacy(self, image_id: UUID, associated_data: bytes) -> int:
        """Re-seal a pre-3.0 file under the service key, atomically; returns its size.

        Not a new image: the same bytes, already permitted by the capture gate
        when first written, bound to the same metadata. Raises
        `PayloadIntegrityError` if the file does not open with the legacy key,
        `KeyUnavailableError` if there is no legacy key, `OSError` on disk errors.
        """
        if self._legacy_cipher is None:
            if self._legacy_keys is None:
                raise KeyUnavailableError("no legacy data key to re-seal with")
            self._legacy_cipher = PayloadCipher(
                InMemoryKeyProvider(derive_legacy_evidence_key(self._legacy_keys))
            )
        raw = self.path_for(image_id).read_bytes()
        jpeg = self._open(self._legacy_cipher, LEGACY_MAGIC, raw, associated_data)
        return self.write(image_id, jpeg, associated_data)

    def delete(self, image_id: UUID) -> bool:
        """Remove an image file; False if it was not there."""
        try:
            self.path_for(image_id).unlink()
        except FileNotFoundError:
            return False
        return True

    def stored_ids(self) -> set[UUID]:
        """Ids of all final files (not temporaries); unparseable names are skipped."""
        ids: set[UUID] = set()
        for path in self._dir.glob(f"*{FILE_SUFFIX}"):
            with contextlib.suppress(ValueError):
                ids.add(UUID(path.name.removesuffix(FILE_SUFFIX)))
        return ids

    def remove_temporaries(self) -> int:
        """Delete temporary files left by an interrupted write."""
        count = 0
        for path in self._dir.glob(f"{TMP_PREFIX}*"):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
                count += 1
        return count

    def _sync_dir(self) -> None:
        # Makes the rename durable on POSIX; Windows cannot open a directory
        # this way and NTFS journals the rename itself.
        if os.name == "nt":  # pragma: no cover - Windows only
            return
        fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
