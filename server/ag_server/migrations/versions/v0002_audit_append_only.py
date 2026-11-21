"""Audit log: append-only trigger, chain index and application-role grants (task 3.3).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from ag_server.migrations.grants import (
    DROP_TRIGGERS,
    TRIGGER_FUNCTION,
    TRIGGERS,
    grant_app_role,
    revoke_app_role,
)
from alembic import context, op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _app_role() -> str:
    role = context.config.attributes.get("app_role")
    if not isinstance(role, str):  # env.py always sets it
        raise RuntimeError("app_role missing from the Alembic config attributes")
    return role


def upgrade() -> None:
    # The writer finds a chain's head by (site_id, id DESC).
    op.create_index("ix_audit_log_site_id_id", "audit_log", ["site_id", "id"])
    op.execute(TRIGGER_FUNCTION)
    for statement in TRIGGERS:
        op.execute(statement)
    op.execute(grant_app_role(_app_role()))


def downgrade() -> None:
    op.execute(revoke_app_role(_app_role()))
    for statement in DROP_TRIGGERS:
        op.execute(statement)
    op.drop_index("ix_audit_log_site_id_id", table_name="audit_log")
