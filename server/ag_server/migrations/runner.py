"""Run migrations from Python (tests, tooling) without an alembic.ini on disk.

`script_location` points at this package, so the runner works from the source
tree and from an installed package alike. The CLI path for operators is
`alembic -c server/alembic.ini upgrade head`.
"""

from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

MIGRATIONS_DIR = Path(__file__).resolve().parent


def alembic_config(url: str | None, *, stdout: io.StringIO | None = None) -> Config:
    """A Config for this package's migrations; `url` overrides the settings."""
    cfg = Config(stdout=stdout) if stdout is not None else Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["configure_logging"] = False
    if url is not None:
        cfg.attributes["url"] = url
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade the database at `url` to `revision`."""
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    """Downgrade the database at `url` to `revision` (`base` removes everything)."""
    command.downgrade(alembic_config(url), revision)


def head_revision() -> str | None:
    """The single head revision of the migration scripts."""
    return ScriptDirectory.from_config(alembic_config(None)).get_current_head()


def current_revision(url: str) -> str | None:
    """The revision the database at `url` is at; None for an unmigrated database."""
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def offline_sql(revision: str = "head") -> str:
    """The SQL an upgrade from base to `revision` would run (no database needed)."""
    buffer = io.StringIO()
    cfg = alembic_config("postgresql+psycopg://offline@localhost/offline", stdout=buffer)
    cfg.output_buffer = buffer
    command.upgrade(cfg, revision, sql=True)
    return buffer.getvalue()
