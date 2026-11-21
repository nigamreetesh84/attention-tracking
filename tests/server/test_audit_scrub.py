"""Audit detail scrubbing and canonical hashing (pure, no database).

security-privacy 5: audit entries never contain image data, PINs, passwords,
tokens or secrets. Scrubbing redacts by key name and by value pattern and is
applied on every write path, so a careless caller cannot leak one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

import pytest
from ag_server.audit.chain import AuditRow, canonical_bytes, compute_hash
from ag_server.audit.scrub import (
    MAX_STRING_LENGTH,
    REDACTED,
    AuditDetailError,
    is_sensitive_key,
    scrub_detail,
)


@pytest.mark.parametrize(
    "key",
    [
        "pin",
        "PIN",
        "new_pin",
        "pin_hash",
        "password",
        "Password",
        "old_password",
        "password_hash",
        "passwd",
        "passphrase",
        "secret",
        "device_secret",
        "device_secret_hash",
        "totp_secret_encrypted",
        "token",
        "access_token",
        "enrollmentToken",
        "refresh-token",
        "api_key",
        "apiKey",
        "private_key",
        "authorization",
        "cookie",
        "set_cookie",
        "credentials",
        "recovery_codes",
        "kek",
        "jwt",
        "otp",
        "image_bytes",
        "image_data",
        "jpeg",
        "frame",
        "still",
        "image",
        "thumbnail",
        "png_b64",
        "blob",
    ],
)
def test_sensitive_key_names_are_detected(key: str) -> None:
    assert is_sensitive_key(key), key


@pytest.mark.parametrize(
    "key",
    [
        "pinned",
        "shipping_line",
        "mapping",
        "spinner",
        "evidence_image_id",
        "image_ids",
        "images_withheld",
        "known_worker",
        "method",
        "locked",
        "worker_session_id",
        "employee_id",
        "camera_id",
        "sha256",
        "display_name",
        "secretary",  # a word, not "secret"
    ],
)
def test_ordinary_key_names_are_kept(key: str) -> None:
    assert not is_sensitive_key(key), key


def test_sensitive_keys_are_redacted_at_any_depth() -> None:
    detail = {
        "user": {"display_name": "A", "pin_hash": "$argon2id$v=19$abc", "password": "hunter2"},
        "items": [{"token": "abc", "id": 3}, {"api_key": "k"}],
        "ok": True,
    }
    out = scrub_detail(detail)
    assert out == {
        "user": {"display_name": "A", "pin_hash": REDACTED, "password": REDACTED},
        "items": [{"token": REDACTED, "id": 3}, {"api_key": REDACTED}],
        "ok": True,
    }
    # The input is not modified.
    assert detail["ok"] is True
    assert isinstance(detail["user"], dict) and detail["user"]["password"] == "hunter2"  # noqa: S105 - test data


def test_redacted_null_stays_null() -> None:
    # A null secret (for example no PIN set) carries nothing and keeps the shape honest.
    assert scrub_detail({"pin_hash": None}) == {"pin_hash": None}


@pytest.mark.parametrize(
    "value",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "Bearer abcdef0123456789",
        "bearer xyz.abc",
        "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG",
        "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ",
        "/9j/" + "A" * 300,
        "iVBORw0KGgo" + "A" * 300,
        "QUJD" * 40,  # a long base64 run
    ],
)
def test_secret_looking_values_are_redacted(value: str) -> None:
    out = scrub_detail({"note": value})
    assert out == {"note": REDACTED}


def test_credentials_in_urls_are_redacted_but_the_url_is_kept() -> None:
    out = scrub_detail({"uri": "rtsp://admin:s3cret@10.0.0.5:554/stream1"})
    assert out == {"uri": f"rtsp://{REDACTED}@10.0.0.5:554/stream1"}
    assert "s3cret" not in str(out)
    assert "admin" not in str(out)


def test_secret_inside_a_longer_string_is_redacted() -> None:
    out = scrub_detail({"msg": "header was Authorization: Bearer abc123def and more"})
    assert "abc123def" not in str(out)


def test_ordinary_values_are_kept() -> None:
    sha = "ab" * 32  # evidence SHA-256 hex is useful audit data
    detail = {
        "sha256": sha,
        "reason": "camera moved",
        "threshold": 0.35,
        "count": 4,
        "url": "https://example.test/path",
        "nothing": None,
    }
    assert scrub_detail(detail) == detail


def test_bytes_are_never_stored() -> None:
    out = scrub_detail({"note": b"\xff\xd8\xff\xe0", "nested": [bytearray(b"x"), memoryview(b"y")]})
    assert out == {"note": REDACTED, "nested": [REDACTED, REDACTED]}


def test_very_long_strings_are_redacted() -> None:
    out = scrub_detail({"note": "a b " * MAX_STRING_LENGTH})
    assert out == {"note": REDACTED}


def test_common_types_become_json() -> None:
    class Colour(StrEnum):
        RED = "red"

    uid = uuid.UUID("01890000-0000-7000-8000-000000000001")
    at = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    out = scrub_detail({"id": uid, "at": at, "c": Colour.RED, "t": (1, 2), "s": {"x": 1}})
    assert out == {
        "id": str(uid),
        "at": "2026-11-22T10:00:00.000000+00:00",
        "c": "red",
        "t": [1, 2],
        "s": {"x": 1},
    }


def test_unsupported_values_are_rejected() -> None:
    with pytest.raises(AuditDetailError):
        scrub_detail({"x": object()})
    with pytest.raises(AuditDetailError):
        scrub_detail({"x": float("nan")})
    with pytest.raises(AuditDetailError):
        scrub_detail({1: "non-string key"})  # type: ignore[dict-item]  # deliberately wrong
    with pytest.raises(AuditDetailError):
        scrub_detail({"x": "nul\x00byte"})  # PostgreSQL JSONB cannot store U+0000


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(AuditDetailError):
        scrub_detail({"at": datetime(2026, 1, 1)})  # naive on purpose


def test_too_deep_detail_is_rejected() -> None:
    deep: dict[str, object] = {}
    node = deep
    for _ in range(50):
        child: dict[str, object] = {}
        node["n"] = child
        node = child
    with pytest.raises(AuditDetailError):
        scrub_detail(deep)


# --- canonical form and hash -------------------------------------------------


def _row(**over: object) -> AuditRow:
    base: dict[str, object] = {
        "id": 7,
        "at": datetime(2026, 11, 22, 10, 0, 0, 123456, tzinfo=UTC),
        "actor_user_id": uuid.UUID("01890000-0000-7000-8000-000000000001"),
        "actor_workstation_id": None,
        "site_id": uuid.UUID("01890000-0000-7000-8000-000000000002"),
        "action": "evidence.view",
        "object_type": "evidence_image",
        "object_id": "abc",
        "ip": "10.0.0.1",
        "detail": {"b": 1, "a": [1, "x"]},
    }
    base.update(over)
    return AuditRow(**base)  # type: ignore[arg-type]  # test helper builds from a dict


def test_canonical_form_is_stable_and_key_order_independent() -> None:
    a = _row(detail={"b": 1, "a": 2})
    b = _row(detail={"a": 2, "b": 1})
    assert canonical_bytes(a) == canonical_bytes(b)
    # Timezone representation does not matter, only the instant.
    ist = timezone(timedelta(hours=5, minutes=30))
    c = _row(at=datetime(2026, 11, 22, 15, 30, 0, 123456, tzinfo=ist))
    assert canonical_bytes(c) == canonical_bytes(_row())


@pytest.mark.parametrize(
    "change",
    [
        {"id": 8},
        {"at": datetime(2026, 11, 22, 10, 0, 0, 123457, tzinfo=UTC)},
        {"actor_user_id": None},
        {"actor_workstation_id": uuid.UUID(int=5)},
        {"site_id": None},
        {"action": "evidence.export"},
        {"object_type": "attention_event"},
        {"object_id": "abd"},
        {"ip": "10.0.0.2"},
        {"detail": {"b": 2, "a": [1, "x"]}},
    ],
)
def test_every_field_is_covered_by_the_hash(change: dict[str, object]) -> None:
    assert compute_hash(None, _row(**change)) != compute_hash(None, _row())


def test_hash_chains_on_the_previous_hash() -> None:
    first = compute_hash(None, _row())
    assert len(first) == 32
    assert compute_hash(first, _row()) != first
    assert compute_hash(first, _row()) == compute_hash(first, _row())
    assert compute_hash(b"\x00" * 32, _row()) != compute_hash(None, _row())
