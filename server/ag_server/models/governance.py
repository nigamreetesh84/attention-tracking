"""Governance tables (`docs/schema.md`).

`audit_log` and `purge_log` are append-only, use `bigserial` ids for strict
order and reference other rows by type and id only (no foreign keys), so they
outlive what they describe. The audit trigger and grants come with task 3.3.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Integer,
    LargeBinary,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ag_server.models.base import (
    Base,
    JsonList,
    JsonObject,
    created_now,
    fk,
    pg_enum,
    server_uuid_pk,
    tstz,
)
from ag_server.models.enums import DeletionRequestStatus, PurgeKind, ReleaseChannel, ReportFormat


class EvidenceDeletionRequest(Base):
    """Dual-approval deletion. Both users must be Admins at decision time (service check)."""

    __tablename__ = "evidence_deletion_request"
    __table_args__ = (
        # Rule 9: the decider is a second, different Admin. NULL (undecided) passes.
        CheckConstraint("decided_by <> requested_by", name="second_admin"),
    )

    id: Mapped[uuid.UUID] = server_uuid_pk()
    site_id: Mapped[uuid.UUID] = fk("site.id")
    requested_by: Mapped[uuid.UUID] = fk("user.id")
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[DeletionRequestStatus] = mapped_column(
        pg_enum(DeletionRequestStatus, "evidence_deletion_request", "status"),
        server_default="pending",
    )
    decided_by: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    decided_at: Mapped[datetime | None] = tstz()
    decision_note: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = tstz()


class EvidenceDeletionItem(Base):
    __tablename__ = "evidence_deletion_item"

    request_id: Mapped[uuid.UUID] = fk(
        "evidence_deletion_request.id", ondelete="CASCADE", primary_key=True
    )
    attention_event_id: Mapped[uuid.UUID] = fk("attention_event.id", primary_key=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = created_now()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_workstation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(Text)  # dotted key, e.g. evidence.view
    object_type: Mapped[str | None] = mapped_column(Text)
    # Text: objects are keyed by UUID, bigint or composite keys.
    object_id: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    detail: Mapped[JsonObject] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)  # null for the first row
    hash: Mapped[bytes] = mapped_column(LargeBinary)


class PurgeLog(Base):
    __tablename__ = "purge_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = created_now()
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[PurgeKind] = mapped_column(pg_enum(PurgeKind, "purge_log", "kind"))
    cutoff: Mapped[datetime] = tstz(nullable=False)
    items_removed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    bytes_freed: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text)
    detail: Mapped[JsonObject] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class IngestBatch(Base):
    """A processed batch, kept 30 days so a retry gets the original response."""

    __tablename__ = "ingest_batch"

    workstation_id: Mapped[uuid.UUID] = fk("workstation.id", primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    received_at: Mapped[datetime] = created_now()
    item_count: Mapped[int] = mapped_column(Integer)
    result: Mapped[JsonObject] = mapped_column(JSONB)


class ReportSchedule(Base):
    __tablename__ = "report_schedule"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    site_id: Mapped[uuid.UUID] = fk("site.id")
    report_kind: Mapped[str] = mapped_column(Text)
    params: Mapped[JsonObject] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    format: Mapped[ReportFormat] = mapped_column(pg_enum(ReportFormat, "report_schedule", "format"))
    cron: Mapped[str] = mapped_column(Text)
    recipients: Mapped[JsonList] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_by: Mapped[uuid.UUID] = fk("user.id")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class ReportRun(Base):
    __tablename__ = "report_run"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    report_schedule_id: Mapped[uuid.UUID | None] = fk("report_schedule.id", nullable=True)
    run_at: Mapped[datetime] = created_now()
    status: Mapped[str] = mapped_column(Text)
    file_key: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class AppRelease(Base):
    """A signed workstation release (auto-update, phase 7)."""

    __tablename__ = "app_release"

    version: Mapped[str] = mapped_column(Text, primary_key=True)
    channel: Mapped[ReleaseChannel] = mapped_column(
        pg_enum(ReleaseChannel, "app_release", "channel")
    )
    sha256: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(Text)
    file_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_now()
    notes: Mapped[str | None] = mapped_column(Text)
