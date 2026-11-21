"""People and access tables (`docs/schema.md`)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Integer, LargeBinary, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ag_server.models.base import (
    Base,
    JsonList,
    created_now,
    fk,
    pg_enum,
    server_uuid_pk,
    tstz,
)
from ag_server.models.enums import ConsentMethod, ConsentStatus


class Role(Base):
    __tablename__ = "role"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    permissions: Mapped[JsonList] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class User(Base):
    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("site_id", "employee_id"),)

    id: Mapped[uuid.UUID] = server_uuid_pk()
    # Null means all sites (Admin only; enforced in the service).
    site_id: Mapped[uuid.UUID | None] = fk("site.id", nullable=True)
    role_id: Mapped[uuid.UUID] = fk("role.id")
    # Console-only accounts may have no employee ID.
    employee_id: Mapped[str | None] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    pin_hash: Mapped[str | None] = mapped_column(Text)
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    ppe_profile_override_id: Mapped[uuid.UUID | None] = fk("ppe_profile.id", nullable=True)
    preferred_language: Mapped[str | None] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = tstz()
    password_changed_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_now()


class SupervisorLine(Base):
    """A supervisor's assigned lines."""

    __tablename__ = "supervisor_line"

    user_id: Mapped[uuid.UUID] = fk("user.id", ondelete="CASCADE", primary_key=True)
    line_id: Mapped[uuid.UUID] = fk("line.id", ondelete="CASCADE", primary_key=True)


class WebSession(Base):
    __tablename__ = "web_session"

    # SHA-256 of the random 256-bit session id; the raw id is only in the cookie.
    id: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[uuid.UUID] = fk("user.id", ondelete="CASCADE")
    created_at: Mapped[datetime] = created_now()
    last_seen_at: Mapped[datetime] = created_now()
    expires_at: Mapped[datetime] = tstz(nullable=False)
    mfa_passed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = tstz()


class ConsentPolicy(Base):
    __tablename__ = "consent_policy"
    __table_args__ = (UniqueConstraint("site_id", "version", "language"),)

    id: Mapped[uuid.UUID] = server_uuid_pk()
    site_id: Mapped[uuid.UUID] = fk("site.id")
    version: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[datetime] = tstz(nullable=False)
    created_by: Mapped[uuid.UUID] = fk("user.id")


class ConsentRecord(Base):
    __tablename__ = "consent_record"

    id: Mapped[uuid.UUID] = server_uuid_pk()
    user_id: Mapped[uuid.UUID] = fk("user.id")
    consent_policy_id: Mapped[uuid.UUID] = fk("consent_policy.id")
    status: Mapped[ConsentStatus] = mapped_column(
        pg_enum(ConsentStatus, "consent_record", "status")
    )
    method: Mapped[ConsentMethod] = mapped_column(
        pg_enum(ConsentMethod, "consent_record", "method")
    )
    # Storage key of the encrypted signature image or scanned form.
    artifact_key: Mapped[str | None] = mapped_column(Text)
    # A withdrawal is its own record (status withdrawn), so granted_at may be empty.
    granted_at: Mapped[datetime | None] = tstz()
    withdrawn_at: Mapped[datetime | None] = tstz()
    recorded_by: Mapped[uuid.UUID | None] = fk("user.id", nullable=True)
