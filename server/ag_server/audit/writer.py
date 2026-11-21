"""Append-only audit writer with one hash chain per site.

Each site has its own chain (rows with a null `site_id`, such as all-sites
Admin actions, form the global chain), so per-site retention can purge the
oldest rows of one chain without breaking another.

Writes to a chain are serialised with a transaction-scoped advisory lock on
the chain: the writer takes the lock, reads the chain head, takes the next id
and inserts. The lock is held until the caller's transaction ends, so the
next writer sees the committed head and ids grow in chain order. That only
holds under READ COMMITTED (a REPEATABLE READ snapshot can predate the lock
and miss the head), so other isolation levels are refused.

The entry is written in the caller's transaction: an evidence view and its
audit row commit or roll back together (security-privacy 1).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from ag_shared.api import LoginFailurePayload
from sqlalchemy import Connection, cast, func, insert, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from ag_server.audit.chain import AuditRow, compute_hash
from ag_server.audit.errors import AuditError
from ag_server.audit.scrub import scrub_detail
from ag_server.models import AuditLog

Clock = Callable[[], datetime]
Executor = Connection | Session

# First key of the two-key advisory lock ("AGAL"); the second is per chain.
LOCK_NAMESPACE: Final = 0x4147414C
LOGIN_FAILURE_ACTION: Final = "worker.login_failure"

_ACTION = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_MISSING: Final = object()


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AuditEntry:
    """One audit event to record. `detail` is scrubbed before it is stored."""

    action: str
    actor_user_id: uuid.UUID | None = None
    actor_workstation_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    object_type: str | None = None
    object_id: str | None = None
    ip: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditRecord:
    """What was stored: the row id, its time and its place in the chain."""

    id: int
    at: datetime
    prev_hash: bytes | None
    hash: bytes


def chain_lock_key(site_id: uuid.UUID | None) -> int:
    """Stable signed 32-bit advisory-lock key of a site's chain (same in every process)."""
    seed = b"audit-chain:" + (site_id.bytes if site_id is not None else b"global")
    return int.from_bytes(hashlib.sha256(seed).digest()[:4], "big", signed=True)


def _check_entry(entry: AuditEntry) -> str | None:
    if not _ACTION.fullmatch(entry.action):
        raise AuditError("audit action must be a dotted lower-case key, e.g. evidence.view")
    if entry.ip is None:
        return None
    try:
        return str(ipaddress.ip_address(entry.ip))
    except ValueError:
        raise AuditError("audit ip must be a single IPv4 or IPv6 address") from None


def _timestamp(now: Clock) -> datetime:
    at = now()
    if at.tzinfo is None:
        raise AuditError("audit clock must return timezone-aware datetimes")
    return at.astimezone(UTC)


def write_audit(conn: Executor, entry: AuditEntry, *, now: Clock = utc_now) -> AuditRecord:
    """Append `entry` to its site's chain in the caller's transaction.

    Raises `AuditError` for an invalid entry or a transaction that is not
    READ COMMITTED. Database errors propagate: a caller serving evidence must
    not serve it when the audit row cannot be written.
    """
    ip = _check_entry(entry)
    scrubbed = scrub_detail(entry.detail)
    at = _timestamp(now)

    level = conn.execute(select(func.current_setting("transaction_isolation"))).scalar_one()
    if level != "read committed":
        raise AuditError(f"audit writes need READ COMMITTED isolation, not {level}")

    conn.execute(select(func.pg_advisory_xact_lock(LOCK_NAMESPACE, chain_lock_key(entry.site_id))))
    prev_hash: bytes | None = conn.execute(
        select(AuditLog.hash)
        .where(AuditLog.site_id.is_not_distinct_from(entry.site_id))
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    row_id: int = conn.execute(
        select(func.nextval(func.pg_get_serial_sequence("audit_log", "id")))
    ).scalar_one()
    # Hash the detail exactly as JSONB will hand it back (numbers normalised),
    # so verification recomputes the same bytes.
    detail = conn.execute(select(cast(literal(scrubbed, JSONB), JSONB))).scalar_one()

    row = AuditRow(
        id=row_id,
        at=at,
        actor_user_id=entry.actor_user_id,
        actor_workstation_id=entry.actor_workstation_id,
        site_id=entry.site_id,
        action=entry.action,
        object_type=entry.object_type,
        object_id=entry.object_id,
        ip=ip,
        detail=detail,
    )
    digest = compute_hash(prev_hash, row)
    conn.execute(
        insert(AuditLog).values(
            id=row_id,
            at=at,
            actor_user_id=entry.actor_user_id,
            actor_workstation_id=entry.actor_workstation_id,
            site_id=entry.site_id,
            action=entry.action,
            object_type=entry.object_type,
            object_id=entry.object_id,
            ip=ip,
            detail=detail,
            prev_hash=prev_hash,
            hash=digest,
        )
    )
    return AuditRecord(id=row_id, at=at, prev_hash=prev_hash, hash=digest)


def _changed_keys(
    before: Mapping[str, object] | None, after: Mapping[str, object] | None
) -> list[str]:
    old = before or {}
    new = after or {}
    return sorted(
        k for k in old.keys() | new.keys() if old.get(k, _MISSING) != new.get(k, _MISSING)
    )


def log_change(
    conn: Executor,
    *,
    action: str,
    object_type: str,
    object_id: str | None,
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
    actor_user_id: uuid.UUID | None = None,
    actor_workstation_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    ip: str | None = None,
    now: Clock = utc_now,
) -> AuditRecord:
    """Record a create (`before=None`), update or delete (`after=None`).

    Detail is `{before, after, changed}`. `changed` lists the keys whose raw
    values differ, so a PIN or password reset is visible even though both
    values are redacted.
    """
    detail: dict[str, object] = {
        "before": before,
        "after": after,
        "changed": _changed_keys(before, after),
    }
    entry = AuditEntry(
        action=action,
        actor_user_id=actor_user_id,
        actor_workstation_id=actor_workstation_id,
        site_id=site_id,
        object_type=object_type,
        object_id=object_id,
        ip=ip,
        detail=detail,
    )
    return write_audit(conn, entry, now=now)


def log_login_failure(
    conn: Executor,
    payload: LoginFailurePayload,
    *,
    workstation_id: uuid.UUID,
    site_id: uuid.UUID | None,
    ip: str | None = None,
    now: Clock = utc_now,
) -> AuditRecord:
    """One `worker.login_failure` row for a failed station login (ingest of `login_failure`).

    Detail is exactly `{method, known_worker, locked}`; what was typed is
    never stored (an unknown ID is often a PIN typed into the ID field). The
    matched roster user, if any, is the row's object.
    """
    if not payload.known_worker and payload.user_id is not None:
        raise AuditError("login_failure names a user but says the worker is unknown")
    entry = AuditEntry(
        action=LOGIN_FAILURE_ACTION,
        actor_workstation_id=workstation_id,
        site_id=site_id,
        object_type="user",
        object_id=None if payload.user_id is None else str(payload.user_id),
        ip=ip,
        detail={
            "method": payload.method,
            "known_worker": payload.known_worker,
            "locked": payload.locked,
        },
    )
    return write_audit(conn, entry, now=now)
