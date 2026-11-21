"""Configuration tables (`docs/schema.md`)."""

from __future__ import annotations

import uuid
from datetime import datetime, time

from ag_shared.ppe import PpeProfileKind
from sqlalchemy import Boolean, Integer, SmallInteger, Text, Time, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
from ag_server.models.enums import AssignmentStatus


class PpeProfile(Base):
    __tablename__ = "ppe_profile"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    # Null for the built-in profiles (seeded from ag_shared.ppe).
    site_id: Mapped[uuid.UUID | None] = fk("site.id", nullable=True)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[PpeProfileKind] = mapped_column(pg_enum(PpeProfileKind, "ppe_profile", "kind"))
    # {signal name: {"enabled": bool, "weight": float}} (ag_shared.ppe.SignalSetting).
    signals: Mapped[JsonObject] = mapped_column(JSONB)
    threshold_profile_id: Mapped[uuid.UUID | None] = fk("threshold_profile.id", nullable=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class ThresholdProfile(Base):
    __tablename__ = "threshold_profile"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    site_id: Mapped[uuid.UUID] = fk("site.id")
    name: Mapped[str] = mapped_column(Text)
    # Partial ag_shared.thresholds.Thresholds: only the overridden keys.
    values: Mapped[JsonObject] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Shift(Base):
    __tablename__ = "shift"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    site_id: Mapped[uuid.UUID] = fk("site.id")
    # Null means the whole site.
    line_id: Mapped[uuid.UUID | None] = fk("line.id", nullable=True)
    name: Mapped[str] = mapped_column(Text)
    # Site-local wall times; may cross midnight.
    start_time: Mapped[time] = mapped_column(Time(timezone=False))
    end_time: Mapped[time] = mapped_column(Time(timezone=False))
    # 0 = Monday ... 6 = Sunday, the day the shift starts (ag_shared.config.Shift).
    days_of_week: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))
    threshold_profile_id: Mapped[uuid.UUID | None] = fk("threshold_profile.id", nullable=True)


class ShiftBreak(Base):
    __tablename__ = "shift_break"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    shift_id: Mapped[uuid.UUID] = fk("shift.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(Text)
    start_time: Mapped[time] = mapped_column(Time(timezone=False))
    end_time: Mapped[time] = mapped_column(Time(timezone=False))


class WorkstationConfig(Base):
    """Every config bundle version sent to a device; old versions stay for rollback."""

    __tablename__ = "workstation_config"

    workstation_id: Mapped[uuid.UUID] = fk("workstation.id", primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[JsonObject] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
    created_at: Mapped[datetime] = created_now()
    note: Mapped[str | None] = mapped_column(Text)


class StationAssignment(Base):
    """Supervisor fallback for worker login."""

    __tablename__ = "station_assignment"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    workstation_id: Mapped[uuid.UUID] = fk("workstation.id")
    user_id: Mapped[uuid.UUID] = fk("user.id")
    assigned_by: Mapped[uuid.UUID] = fk("user.id")
    assigned_at: Mapped[datetime] = created_now()
    valid_until: Mapped[datetime] = tstz(nullable=False)
    status: Mapped[AssignmentStatus] = mapped_column(
        pg_enum(AssignmentStatus, "station_assignment", "status"), server_default="pending"
    )
