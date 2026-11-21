"""Verify every audit chain: links, hashes and purge anchors.

A chain's first row may have a `prev_hash` only if a retention purge of that
site's chain recorded it as `purge_log.detail["chain_anchor"]` (hex of the
last purged row's hash, `kind = 'audit'`, same `site_id`).

Limits: rows removed from the newest end of a chain, or a chain rewritten
end to end by a database superuser, cannot be detected from the table alone.
Record the head hashes this prints somewhere outside the database to cover that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import Connection, select

from ag_server.audit.chain import AuditRow, compute_hash, normalize_ip
from ag_server.models import AuditLog, PurgeLog
from ag_server.models.enums import PurgeKind

ANCHOR_KEY: Final = "chain_anchor"


@dataclass(frozen=True)
class ChainHead:
    """The newest row of one chain."""

    last_id: int
    hash: bytes
    rows: int


@dataclass(frozen=True)
class Problem:
    """One verification failure. Reasons never quote stored detail."""

    row_id: int
    site_id: uuid.UUID | None
    reason: str


@dataclass
class VerifyResult:
    rows: int = 0
    heads: dict[uuid.UUID | None, ChainHead] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _purge_anchors(conn: Connection) -> dict[uuid.UUID | None, set[bytes]]:
    anchors: dict[uuid.UUID | None, set[bytes]] = {}
    rows = conn.execute(
        select(PurgeLog.site_id, PurgeLog.detail).where(PurgeLog.kind == PurgeKind.AUDIT)
    )
    for site_id, detail in rows:
        value = detail.get(ANCHOR_KEY) if isinstance(detail, dict) else None
        if not isinstance(value, str):
            continue
        try:
            anchors.setdefault(site_id, set()).add(bytes.fromhex(value))
        except ValueError:
            continue
    return anchors


def verify_chain(conn: Connection, *, batch_size: int = 1000) -> VerifyResult:
    """Check every row of every chain; streams rows in id order."""
    anchors = _purge_anchors(conn)
    result = VerifyResult()
    heads: dict[uuid.UUID | None, ChainHead] = {}
    stmt = select(AuditLog.__table__).order_by(AuditLog.id).execution_options(yield_per=batch_size)
    for rec in conn.execute(stmt):
        site = rec.site_id
        head = heads.get(site)
        if head is None:
            if rec.prev_hash is not None and rec.prev_hash not in anchors.get(site, set()):
                result.problems.append(
                    Problem(rec.id, site, "chain start: prev_hash matches no recorded purge anchor")
                )
        elif rec.prev_hash != head.hash:
            result.problems.append(
                Problem(rec.id, site, "broken link: prev_hash is not the previous row's hash")
            )
        try:
            row = AuditRow(
                id=rec.id,
                at=rec.at,
                actor_user_id=rec.actor_user_id,
                actor_workstation_id=rec.actor_workstation_id,
                site_id=site,
                action=rec.action,
                object_type=rec.object_type,
                object_id=rec.object_id,
                ip=normalize_ip(rec.ip),
                detail=rec.detail,
            )
            expected = compute_hash(rec.prev_hash, row)
        except (TypeError, ValueError):
            expected = b""
        if expected != rec.hash:
            result.problems.append(Problem(rec.id, site, "hash mismatch: row content was changed"))
        # Continue from the stored hash so one tampered row is reported once.
        heads[site] = ChainHead(last_id=rec.id, hash=rec.hash, rows=(head.rows + 1 if head else 1))
        result.rows += 1
    result.heads = heads
    return result
