"""TOTP and recovery codes (security-privacy 3): pure logic, injected time."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from ag_server.auth.mfa import (
    RECOVERY_CODE_COUNT,
    TOTP_PERIOD,
    MfaState,
    generate_recovery_codes,
    new_pending_state,
    normalize_recovery_code,
    open_mfa,
    provisioning_uri,
    seal_mfa,
    use_recovery_code,
    verify_totp,
)
from ag_server.crypto import EnvelopeError, KeyRing

NOW = datetime(2026, 11, 22, 9, 0, 7, tzinfo=UTC)
RING = KeyRing.from_secrets("kkkkkkkkZx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9")
USER = uuid.UUID("01890000-0000-7000-8000-0000000000ad")


def code_at(state: MfaState, when: datetime) -> str:
    return pyotp.TOTP(state.secret).at(when)


def test_pending_state_has_a_strong_secret_and_no_codes() -> None:
    s = new_pending_state()
    assert len(pyotp.TOTP(s.secret).byte_secret()) >= 20
    assert not s.confirmed
    assert s.recovery_hashes == ()
    assert new_pending_state().secret != s.secret


def test_current_code_verifies_and_returns_its_step() -> None:
    s = new_pending_state()
    step = verify_totp(s, code_at(s, NOW), NOW)
    assert step == int(NOW.timestamp()) // TOTP_PERIOD


def test_one_step_of_clock_skew_is_tolerated_but_not_two() -> None:
    s = new_pending_state()
    assert verify_totp(s, code_at(s, NOW - timedelta(seconds=30)), NOW) is not None
    assert verify_totp(s, code_at(s, NOW + timedelta(seconds=30)), NOW) is not None
    assert verify_totp(s, code_at(s, NOW - timedelta(seconds=90)), NOW) is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 5"])
def test_malformed_codes_are_rejected(bad: str) -> None:
    assert verify_totp(new_pending_state(), bad, NOW) is None


def test_code_with_spaces_is_accepted() -> None:
    s = new_pending_state()
    c = code_at(s, NOW)
    assert verify_totp(s, f" {c[:3]} {c[3:]} ", NOW) is not None


def test_a_used_step_cannot_be_replayed() -> None:
    s = new_pending_state()
    c = code_at(s, NOW)
    step = verify_totp(s, c, NOW)
    assert step is not None
    used = s.after_totp(step)
    assert verify_totp(used, c, NOW) is None
    # An older (still in-window) code is also refused after a newer one was used.
    assert verify_totp(used, code_at(s, NOW - timedelta(seconds=30)), NOW) is None
    later = NOW + timedelta(seconds=30)
    assert verify_totp(used, code_at(s, later), later) is not None


def test_recovery_codes_are_distinct_and_well_formed() -> None:
    codes = generate_recovery_codes()
    assert len(codes) == RECOVERY_CODE_COUNT
    assert len(set(codes)) == len(codes)
    for c in codes:
        assert normalize_recovery_code(c) is not None
        assert len(c.replace("-", "")) == 16


def test_recovery_code_normalization() -> None:
    c = generate_recovery_codes(1)[0]
    assert normalize_recovery_code(c.upper().replace("-", " ")) == normalize_recovery_code(c)
    assert normalize_recovery_code("123456") is None
    assert normalize_recovery_code("") is None


def test_recovery_code_is_single_use() -> None:
    codes = generate_recovery_codes()
    s = new_pending_state().confirm().with_recovery_codes(codes)
    assert all(c not in repr(s) for c in codes)
    after = use_recovery_code(s, codes[3])
    assert after is not None
    assert len(after.recovery_hashes) == RECOVERY_CODE_COUNT - 1
    assert use_recovery_code(after, codes[3]) is None
    assert use_recovery_code(after, codes[4]) is not None
    assert use_recovery_code(s, "aaaa-aaaa-aaaa-aaaa") is None


def test_recovery_hashes_are_not_the_codes() -> None:
    codes = generate_recovery_codes()
    s = new_pending_state().with_recovery_codes(codes)
    joined = "".join(s.recovery_hashes)
    for c in codes:
        norm = normalize_recovery_code(c)
        assert norm is not None
        assert norm not in joined


def test_sealed_state_round_trips_and_hides_secrets() -> None:
    codes = generate_recovery_codes()
    s = new_pending_state().confirm().with_recovery_codes(codes).after_totp(123)
    blob = seal_mfa(RING, USER, s)
    assert s.secret.encode() not in blob
    assert open_mfa(RING, USER, blob) == s


def test_sealed_state_is_bound_to_its_user() -> None:
    blob = seal_mfa(RING, USER, new_pending_state())
    with pytest.raises(EnvelopeError):
        open_mfa(RING, uuid.uuid4(), blob)


def test_state_repr_hides_the_secret() -> None:
    s = new_pending_state()
    assert s.secret not in repr(s)


def test_provisioning_uri_names_issuer_and_account() -> None:
    s = new_pending_state()
    uri = provisioning_uri(s, "asha")
    assert uri.startswith("otpauth://totp/")
    assert "AttentionGuard" in uri
    assert "asha" in uri
