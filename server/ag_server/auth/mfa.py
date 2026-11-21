"""TOTP second factor and single-use recovery codes (security-privacy 3).

All MFA material of a user lives in one encrypted value,
`user.totp_secret_encrypted` (KEK envelope, bound to the user id):

- the TOTP secret (base32, 160 bits) and whether enrollment was confirmed;
- the last accepted TOTP time step, so a code cannot be replayed;
- salted SHA-256 hashes of the unused recovery codes (80 random bits each,
  so a fast salted hash is enough; the hashes are also inside the envelope).

Decision (task 3.4): schema.md has no recovery-code table; keeping the hashes
in the encrypted MFA value avoids a schema change and lets one row lock make
"use a code" atomic. A dedicated table is proposed to the owner in PROGRESS.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final

import pyotp

from ag_server.crypto import KeyRing, open_sealed, seal

TOTP_PERIOD: Final = 30
TOTP_DIGITS: Final = 6
# Accept the previous and next step for clock skew between phone and server.
TOTP_WINDOW: Final = 1
ISSUER: Final = "AttentionGuard"
RECOVERY_CODE_COUNT: Final = 10
# No 0/1/i/l/o: codes are read off paper.
_RECOVERY_ALPHABET: Final = "abcdefghjkmnpqrstuvwxyz23456789"
_RECOVERY_LEN: Final = 16
_RECOVERY_GROUP: Final = 4
_RECOVERY_RE: Final = re.compile(rf"[{_RECOVERY_ALPHABET}]{{{_RECOVERY_LEN}}}")
_CODE_RE: Final = re.compile(rf"[0-9]{{{TOTP_DIGITS}}}")
_STATE_VERSION: Final = 1


@dataclass(frozen=True)
class MfaState:
    """Decrypted MFA material of one user. Never logged: secrets hidden from repr."""

    secret: str = field(repr=False)
    confirmed: bool = False
    last_step: int | None = None
    recovery_salt: str = field(default="", repr=False)
    recovery_hashes: tuple[str, ...] = field(default=(), repr=False)

    def confirm(self) -> MfaState:
        return replace(self, confirmed=True)

    def after_totp(self, step: int) -> MfaState:
        """The state once the code of `step` has been used."""
        return replace(self, last_step=step)

    def with_recovery_codes(self, codes: list[str]) -> MfaState:
        """Replace the recovery codes (new salt; old codes stop working)."""
        salt = secrets.token_hex(16)
        hashes: list[str] = []
        for code in codes:
            norm = normalize_recovery_code(code)
            if norm is None:
                raise ValueError("not a recovery code")
            hashes.append(_hash_code(salt, norm))
        return replace(self, recovery_salt=salt, recovery_hashes=tuple(hashes))


def _hash_code(salt: str, normalized: str) -> str:
    return hashlib.sha256(f"{salt}:{normalized}".encode()).hexdigest()


def new_pending_state() -> MfaState:
    """A fresh, unconfirmed TOTP secret for enrollment."""
    return MfaState(secret=pyotp.random_base32(32))


def provisioning_uri(state: MfaState, username: str) -> str:
    """The `otpauth://` URI an authenticator app imports."""
    return pyotp.TOTP(state.secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD).provisioning_uri(
        name=username, issuer_name=ISSUER
    )


def verify_totp(state: MfaState, code: str, now: datetime) -> int | None:
    """The matched time step if `code` is valid at `now` and newer than the last used one."""
    digits = "".join(code.split())
    if not _CODE_RE.fullmatch(digits):
        return None
    totp = pyotp.TOTP(state.secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD)
    current = int(now.timestamp()) // TOTP_PERIOD
    matched: int | None = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        # Compare every candidate so the time taken does not depend on which matched.
        if hmac.compare_digest(totp.at(step * TOTP_PERIOD), digits):
            matched = step
    if matched is None or (state.last_step is not None and matched <= state.last_step):
        return None
    return matched


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """`count` distinct random codes like `abcd-efgh-jkmn-pqrs`."""
    codes: set[str] = set()
    while len(codes) < count:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LEN))
        codes.add(
            "-".join(raw[i : i + _RECOVERY_GROUP] for i in range(0, _RECOVERY_LEN, _RECOVERY_GROUP))
        )
    return sorted(codes)


def normalize_recovery_code(raw: str) -> str | None:
    """Lower-case, separators removed; None if it cannot be a recovery code."""
    value = re.sub(r"[\s-]", "", raw).lower()
    return value if _RECOVERY_RE.fullmatch(value) else None


def use_recovery_code(state: MfaState, raw: str) -> MfaState | None:
    """The state without the matching code, or None if `raw` is not an unused code."""
    norm = normalize_recovery_code(raw)
    if norm is None or not state.recovery_salt:
        return None
    candidate = _hash_code(state.recovery_salt, norm)
    remaining = [h for h in state.recovery_hashes if not hmac.compare_digest(h, candidate)]
    if len(remaining) == len(state.recovery_hashes):
        return None
    return replace(state, recovery_hashes=tuple(remaining))


def _aad(user_id: uuid.UUID) -> bytes:
    return b"user.totp_secret_encrypted:" + user_id.bytes


def seal_mfa(ring: KeyRing, user_id: uuid.UUID, state: MfaState) -> bytes:
    """Encrypt `state` for `user.totp_secret_encrypted` of `user_id`."""
    doc = {
        "v": _STATE_VERSION,
        "secret": state.secret,
        "confirmed": state.confirmed,
        "last_step": state.last_step,
        "recovery_salt": state.recovery_salt,
        "recovery_hashes": list(state.recovery_hashes),
    }
    return seal(ring, json.dumps(doc, separators=(",", ":")).encode(), _aad(user_id))


def open_mfa(ring: KeyRing, user_id: uuid.UUID, blob: bytes) -> MfaState:
    """Decrypt the MFA state of `user_id`. Raises `EnvelopeError` if it does not open."""
    doc = json.loads(open_sealed(ring, blob, _aad(user_id)))
    if doc.get("v") != _STATE_VERSION:
        raise ValueError("unknown MFA state version")
    last = doc["last_step"]
    return MfaState(
        secret=str(doc["secret"]),
        confirmed=bool(doc["confirmed"]),
        last_step=None if last is None else int(last),
        recovery_salt=str(doc["recovery_salt"]),
        recovery_hashes=tuple(str(h) for h in doc["recovery_hashes"]),
    )
