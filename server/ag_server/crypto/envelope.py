"""KEK envelope: AES-256-GCM data key per value, wrapped by the KEK.

**KEK format (decision, task 3.4).** `AG_KEK` is any high-entropy string of at
least 32 characters (production refuses placeholders and short values; for
example `openssl rand -base64 48`). The 32-byte KEK is derived from it with
HKDF-SHA256 under a fixed, versioned salt, so the secret's encoding does not
matter and the raw string is never used as a key. Purpose subkeys (CSRF, ...)
come from the same secret with a different HKDF label, so they never equal the
KEK. The KEK id is the first 8 bytes of a domain-separated SHA-256 of the
derived key: it names the key in every blob without revealing it, which is
what lets a rotated KEK (`KeyRing.previous`) still open old blobs.

Blob layout (all fixed sizes, so a truncated blob is detected before
decrypting)::

    "AGK1" | kek_id (8) | wrap_nonce (12) | wrapped data key (32 + 16 tag)
           | data_nonce (12) | ciphertext + tag (16)

The header (magic and kek id) and the caller's associated data (for example
the table, column and row id) are authenticated in both GCM operations, so a
blob copied to another row or with an edited header does not open.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    from ag_server.settings import Settings

ENVELOPE_MAGIC: Final = b"AGK1"
_HKDF_SALT: Final = b"AttentionGuard KEK v1"
_ID_DOMAIN: Final = b"AttentionGuard kek-id v1"
_KEY_LEN: Final = 32
_ID_LEN: Final = 8
_NONCE_LEN: Final = 12
_TAG_LEN: Final = 16
_WRAPPED_LEN: Final = _KEY_LEN + _TAG_LEN
_HEADER_LEN: Final = len(ENVELOPE_MAGIC) + _ID_LEN
_MIN_LEN: Final = _HEADER_LEN + _NONCE_LEN + _WRAPPED_LEN + _NONCE_LEN + _TAG_LEN


class EnvelopeError(Exception):
    """A blob does not open: unknown KEK, wrong associated data, tampered or truncated."""


def _hkdf(secret: str, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=_HKDF_SALT, info=info).derive(
        secret.encode("utf-8")
    )


@dataclass(frozen=True)
class Kek:
    """A derived key-encryption key and its public id."""

    kek_id: bytes
    key: bytes = field(repr=False)


def derive_kek(secret: str) -> Kek:
    """The KEK derived from the `AG_KEK` secret string."""
    key = _hkdf(secret, b"kek")
    return Kek(kek_id=hashlib.sha256(_ID_DOMAIN + key).digest()[:_ID_LEN], key=key)


def derive_subkey(secret: str, label: str) -> bytes:
    """A 32-byte purpose key from the same secret, independent of the KEK."""
    if not label:
        raise ValueError("subkey label must not be empty")
    return _hkdf(secret, b"subkey:" + label.encode("utf-8"))


@dataclass(frozen=True)
class KeyRing:
    """The current KEK (used to seal) and earlier ones (only to open)."""

    current: Kek
    previous: tuple[Kek, ...] = ()

    @classmethod
    def from_secrets(cls, current: str, previous: Sequence[str] = ()) -> KeyRing:
        return cls(derive_kek(current), tuple(derive_kek(p) for p in previous))

    @classmethod
    def from_settings(cls, settings: Settings) -> KeyRing:
        return cls.from_secrets(settings.kek.get_secret_value())

    def find(self, kek_id: bytes) -> Kek | None:
        for kek in (self.current, *self.previous):
            if kek.kek_id == kek_id:
                return kek
        return None

    def needs_rewrap(self, blob: bytes) -> bool:
        """Whether `blob` was sealed with an older KEK and should be re-sealed."""
        return blob[len(ENVELOPE_MAGIC) : _HEADER_LEN] != self.current.kek_id


def seal(ring: KeyRing, plaintext: bytes, aad: bytes) -> bytes:
    """Encrypt `plaintext` under a fresh data key wrapped by the current KEK."""
    header = ENVELOPE_MAGIC + ring.current.kek_id
    bound = header + aad
    data_key = AESGCM.generate_key(bit_length=256)
    wrap_nonce, data_nonce = os.urandom(_NONCE_LEN), os.urandom(_NONCE_LEN)
    wrapped = AESGCM(ring.current.key).encrypt(wrap_nonce, data_key, bound)
    body = AESGCM(data_key).encrypt(data_nonce, plaintext, bound)
    return header + wrap_nonce + wrapped + data_nonce + body


def open_sealed(ring: KeyRing, blob: bytes, aad: bytes) -> bytes:
    """Decrypt a blob made by `seal` with the same associated data."""
    if len(blob) < _MIN_LEN or not blob.startswith(ENVELOPE_MAGIC):
        raise EnvelopeError("not a KEK envelope")
    kek = ring.find(blob[len(ENVELOPE_MAGIC) : _HEADER_LEN])
    if kek is None:
        raise EnvelopeError("sealed with an unknown KEK")
    bound = blob[:_HEADER_LEN] + aad
    pos = _HEADER_LEN
    wrap_nonce = blob[pos : pos + _NONCE_LEN]
    pos += _NONCE_LEN
    wrapped = blob[pos : pos + _WRAPPED_LEN]
    pos += _WRAPPED_LEN
    data_nonce = blob[pos : pos + _NONCE_LEN]
    pos += _NONCE_LEN
    try:
        data_key = AESGCM(kek.key).decrypt(wrap_nonce, wrapped, bound)
        return AESGCM(data_key).decrypt(data_nonce, blob[pos:], bound)
    except InvalidTag:
        raise EnvelopeError("envelope does not authenticate") from None
