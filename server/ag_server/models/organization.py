"""Organization tables: site, line, workstation, camera (`docs/schema.md`)."""

from __future__ import annotations

import uuid
from datetime import datetime

from ag_shared.config import CameraKind, CameraRole
from ag_shared.states import AttentionState
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
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
    created_now,
    fk,
    pg_enum,
    server_uuid_pk,
    tstz,
)
from ag_server.models.enums import WorkstationStatus


class Site(Base):
    __tablename__ = "site"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64))
    # Cycle with threshold_profile.site_id: created after both tables.
    default_threshold_profile_id: Mapped[uuid.UUID | None] = fk(
        "threshold_profile.id", nullable=True, use_alter=True
    )
    settings: Mapped[JsonObject] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_now()


class Line(Base):
    __tablename__ = "line"
    __table_args__ = (UniqueConstraint("site_id", "name"),)

    id: Mapped[uuid.UUID] = server_uuid_pk()
    site_id: Mapped[uuid.UUID] = fk("site.id")
    name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Workstation(Base):
    __tablename__ = "workstation"
    __table_args__ = (CheckConstraint("camera_count BETWEEN 1 AND 4", name="camera_count"),)

    id: Mapped[uuid.UUID] = server_uuid_pk()
    line_id: Mapped[uuid.UUID] = fk("line.id")
    name: Mapped[str] = mapped_column(Text)
    ppe_profile_id: Mapped[uuid.UUID] = fk("ppe_profile.id")
    threshold_profile_id: Mapped[uuid.UUID | None] = fk("threshold_profile.id", nullable=True)
    camera_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("2"))
    device_secret_hash: Mapped[str | None] = mapped_column(Text)
    enrollment_token_hash: Mapped[str | None] = mapped_column(Text)
    enrollment_expires_at: Mapped[datetime | None] = tstz()
    status: Mapped[WorkstationStatus] = mapped_column(
        pg_enum(WorkstationStatus, "workstation", "status"), server_default="pending"
    )
    enrolled_at: Mapped[datetime | None] = tstz()
    last_seen_at: Mapped[datetime | None] = tstz()
    app_version: Mapped[str | None] = mapped_column(Text)
    target_app_version: Mapped[str | None] = mapped_column(Text)
    applied_config_version: Mapped[int | None] = mapped_column(Integer)
    # Denormalized live status written by the heartbeat.
    current_state: Mapped[AttentionState | None] = mapped_column(
        pg_enum(AttentionState, "workstation", "current_state")
    )
    current_user_id: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    current_state_at: Mapped[datetime | None] = tstz()


class Camera(Base):
    __tablename__ = "camera"
    __table_args__ = (
        UniqueConstraint("workstation_id", "slot"),
        CheckConstraint("slot BETWEEN 1 AND 4", name="slot"),
        # At most one primary per workstation; the console creates the
        # primary with the workstation, so "exactly one" holds in practice.
        Index(
            "uq_camera_one_primary_per_workstation",
            "workstation_id",
            unique=True,
            postgresql_where=text("role = 'primary'"),
        ),
    )

    id: Mapped[uuid.UUID] = server_uuid_pk()
    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    slot: Mapped[int] = mapped_column(SmallInteger)
    role: Mapped[CameraRole] = mapped_column(pg_enum(CameraRole, "camera", "role"))
    kind: Mapped[CameraKind] = mapped_column(pg_enum(CameraKind, "camera", "kind"))
    # Ciphertext: the URI may hold camera credentials.
    uri_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    roi: Mapped[JsonObject | None] = mapped_column(JSONB)
    calibration: Mapped[JsonObject | None] = mapped_column(JSONB)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
