"""Console password policy and hashing (security-privacy 3)."""

from __future__ import annotations

import pytest
from ag_server.auth.passwords import (
    COMMON_PASSWORDS,
    HISTORY_DEPTH,
    MAX_LENGTH,
    MIN_LENGTH,
    PolicyViolation,
    check_password_policy,
    default_password_hasher,
)
from argon2 import PasswordHasher, Type, extract_parameters

FAST = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)
GOOD = "violet-tractor-87-lantern"


def test_policy_constants_match_the_design() -> None:
    assert MIN_LENGTH == 12
    assert HISTORY_DEPTH == 5


def test_a_good_password_passes() -> None:
    assert check_password_policy(GOOD, username="asha", previous_hashes=[], hasher=FAST) == []


def test_too_short_is_rejected() -> None:
    v = check_password_policy("Abcdef12345", username="u", previous_hashes=[], hasher=FAST)
    assert PolicyViolation.TOO_SHORT in v


def test_twelve_characters_is_enough() -> None:
    assert (
        check_password_policy("qz7!kq8@mv3#", username="u", previous_hashes=[], hasher=FAST) == []
    )


def test_too_long_is_rejected() -> None:
    v = check_password_policy("a" * (MAX_LENGTH + 1), username="u", previous_hashes=[], hasher=FAST)
    assert PolicyViolation.TOO_LONG in v


@pytest.mark.parametrize("pw", ["password1234", "Password1234", "  123456789012  ", "qwertyuiop12"])
def test_common_passwords_are_rejected_case_insensitively(pw: str) -> None:
    v = check_password_policy(pw, username="u", previous_hashes=[], hasher=FAST)
    assert PolicyViolation.COMMON in v


def test_common_list_holds_only_long_enough_entries() -> None:
    # Entries shorter than the minimum are already rejected by length.
    assert COMMON_PASSWORDS
    assert all(len(p) >= MIN_LENGTH for p in COMMON_PASSWORDS)
    assert all(p == p.lower() for p in COMMON_PASSWORDS)


def test_password_containing_the_username_is_rejected() -> None:
    v = check_password_policy(
        "supervisor-ravi-2026", username="Ravi", previous_hashes=[], hasher=FAST
    )
    assert PolicyViolation.CONTAINS_USERNAME in v


def test_reuse_of_recent_passwords_is_rejected() -> None:
    history = [FAST.hash(f"older-password-{i}") for i in range(4)] + [FAST.hash(GOOD)]
    v = check_password_policy(GOOD, username="u", previous_hashes=history, hasher=FAST)
    assert v == [PolicyViolation.REUSED]


def test_only_the_last_five_count() -> None:
    history = [FAST.hash(GOOD)] + [FAST.hash(f"older-password-{i}") for i in range(5)]
    # Newest first: GOOD is the sixth entry, so it may be used again.
    v = check_password_policy(GOOD, username="u", previous_hashes=history[::-1], hasher=FAST)
    assert v == []


def test_default_hasher_is_argon2id() -> None:
    params = extract_parameters(default_password_hasher().hash("x" * 12))
    assert params.type is Type.ID
    assert params.memory_cost >= 19 * 1024
