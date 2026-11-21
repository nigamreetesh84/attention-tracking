"""Console password hashing and policy (security-privacy 3).

Policy: at least 12 characters, not on a small common-password list, does not
contain the username, not one of the last 5 passwords. Hashes are argon2id.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH: Final = 12
# Bounds the hashing work an attacker can request per attempt.
MAX_LENGTH: Final = 256
HISTORY_DEPTH: Final = 5

# Frequent passwords of 12+ characters (shorter ones already fail the length
# rule), lower-case; the check folds case and strips surrounding space.
COMMON_PASSWORDS: Final[frozenset[str]] = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456789012345",
        "1234567890qwerty",
        "1q2w3e4r5t6y",
        "1qaz2wsx3edc",
        "abc123456789",
        "abcdefghijkl",
        "administrator",
        "admin1234567",
        "admin@123456",
        "attentionguard",
        "changeme1234",
        "iloveyou1234",
        "letmein12345",
        "password1234",
        "password12345",
        "password123456",
        "password@123",
        "password@1234",
        "passw0rd1234",
        "p@ssw0rd1234",
        "qwerty123456",
        "qwertyuiop12",
        "qwertyuiop123",
        "qwertyuiopasdf",
        "superman1234",
        "supervisor123",
        "supervisor1234",
        "welcome12345",
        "welcome@1234",
        "zaq12wsxcde3",
        "000000000000",
        "111111111111",
        "aaaaaaaaaaaa",
        "india@123456",
        "warehouse123",
        "warehouse1234",
    }
)


class PolicyViolation(StrEnum):
    """Why a new password is refused; also the i18n key suffix."""

    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    COMMON = "common"
    CONTAINS_USERNAME = "contains_username"
    REUSED = "reused"


def default_password_hasher() -> PasswordHasher:
    """argon2id with the argon2-cffi defaults (RFC 9106 low-memory profile)."""
    return PasswordHasher()


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _matches(hasher: PasswordHasher, encoded: str, password: str) -> bool:
    try:
        return hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def check_password_policy(
    password: str,
    *,
    username: str,
    previous_hashes: Sequence[str],
    hasher: PasswordHasher,
) -> list[PolicyViolation]:
    """Every rule `password` breaks; empty means acceptable.

    `previous_hashes` is newest first; only the first `HISTORY_DEPTH` count.
    """
    violations: list[PolicyViolation] = []
    if len(password) < MIN_LENGTH:
        violations.append(PolicyViolation.TOO_SHORT)
    if len(password) > MAX_LENGTH:
        violations.append(PolicyViolation.TOO_LONG)
        return violations
    folded = _fold(password)
    if folded in COMMON_PASSWORDS:
        violations.append(PolicyViolation.COMMON)
    name = _fold(username)
    if len(name) >= 3 and name in folded:
        violations.append(PolicyViolation.CONTAINS_USERNAME)
    if any(_matches(hasher, h, password) for h in previous_hashes[:HISTORY_DEPTH]):
        violations.append(PolicyViolation.REUSED)
    return violations
