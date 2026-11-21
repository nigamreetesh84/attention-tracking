"""Credential checks behind a plug-in interface (security-privacy 3).

An `AuthProvider` answers one question: which local user do these
credentials belong to, and are they right? Sessions, MFA, lockout and RBAC
sit above it, so LDAP/AD/SSO can be added in release 2 without touching them.
"""

from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass
from typing import Final, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ag_server.auth.passwords import MAX_LENGTH
from ag_server.models import User

MAX_USERNAME_LENGTH: Final = 128


@dataclass(frozen=True)
class CredentialCheck:
    """`user` is the matched row (locked FOR UPDATE) or None; `password_ok` only if it matched."""

    user: User | None
    password_ok: bool


class AuthProvider(Protocol):
    """Checks console credentials. Implementations must do the same work for
    unknown and known users, so response time does not reveal which names exist."""

    name: str

    def authenticate(self, db: Session, username: str, password: str) -> CredentialCheck: ...


def normalize_username(raw: str) -> str | None:
    """NFKC, surrounding space stripped; None if empty or too long."""
    value = unicodedata.normalize("NFKC", raw).strip()
    return value if 0 < len(value) <= MAX_USERNAME_LENGTH else None


class LocalAuthProvider:
    """Local accounts: `user.username` + argon2id `user.password_hash`."""

    name = "local"

    def __init__(self, hasher: PasswordHasher) -> None:
        self._hasher = hasher
        # Same parameters as real hashes, so a miss costs the same as a wrong password.
        self._dummy_hash = hasher.hash(secrets.token_urlsafe(24))

    def authenticate(self, db: Session, username: str, password: str) -> CredentialCheck:
        name = normalize_username(username)
        user = None
        if name is not None:
            user = db.execute(
                select(User).where(User.username == name).with_for_update()
            ).scalar_one_or_none()
        stored = user.password_hash if user is not None else None
        ok = self._verify(stored or self._dummy_hash, password)
        if user is None or stored is None:
            return CredentialCheck(user=user, password_ok=False)
        if ok and self._hasher.check_needs_rehash(stored):
            user.password_hash = self._hasher.hash(password)
        return CredentialCheck(user=user, password_ok=ok)

    def _verify(self, encoded: str, password: str) -> bool:
        # Over-long input is hashed as a fixed string: same cost, never a match.
        candidate = password if len(password) <= MAX_LENGTH else secrets.token_urlsafe(16)
        try:
            return self._hasher.verify(encoded, candidate) and candidate is password
        except (VerificationError, InvalidHashError):
            return False
