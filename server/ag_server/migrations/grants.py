"""SQL for the append-only audit trigger and the application role's grants (task 3.3).

Two database roles (docs/schema.md, audit_log): migrations run as the schema
owner; the app and worker connect as a separate login role that gets DML on
ordinary tables but only INSERT and SELECT on `audit_log`, and SELECT on
`alembic_version`. Grants are applied only if the role exists, so a
single-role development database still migrates. Default privileges give the
role DML on tables that later migrations create; a later append-only table
must revoke explicitly, as this module does for `audit_log`.

Role names cannot be bound parameters in GRANT, so they are validated as
plain lower-case identifiers (`ag_server.settings.app_role_name`) before they
are put into SQL (hence the S608 suppressions).
"""

from __future__ import annotations

from typing import Final

from ag_server.settings import app_role_name

# The DELETE escape hatch for the retention purge (security-privacy 7): only a
# role that already holds DELETE (never the app role) and sets this in its
# transaction can remove rows. UPDATE and TRUNCATE are always rejected.
PURGE_SETTING: Final = "ag.audit_purge"

TRIGGER_FUNCTION: Final = f"""
CREATE FUNCTION audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('{PURGE_SETTING}', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only: % rejected', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$fn$
"""

# ENABLE ALWAYS: the triggers also fire with session_replication_role = replica.
TRIGGERS: Final[tuple[str, ...]] = (
    "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log"
    " FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()",
    "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log"
    " FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()",
    "ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_append_only",
    "ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_no_truncate",
)

DROP_TRIGGERS: Final[tuple[str, ...]] = (
    "DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log",
    "DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log",
    "DROP FUNCTION IF EXISTS audit_log_append_only()",
)


_DEFAULTS: Final = "ALTER DEFAULT PRIVILEGES IN SCHEMA public"


def _if_role_exists(role: str, body: list[str]) -> str:
    statements = "\n".join(f"        {line};" for line in body)
    return f"""
DO $do$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
{statements}
    ELSE
        RAISE NOTICE 'role {role} does not exist: no application grants applied';
    END IF;
END
$do$
"""


def grant_app_role(role: str) -> str:
    """DO block granting the app role its privileges (no-op if the role is missing)."""
    role = app_role_name({"AG_DB_APP_ROLE": role})
    return _if_role_exists(
        role,
        [
            f"GRANT USAGE ON SCHEMA public TO {role}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
            f"REVOKE ALL ON audit_log FROM {role}",
            f"GRANT SELECT, INSERT ON audit_log TO {role}",
            f"REVOKE ALL ON alembic_version FROM {role}",
            f"GRANT SELECT ON alembic_version TO {role}",
            f"{_DEFAULTS} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
            f"{_DEFAULTS} GRANT USAGE, SELECT ON SEQUENCES TO {role}",
        ],
    )


def revoke_app_role(role: str) -> str:
    """DO block undoing `grant_app_role` (no-op if the role is missing)."""
    role = app_role_name({"AG_DB_APP_ROLE": role})
    return _if_role_exists(
        role,
        [
            f"{_DEFAULTS} REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {role}",
            f"{_DEFAULTS} REVOKE USAGE, SELECT ON SEQUENCES FROM {role}",  # noqa: S608
            f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}",
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}",
            f"REVOKE USAGE ON SCHEMA public FROM {role}",
        ],
    )
