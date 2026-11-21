"""Operational data (`docs/schema.md`).

`worker_session`, `scan_record`, `attention_event`, `evidence_image` and
`dispute` are created on the workstation and keep the client's UUIDv7 as their
primary key, which is what makes ingest idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ag_shared.api.events import (
    AttentionEventKind,
    DisputeReasonCode,
    ImagesWithheld,
    SessionEndReason,
    SessionMethod,
)
from ag_shared.states import AttentionState, ScanOutcome
from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ag_server.models.base import (
    Base,
    JsonObject,
    client_uuid_pk,
    created_now,
    fk,
    pg_enum,
    server_uuid_pk,
    tstz,
)
from ag_server.models.enums import (
    AlertKind,
    AlertStatus,
    DeliveryChannel,
    DeliveryStatus,
    DisputeStatus,
    LiveViewMode,
    RemovedReason,
    ReviewStatus,
)


class WorkerSession(Base):
    __tablename__ = "worker_session"

    id: Mapped[uuid.UUID] = client_uuid_pk()
    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    user_id: Mapped[uuid.UUID] = fk("user.id")
    method: Mapped[SessionMethod] = mapped_column(
        pg_enum(SessionMethod, "worker_session", "method")
    )
    station_assignment_id: Mapped[uuid.UUID | None] = fk("station_assignment.id", nullable=True)
    started_at: Mapped[datetime] = tstz(nullable=False)
    ended_at: Mapped[datetime | None] = tstz()
    end_reason: Mapped[SessionEndReason | None] = mapped_column(
        pg_enum(SessionEndReason, "worker_session", "end_reason")
    )
    received_at: Mapped[datetime] = created_now()


class ScanRecord(Base):
    __tablename__ = "scan_record"
    __table_args__ = (
        Index("ix_scan_record_workstation_id_scanned_at", "workstation_id", "scanned_at"),
        Index("ix_scan_record_user_id_scanned_at", "user_id", "scanned_at"),
    )

    id: Mapped[uuid.UUID] = client_uuid_pk()
    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    # Null: a scan with no logged-in worker (unattributed), still accepted.
    worker_session_id: Mapped[uuid.UUID | None] = fk("worker_session.id", nullable=True)
    user_id: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    barcode: Mapped[str] = mapped_column(Text)
    scanned_at: Mapped[datetime] = tstz(nullable=False)
    state_at_scan: Mapped[AttentionState] = mapped_column(
        pg_enum(AttentionState, "scan_record", "state_at_scan")
    )
    confidence: Mapped[float] = mapped_column(Float)
    outcome: Mapped[ScanOutcome] = mapped_column(pg_enum(ScanOutcome, "scan_record", "outcome"))
    received_at: Mapped[datetime] = created_now()


class AttentionEvent(Base):
    __tablename__ = "attention_event"
    __table_args__ = (
        Index("ix_attention_event_workstation_id_started_at", "workstation_id", "started_at"),
        Index("ix_attention_event_user_id_started_at", "user_id", "started_at"),
        Index("ix_attention_event_review_status", "review_status"),
    )

    id: Mapped[uuid.UUID] = client_uuid_pk()
    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    user_id: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    worker_session_id: Mapped[uuid.UUID | None] = fk("worker_session.id", nullable=True)
    kind: Mapped[AttentionEventKind] = mapped_column(
        pg_enum(AttentionEventKind, "attention_event", "kind")
    )
    state: Mapped[AttentionState] = mapped_column(
        pg_enum(AttentionState, "attention_event", "state")
    )
    confidence: Mapped[float] = mapped_column(Float)
    # Fusion disagreement between cameras.
    low_confidence: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    started_at: Mapped[datetime] = tstz(nullable=False)
    ended_at: Mapped[datetime | None] = tstz()  # null for scan warnings
    scan_record_id: Mapped[uuid.UUID | None] = fk("scan_record.id", nullable=True, unique=True)
    signals: Mapped[JsonObject] = mapped_column(JSONB)
    camera_states: Mapped[JsonObject] = mapped_column(JSONB)
    engine_version: Mapped[str] = mapped_column(Text)
    config_version: Mapped[int] = mapped_column(Integer)
    images_withheld: Mapped[ImagesWithheld] = mapped_column(
        pg_enum(ImagesWithheld, "attention_event", "images_withheld"), server_default="none"
    )
    review_status: Mapped[ReviewStatus] = mapped_column(
        pg_enum(ReviewStatus, "attention_event", "review_status"), server_default="pending"
    )
    reviewed_by: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    reviewed_at: Mapped[datetime | None] = tstz()
    review_note: Mapped[str | None] = mapped_column(Text)
    escalated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    received_at: Mapped[datetime] = created_now()


class EvidenceImage(Base):
    """One evidence frame. The row stays after removal; file and key material do not."""

    __tablename__ = "evidence_image"

    id: Mapped[uuid.UUID] = client_uuid_pk()
    attention_event_id: Mapped[uuid.UUID] = fk("attention_event.id")
    camera_id: Mapped[uuid.UUID] = fk("camera.id")
    seq: Mapped[int] = mapped_column(Integer)
    offset_ms: Mapped[int] = mapped_column(Integer)  # negative = pre-frame
    captured_at: Mapped[datetime] = tstz(nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))  # hex, as in X-AG-SHA256
    size_bytes: Mapped[int] = mapped_column(Integer)
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    kek_id: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = created_now()
    removed_at: Mapped[datetime | None] = tstz()
    removed_reason: Mapped[RemovedReason | None] = mapped_column(
        pg_enum(RemovedReason, "evidence_image", "removed_reason")
    )


class AttentionBucket(Base):
    """Seconds per state for one workstation, minute and worker.

    schema.md keys this by (`workstation_id`, `bucket_start`, `user_id`), but
    `user_id` is nullable and PostgreSQL primary-key columns cannot be null. The
    key is therefore a unique constraint with NULLS NOT DISTINCT (PostgreSQL
    15+), which gives the same one-row-per-key guarantee and serves
    `ON CONFLICT` upserts; the ORM uses it as the mapper's primary key.
    """

    __tablename__ = "attention_bucket"
    __table_args__ = (
        UniqueConstraint(
            "workstation_id",
            "bucket_start",
            "user_id",
            name="uq_attention_bucket_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    __mapper_args__ = {"primary_key": ["workstation_id", "bucket_start", "user_id"]}  # noqa: RUF012

    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    user_id: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    bucket_start: Mapped[datetime] = tstz(nullable=False)
    seconds: Mapped[JsonObject] = mapped_column(JSONB)
    received_at: Mapped[datetime] = created_now()


class Dispute(Base):
    __tablename__ = "dispute"

    id: Mapped[uuid.UUID] = client_uuid_pk()
    attention_event_id: Mapped[uuid.UUID] = fk("attention_event.id")
    raised_by: Mapped[uuid.UUID] = fk("user.id")
    raised_at: Mapped[datetime] = tstz(nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[DisputeReasonCode | None] = mapped_column(
        pg_enum(DisputeReasonCode, "dispute", "reason")
    )
    status: Mapped[DisputeStatus] = mapped_column(
        pg_enum(DisputeStatus, "dispute", "status"), server_default="open"
    )
    resolved_by: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    resolved_at: Mapped[datetime | None] = tstz()
    resolution_note: Mapped[str | None] = mapped_column(Text)


class Alert(Base):
    __tablename__ = "alert"
    __table_args__ = (
        # One open (or acknowledged) alert per dedupe key.
        Index(
            "uq_alert_dedupe_key_open",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
    )

    id: Mapped[uuid.UUID] = server_uuid_pk()
    site_id: Mapped[uuid.UUID] = fk("site.id")
    workstation_id: Mapped[uuid.UUID | None] = fk("workstation.id", nullable=True)
    user_id: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    kind: Mapped[AlertKind] = mapped_column(pg_enum(AlertKind, "alert", "kind"))
    severity: Mapped[str] = mapped_column(Text)
    status: Mapped[AlertStatus] = mapped_column(
        pg_enum(AlertStatus, "alert", "status"), server_default="open"
    )
    dedupe_key: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = created_now()
    acknowledged_by: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    acknowledged_at: Mapped[datetime | None] = tstz()
    closed_at: Mapped[datetime | None] = tstz()
    payload: Mapped[JsonObject] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class AlertDelivery(Base):
    __tablename__ = "alert_delivery"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    alert_id: Mapped[uuid.UUID] = fk("alert.id", ondelete="CASCADE")
    channel: Mapped[DeliveryChannel] = mapped_column(
        pg_enum(DeliveryChannel, "alert_delivery", "channel")
    )
    recipient: Mapped[str] = mapped_column(Text)
    status: Mapped[DeliveryStatus] = mapped_column(
        pg_enum(DeliveryStatus, "alert_delivery", "status"), server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = tstz()


class DeviceHealth(Base):
    """One sample per minute per workstation."""

    __tablename__ = "device_health"
    __table_args__ = (Index("ix_device_health_workstation_id_at", "workstation_id", "at"),)

    id: Mapped[uuid.UUID] = server_uuid_pk()
    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    at: Mapped[datetime] = tstz(nullable=False)
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    mem_pct: Mapped[float | None] = mapped_column(Float)
    disk_free_mb: Mapped[int | None] = mapped_column(BigInteger)
    cameras: Mapped[JsonObject] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Text, not an enum: schema.md lists no values, and ag_shared.health.EngineStatus
    # is lenient on the wire (newer clients may send newer values).
    engine_status: Mapped[str | None] = mapped_column(Text)
    engine_restarts: Mapped[int | None] = mapped_column(Integer)
    queue_depth: Mapped[int | None] = mapped_column(Integer)
    clock_offset_ms: Mapped[int | None] = mapped_column(BigInteger)
    app_version: Mapped[str | None] = mapped_column(Text)


class LiveViewSession(Base):
    """A live view; every row also produces an audit_log entry."""

    __tablename__ = "live_view_session"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    user_id: Mapped[uuid.UUID] = fk("user.id")
    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    camera_id: Mapped[uuid.UUID] = fk("camera.id")
    mode: Mapped[LiveViewMode] = mapped_column(pg_enum(LiveViewMode, "live_view_session", "mode"))
    started_at: Mapped[datetime] = created_now()
    ended_at: Mapped[datetime | None] = tstz()
    frames_served: Mapped[int] = mapped_column(Integer, server_default=text("0"))
