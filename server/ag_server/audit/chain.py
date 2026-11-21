"""Canonical form and hash of an audit row (pure; no database).

`hash = sha256(prev_hash || canonical_row)` (docs/schema.md). The canonical
row is compact JSON with sorted keys over every stored column except the two
hashes, with a format version, so any change to any field breaks the hash.
`at` is the UTC instant with microseconds (PostgreSQL's precision), UUIDs
are lower-case strings and IPs are in Python's normal form. The detail must
already be in database normal form (see `writer.write_audit`).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

CANONICAL_VERSION: Final = 1


@dataclass(frozen=True)
class AuditRow:
    """The hashed content of one `audit_log` row."""

    id: int
    at: datetime
    actor_user_id: uuid.UUID | None
    actor_workstation_id: uuid.UUID | None
    site_id: uuid.UUID | None
    action: str
    object_type: str | None
    object_id: str | None
    ip: str | None
    # Any: stored JSONB as loaded from the database (validated JSON by construction).
    detail: dict[str, Any]


def normalize_ip(value: object) -> str | None:
    """Text form of an INET value as loaded or as given (None stays None)."""
    if value is None:
        return None
    text = str(value)
    if "/" in text:
        return str(ipaddress.ip_interface(text))
    return str(ipaddress.ip_address(text))


def _uuid(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def canonical_bytes(row: AuditRow) -> bytes:
    """The canonical serialization of `row` that the hash covers."""
    if row.at.tzinfo is None:
        raise ValueError("audit time must be timezone-aware")
    doc = {
        "v": CANONICAL_VERSION,
        "id": row.id,
        "at": row.at.astimezone(UTC).isoformat(timespec="microseconds"),
        "actor_user_id": _uuid(row.actor_user_id),
        "actor_workstation_id": _uuid(row.actor_workstation_id),
        "site_id": _uuid(row.site_id),
        "action": row.action,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "ip": normalize_ip(row.ip),
        "detail": row.detail,
    }
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def compute_hash(prev_hash: bytes | None, row: AuditRow) -> bytes:
    """`sha256(prev_hash || canonical_row)`; the first row of a chain has no prev_hash."""
    return hashlib.sha256((prev_hash or b"") + canonical_bytes(row)).digest()
