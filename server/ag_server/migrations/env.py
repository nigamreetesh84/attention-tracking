"""Alembic environment.

Database URL, in order: a connection passed in `config.attributes["connection"]`
(tests and the runner), `config.attributes["url"]`, then the migration
settings: `AG_MIGRATE_DATABASE_URL` / `_FILE`, the schema-owner DSN (task 3.3;
outside production it falls back to `AG_DATABASE_URL`). The URL is never logged.

The application role that migrations grant privileges to is
`config.attributes["app_role"]`, else `AG_DB_APP_ROLE`, else
`attentionguard_app` (see `ag_server.migrations.grants`).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from ag_server.models import Base
from ag_server.settings import app_role_name, load_migration_settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("url")
    if isinstance(url, str):
        return url
    return load_migration_settings().database_url.get_secret_value()


if not isinstance(config.attributes.get("app_role"), str):
    config.attributes["app_role"] = app_role_name()


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL to stdout (`alembic upgrade head --sql`) for review by a DBA."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if isinstance(connection, Connection):
        _configure(connection)
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            _configure(conn)
            conn.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
