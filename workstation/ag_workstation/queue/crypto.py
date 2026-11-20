"""AES-256-GCM sealing of queue payloads.

Each payload gets a fresh random 96-bit nonce. The caller binds the record's
identity (id, type, seq, schema) as associated data, so a ciphertext moved to
another row, or a row whose metadata was edited, fails authentication.
"""

import secrets
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ag_workstation.queue.keys import KeyProvider

NONCE_BYTES: Final = 12


class PayloadIntegrityError(Exception):
    """A sealed payload failed authentication: tampered, swapped, or a different key."""


class PayloadCipher:
    """Seals and opens payloads with the provider's data key."""

    def __init__(self, keys: KeyProvider) -> None:
        self._aead = AESGCM(keys.data_key())

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> tuple[bytes, bytes]:
        """Return `(nonce, ciphertext_with_tag)`."""
        nonce = secrets.token_bytes(NONCE_BYTES)
        return nonce, self._aead.encrypt(nonce, plaintext, associated_data)

    def decrypt(self, nonce: bytes, ciphertext: bytes, associated_data: bytes) -> bytes:
        """Open a sealed payload; raises `PayloadIntegrityError` if it does not authenticate."""
        if len(nonce) != NONCE_BYTES:
            raise PayloadIntegrityError("payload integrity check failed: bad nonce length")
        try:
            return self._aead.decrypt(nonce, ciphertext, associated_data)
        except InvalidTag as exc:
            raise PayloadIntegrityError("payload integrity check failed") from exc
