"""Pure parts of device authentication (task 3.6): secrets, tokens, rate limits.

No database here; the HTTP and PostgreSQL behaviour is in `test_devices.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from ag_server.devices.credentials import (
    SecretState,
    credential_id,
    hash_enrollment_token,
    hash_secret,
    new_secret,
)
from ag_server.devices.ratelimit import Limit, SlidingWindowLimiter
from ag_server.devices.tokens import DeviceTokenCodec, DeviceTokenError

NOW = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)
KEY = "unit-test-jwt-signing-key-0123456789abcdef"
WS = uuid.UUID("01936f6e-0000-7000-8000-000000000001")
SITE = uuid.UUID("01936f6e-0000-7000-8000-0000000000aa")


# --- secrets -----------------------------------------------------------------------------


def test_new_secret_has_256_bits_and_is_url_safe() -> None:
    secrets = {new_secret() for _ in range(50)}
    assert len(secrets) == 50
    for s in secrets:
        assert len(s) >= 43  # 32 bytes in unpadded base64url
        assert all(c.isalnum() or c in "-_" for c in s)


def test_hashes_are_domain_separated_and_never_contain_the_secret() -> None:
    s = new_secret()
    assert hash_secret(s) != hash_enrollment_token(s)
    assert s not in hash_secret(s) and s not in hash_enrollment_token(s)
    assert hash_secret(s) == hash_secret(s)


def test_secret_state_round_trip_with_and_without_previous() -> None:
    cur, prev = hash_secret(new_secret()), hash_secret(new_secret())
    plain = SecretState(current=cur)
    assert SecretState.decode(plain.encode()) == plain
    rotated = SecretState(current=cur, previous=prev, previous_until=NOW)
    assert SecretState.decode(rotated.encode()) == rotated


@pytest.mark.parametrize(
    "stored", [None, "", "garbage", "v1:", "v1:zz", "v9:" + "a" * 64, "v1:" + "a" * 64 + ":b:c"]
)
def test_unreadable_secret_state_fails_closed(stored: str | None) -> None:
    assert SecretState.decode(stored) is None


def test_matching_current_and_previous_inside_the_overlap_only() -> None:
    old, new = new_secret(), new_secret()
    state = SecretState(
        current=hash_secret(new), previous=hash_secret(old), previous_until=NOW + timedelta(10)
    )
    assert state.match(new, NOW) == hash_secret(new)
    assert state.match(old, NOW) == hash_secret(old)
    until = NOW + timedelta(10)
    assert state.match(old, until - timedelta(microseconds=1)) == hash_secret(old)
    assert state.match(old, until) is None, "the overlap end is exclusive"
    assert state.match(new_secret(), NOW) is None
    assert state.match("", NOW) is None


def test_rotation_keeps_the_secret_the_caller_proved() -> None:
    s0, s1, s2 = (hash_secret(new_secret()) for _ in range(3))
    state = SecretState(current=s0)
    first = state.rotate(s1, proven=credential_id(s0), now=NOW, overlap=timedelta(minutes=10))
    assert first == SecretState(s1, s0, NOW + timedelta(minutes=10))
    # The answer was lost: the device still holds s0 and rotates again with it.
    again = first.rotate(s2, proven=credential_id(s0), now=NOW, overlap=timedelta(minutes=10))
    assert again == SecretState(s2, s0, NOW + timedelta(minutes=10))
    # Proving s1 (the device stored it) drops s0.
    third = first.rotate(s2, proven=credential_id(s1), now=NOW, overlap=timedelta(minutes=10))
    assert third == SecretState(s2, s1, NOW + timedelta(minutes=10))


def test_rotation_refuses_a_secret_no_longer_valid() -> None:
    s0, s1, s2 = (hash_secret(new_secret()) for _ in range(3))
    state = SecretState(s1, s0, NOW)
    assert state.rotate(s2, proven=credential_id(s0), now=NOW, overlap=timedelta(1)) is None
    assert state.rotate(s2, proven="0" * 16, now=NOW, overlap=timedelta(1)) is None


# --- access tokens -----------------------------------------------------------------------


def codec() -> DeviceTokenCodec:
    return DeviceTokenCodec(KEY, ttl=timedelta(minutes=15))


def test_token_carries_the_contract_claims() -> None:
    token, expires_in = codec().issue(WS, SITE, "c" * 16, NOW)
    assert expires_in == 900
    claims = jwt.decode(
        token,
        KEY,
        algorithms=["HS256"],
        options={
            "verify_signature": True,
            "verify_exp": False,
            "verify_aud": False,
            "verify_iat": False,
        },
    )
    assert claims["sub"] == str(WS)
    assert claims["site_id"] == str(SITE)
    assert claims["scope"] == "device"
    assert claims["exp"] - claims["iat"] == 900
    verified = codec().verify(token, NOW + timedelta(minutes=14))
    assert (verified.workstation_id, verified.site_id, verified.credential) == (WS, SITE, "c" * 16)


def test_token_expiry_uses_the_injected_clock() -> None:
    token, _ = codec().issue(WS, SITE, "c" * 16, NOW)
    codec().verify(token, NOW + timedelta(seconds=899))
    with pytest.raises(DeviceTokenError):
        codec().verify(token, NOW + timedelta(seconds=900))
    with pytest.raises(DeviceTokenError):
        codec().verify(token, NOW - timedelta(minutes=5))  # issued in the future


def _forge(claims: dict[str, object], key: str = KEY, alg: str = "HS256") -> str:
    return jwt.encode(claims, key, algorithm=alg)


def _claims(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": "attentionguard",
        "aud": "attentionguard:device",
        "sub": str(WS),
        "site_id": str(SITE),
        "scope": "device",
        "cred": "c" * 16,
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 900,
        "jti": uuid.uuid4().hex,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_the_forge_helper_makes_a_valid_token() -> None:
    assert codec().verify(_forge(_claims()), NOW).workstation_id == WS


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(
            lambda: _forge(_claims(), key="another-key-of-the-same-length-0123456"), id="key"
        ),
        pytest.param(lambda: _forge(_claims(scope="console")), id="scope"),
        pytest.param(lambda: _forge(_claims(aud="attentionguard:console")), id="aud"),
        pytest.param(lambda: _forge(_claims(iss="someone")), id="iss"),
        pytest.param(lambda: _forge(_claims(sub="not-a-uuid")), id="sub"),
        pytest.param(lambda: _forge(_claims(site_id=None)), id="no-site"),
        pytest.param(lambda: _forge(_claims(cred=None)), id="no-cred"),
        pytest.param(lambda: _forge(_claims(exp=None)), id="no-exp"),
        pytest.param(lambda: _forge(_claims(), alg="HS512"), id="alg"),
        pytest.param(
            lambda: jwt.encode(_claims(), None, algorithm="none"),  # type: ignore[arg-type]  # forging alg=none on purpose
            id="none",
        ),
        pytest.param(lambda: "not.a.jwt", id="garbage"),
        pytest.param(lambda: "", id="empty"),
    ],
)
def test_bad_tokens_are_refused(token: object) -> None:
    assert callable(token)
    with pytest.raises(DeviceTokenError):
        codec().verify(token(), NOW)


def test_codec_refuses_a_short_key() -> None:
    with pytest.raises(ValueError):
        DeviceTokenCodec("short", ttl=timedelta(minutes=15))


# --- rate limits -------------------------------------------------------------------------


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_limiter_allows_count_then_refuses_with_retry_after() -> None:
    mono = Mono()
    lim = SlidingWindowLimiter(mono)
    limit = Limit(3, 60.0)
    assert [lim.hit("k", limit) for _ in range(3)] == [None, None, None]
    retry = lim.hit("k", limit)
    assert retry is not None and 0 < retry <= 60
    mono.t += 59.0
    assert lim.hit("k", limit) is not None, "still inside the window"
    mono.t += 1.0
    assert lim.hit("k", limit) is None, "the oldest hit left the window"


def test_refused_hits_do_not_extend_the_window() -> None:
    mono = Mono()
    lim = SlidingWindowLimiter(mono)
    limit = Limit(1, 10.0)
    assert lim.hit("k", limit) is None
    for _ in range(20):
        mono.t += 0.4
        assert lim.hit("k", limit) is not None
    mono.t += 2.5  # past 1010 despite float steps
    assert lim.hit("k", limit) is None


def test_limiter_keys_are_independent() -> None:
    lim = SlidingWindowLimiter(Mono())
    limit = Limit(1, 60.0)
    assert lim.hit("a", limit) is None
    assert lim.hit("b", limit) is None
    assert lim.hit("a", limit) is not None


def test_limiter_memory_is_bounded() -> None:
    mono = Mono()
    lim = SlidingWindowLimiter(mono, max_keys=100)
    limit = Limit(1, 60.0)
    for i in range(1000):
        mono.t += 0.001
        lim.hit(f"k{i}", limit)
    assert lim.size <= 100
    # The most recent keys are kept, so an active client stays limited.
    assert lim.hit("k999", limit) is not None


def test_limit_validation() -> None:
    with pytest.raises(ValueError):
        Limit(0, 60.0)
    with pytest.raises(ValueError):
        Limit(1, 0.0)


def test_holds_names_only_valid_secrets() -> None:
    s0, s1 = hash_secret(new_secret()), hash_secret(new_secret())
    state = SecretState(s1, s0, NOW + timedelta(minutes=10))
    assert state.holds(credential_id(s1), NOW)
    assert state.holds(credential_id(s0), NOW)
    assert not state.holds(credential_id(s0), NOW + timedelta(minutes=10))
    assert not state.holds("0" * 16, NOW)
