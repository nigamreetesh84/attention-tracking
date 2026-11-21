"""Alembic migrations on a real PostgreSQL: round trip and model drift."""

from __future__ import annotations

from collections.abc import Callable

from ag_server.migrations.runner import current_revision, downgrade, head_revision, upgrade
from ag_server.models import Base
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text


def _tables(dsn: str) -> set[str]:
    engine = create_engine(dsn)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _enum_types(dsn: str) -> set[str]:
    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "select t.typname from pg_type t join pg_namespace n on n.oid = t.typnamespace"
                    " where t.typtype = 'e' and n.nspname = 'public'"
                )
            )
            return {str(r[0]) for r in rows}
    finally:
        engine.dispose()


def _model_enum_names() -> set[str]:
    from sqlalchemy import Enum as SaEnum

    return {
        str(c.type.name)
        for t in Base.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, SaEnum)
    }


def test_upgrade_downgrade_upgrade_round_trip(pg_database_factory: Callable[[], str]) -> None:
    dsn = pg_database_factory()
    model_tables = set(Base.metadata.tables)

    upgrade(dsn, "head")
    assert current_revision(dsn) == head_revision()
    assert _tables(dsn) == model_tables | {"alembic_version"}
    assert _enum_types(dsn) == _model_enum_names()

    downgrade(dsn, "base")
    assert current_revision(dsn) is None
    assert _tables(dsn) <= {"alembic_version"}
    assert _enum_types(dsn) == set(), "downgrade must drop every enum type"

    upgrade(dsn, "head")
    assert _tables(dsn) == model_tables | {"alembic_version"}
    assert _enum_types(dsn) == _model_enum_names()


def test_models_and_migrations_do_not_drift(migrated_dsn: str) -> None:
    engine = create_engine(migrated_dsn)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_migrations_are_a_single_linear_history() -> None:
    from ag_server.migrations.runner import alembic_config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config(None))
    assert len(script.get_heads()) == 1
    assert len(script.get_bases()) == 1


def test_offline_sql_generation_works() -> None:
    # `alembic upgrade head --sql` must work for DBAs who review DDL first.
    from ag_server.migrations.runner import offline_sql

    sql = offline_sql("head")
    assert "CREATE TABLE camera" in sql
    assert "CREATE TYPE camera_role AS ENUM" in sql
