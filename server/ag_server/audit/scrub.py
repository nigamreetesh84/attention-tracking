"""Remove secrets and image data from audit detail before it is stored.

security-privacy 5: audit entries never contain image data, PINs, passwords,
tokens or secrets. `scrub_detail` runs on every write (`write_audit`), so a
caller that passes a whole record cannot leak one. Two independent nets:

- by key name: a value under a key such as `pin_hash`, `password`,
  `device_secret`, `enrollmentToken`, `api_key` or `image_bytes` is replaced
  by `REDACTED` (a null stays null);
- by value: bytes, JWTs, bearer and basic credentials, password hashes, data
  URIs, long base64 runs and over-long strings are replaced by `REDACTED`,
  and credentials inside URLs (`rtsp://user:pass@host`) are cut out.

Values are also converted to plain JSON (UUID, datetime, enum, IP address,
pydantic model); anything else is rejected with `AuditDetailError`, because
silently stringifying an unknown object could leak its contents.
"""

from __future__ import annotations

import ipaddress
import itertools
import math
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel

from ag_server.audit.errors import AuditDetailError

__all__ = [
    "MAX_STRING_LENGTH",
    "REDACTED",
    "AuditDetailError",
    "JsonValue",
    "is_sensitive_key",
    "scrub_detail",
]

REDACTED: Final = "[redacted]"
MAX_STRING_LENGTH: Final = 2048
MAX_DEPTH: Final = 32

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

# Words that mark a key as holding a secret. Matched against whole words of
# the key (split on separators and camelCase), so `pinned` or `secretary`
# are not secrets but `new_pin` and `deviceSecret` are.
_SECRET_WORDS: Final = frozenset(
    {
        "pin",
        "pins",
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "pwd",
        "secret",
        "secrets",
        "token",
        "tokens",
        "totp",
        "otp",
        "cookie",
        "cookies",
        "authorization",
        "credential",
        "credentials",
        "kek",
        "dek",
        "jwt",
        "recovery",
    }
)
_SECRET_PAIRS: Final = frozenset(
    {("api", "key"), ("private", "key"), ("access", "key"), ("signing", "key")}
)
# Raw media or binary payloads.
_BINARY_WORDS: Final = frozenset({"jpeg", "jpg", "png", "bytes", "blob", "b64", "base64", "pixels"})
# A key made only of these words is image content (`image`, `frame_data`);
# `evidence_image_id` or `image_ids` are references and stay.
_MEDIA_WORDS: Final = frozenset({"image", "frame", "still", "thumbnail", "snapshot", "photo"})
_MEDIA_FILLER: Final = frozenset({"data", "raw", "content", "body"})

_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

_SECRET_VALUE_PATTERNS: Final = (
    re.compile(r"eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r"\$(?:argon2(?:id|i|d)|2[aby]|scrypt|pbkdf2[^$]*)\$"),  # password hashes
    re.compile(r"(?i)\bdata:[a-z]+/[a-z0-9.+-]+;base64,"),
    re.compile(r"[A-Za-z0-9+/]{120,}={0,2}"),  # base64 blobs (images, keys)
)
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]*@")


def _words(key: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(key)]


def is_sensitive_key(key: str) -> bool:
    """True if values under `key` must never be stored (secrets or image content)."""
    words = _words(key)
    if not words:
        return False
    if any(w in _SECRET_WORDS or w in _BINARY_WORDS for w in words):
        return True
    if any(pair in _SECRET_PAIRS for pair in itertools.pairwise(words)):
        return True
    joined = "".join(words)
    if any(a + b == joined for a, b in _SECRET_PAIRS):  # apikey, privatekey
        return True
    return any(w in _MEDIA_WORDS for w in words) and all(
        w in _MEDIA_WORDS or w in _MEDIA_FILLER for w in words
    )


def _scrub_string(value: str) -> str:
    if "\x00" in value:
        raise AuditDetailError("audit detail strings cannot contain NUL characters")
    if len(value) > MAX_STRING_LENGTH:
        return REDACTED
    if any(p.search(value) for p in _SECRET_VALUE_PATTERNS):
        return REDACTED
    return _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{REDACTED}@", value)


def _scrub(value: object, depth: int) -> JsonValue:
    if depth > MAX_DEPTH:
        raise AuditDetailError(f"audit detail is nested deeper than {MAX_DEPTH} levels")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _scrub(value.value, depth)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditDetailError("audit detail numbers must be finite")
        return value
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return REDACTED
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise AuditDetailError("audit detail datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return str(value)
    if isinstance(value, BaseModel):
        return _scrub(value.model_dump(mode="json"), depth)
    if isinstance(value, Mapping):
        return _scrub_mapping(value, depth)
    if isinstance(value, list | tuple):
        return [_scrub(item, depth + 1) for item in value]
    raise AuditDetailError(f"audit detail cannot hold a {type(value).__name__}")


# Any: callers may pass mappings with non-string keys; they are rejected at runtime.
def _scrub_mapping(value: Mapping[Any, object], depth: int) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AuditDetailError("audit detail keys must be strings")
        if "\x00" in key:
            raise AuditDetailError("audit detail keys cannot contain NUL characters")
        if is_sensitive_key(key):
            out[key] = None if item is None else REDACTED
        else:
            out[key] = _scrub(item, depth + 1)
    return out


def scrub_detail(detail: Mapping[str, object]) -> dict[str, JsonValue]:
    """A JSON-safe copy of `detail` with secrets and image data redacted.

    Raises `AuditDetailError` for values that cannot be stored (non-string
    keys, NaN, naive datetimes, NUL characters, unknown types, deep nesting).
    The input is not modified.
    """
    return _scrub_mapping(detail, 0)
