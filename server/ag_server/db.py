"""Database engine helpers."""

from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from ag_server.settings import Settings

log = logging.getLogger(__name__)


def make_engine(settings: Settings) -> Engine:
    """Engine for the configured database. Connects lazily, so startup never needs the DB."""
    return create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def database_is_up(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        log.warning("database check failed", exc_info=False)
        return False
    return True
