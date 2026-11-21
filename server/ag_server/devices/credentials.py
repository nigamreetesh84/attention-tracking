"""Device secrets and enrollment tokens: generation, hashing, overlap on rotation.

Both are 256 random bits (`api-contract.md` 3), so a fast, domain-separated
SHA-256 is a sufficient hash at rest: there is nothing to brute-force, and
unlike a KEK-keyed HMAC it survives a KEK rotation. Comparisons are constant
time.

`workstation.device_secret_hash` holds a small versioned encoding of
`SecretState`, because the 10-minute overlap after a rotation needs the
previous hash and its end time and `schema.md` has one column (a proposal for
two dedicated columns is in PROGRESS.md):

    v1:<current hex>                         no overlap
    v1:<current hex>:<previous hex>:<until>  until = UTC microseconds since epoch

Anything unreadable decodes to None and matches no secret (fail closed).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

_SECRET_DOMAIN: Final = b"AttentionGuard device-secret v1\x00"
_ENROLLMENT_DOMAIN: Final = b"AttentionGuard enrollment-token v1\x00"
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICRO: Final = timedelta(microseconds=1)
CREDENTIAL_ID_CHARS: Final = 16


def new_secret() -> str:
    """256 random bits, base64url without padding (43 characters)."""
    return secrets.token_urlsafe(32)


def _digest(domain: bytes, value: str) -> str:
    return hashlib.sha256(domain + value.encode("utf-8")).hexdigest()


def hash_secret(secret: str) -> str:
    """The stored hash of a device secret (64 hex characters)."""
    return _digest(_SECRET_DOMAIN, secret)


def hash_enrollment_token(token: str) -> str:
    """The stored hash of an enrollment token (64 hex characters)."""
    return _digest(_ENROLLMENT_DOMAIN, token)


def credential_id(secret_hash: str) -> str:
    """A short, non-secret id of a stored secret, carried in access tokens.

    It tells a rotation which secret the device proved it holds.
    """
    return secret_hash[:CREDENTIAL_ID_CHARS]


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


@dataclass(frozen=True)
class SecretState:
    """The current secret hash and, during the overlap, the previous one."""

    current: str
    previous: str | None = None
    previous_until: datetime | None = None

    def encode(self) -> str:
        """The value stored in `workstation.device_secret_hash`."""
        if self.previous is None or self.previous_until is None:
            return f"v1:{self.current}"
        micros = (self.previous_until - _EPOCH) // _MICRO
        return f"v1:{self.current}:{self.previous}:{micros}"

    @classmethod
    def decode(cls, stored: str | None) -> SecretState | None:
        """Parse a stored value; None if absent or unreadable."""
        if not stored:
            return None
        parts = stored.split(":")
        if parts[0] != "v1" or not _HEX64.fullmatch(parts[1] if len(parts) > 1 else ""):
            return None
        if len(parts) == 2:
            return cls(parts[1])
        if len(parts) != 4 or not _HEX64.fullmatch(parts[2]) or not parts[3].isdigit():
            return None
        return cls(parts[1], parts[2], _EPOCH + int(parts[3]) * _MICRO)

    def live_previous(self, now: datetime) -> str | None:
        """The previous hash while its overlap lasts (the end is exclusive)."""
        if (
            self.previous is not None
            and self.previous_until is not None
            and now < self.previous_until
        ):
            return self.previous
        return None

    def holds(self, credential: str, now: datetime) -> bool:
        """True if `credential` (an id) names a secret that is valid now."""
        if _same(credential, credential_id(self.current)):
            return True
        previous = self.live_previous(now)
        return previous is not None and _same(credential, credential_id(previous))

    def match(self, secret: str, now: datetime) -> str | None:
        """The stored hash `secret` matches (current, or previous inside the overlap)."""
        digest = hash_secret(secret)
        if _same(digest, self.current):
            return self.current
        previous = self.live_previous(now)
        if previous is not None and _same(digest, previous):
            return previous
        return None

    def rotate(
        self, new_hash: str, *, proven: str, now: datetime, overlap: timedelta
    ) -> SecretState | None:
        """The state after a rotation by a caller who proved the secret `proven` (an id).

        The proven secret, not blindly the current one, becomes the previous:
        if a rotation answer was lost the device still holds the older secret,
        and a second rotation must keep that one valid. None if the proven
        secret is no longer valid.
        """
        if _same(proven, credential_id(self.current)):
            return SecretState(new_hash, self.current, now + overlap)
        previous = self.live_previous(now)
        if previous is not None and _same(proven, credential_id(previous)):
            return SecretState(new_hash, previous, now + overlap)
        return None
