"""Application-level encryption of sensitive columns with the KEK (security-privacy 4).

`seal`/`open_sealed` implement envelope encryption: a fresh AES-256-GCM data
key per value, wrapped by the key-encryption key (KEK). Used for
`user.totp_secret_encrypted` (3.4); camera URIs and consent artifacts can use
the same envelope.
"""

from ag_server.crypto.envelope import (
    ENVELOPE_MAGIC,
    EnvelopeError,
    Kek,
    KeyRing,
    derive_kek,
    derive_subkey,
    open_sealed,
    seal,
)

__all__ = [
    "ENVELOPE_MAGIC",
    "EnvelopeError",
    "Kek",
    "KeyRing",
    "derive_kek",
    "derive_subkey",
    "open_sealed",
    "seal",
]
