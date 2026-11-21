"""PostgreSQL test fixtures for the server tests.

Server tests use a real PostgreSQL (never SQLite). ``AG_TEST_PG_DSN`` points at
a server where the role may create databases; each test session creates one
throwaway database and drops it at the end. Without the variable, tests that
use these fixtures are skipped with a clear reason.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

PG_DSN_ENV = "AG_TEST_PG_DSN"
SKIP_REASON = f"{PG_DSN_ENV} is not set; PostgreSQL tests need a real server (see CLAUDE.md)"


@pytest.fixture(scope="session")
def pg_admin_dsn() -> str:
    dsn = os.environ.get(PG_DSN_ENV)
    if not dsn:
        pytest.skip(SKIP_REASON)
    return dsn


@pytest.fixture(scope="session")
def pg_dsn(pg_admin_dsn: str) -> Iterator[str]:
    """DSN of a throwaway database, created for this session and dropped after."""
    name = f"ag_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield make_url(pg_admin_dsn).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def pg_engine(pg_dsn: str) -> Iterator[Engine]:
    engine = create_engine(pg_dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def pg_database_factory(pg_admin_dsn: str) -> Iterator[Callable[[], str]]:
    """Factory for extra throwaway databases (migration tests need empty ones).

    Every database it creates is dropped at the end of the session.
    """
    admin = create_engine(pg_admin_dsn, isolation_level="AUTOCOMMIT")
    created: list[str] = []

    def create() -> str:
        name = f"ag_test_{uuid.uuid4().hex[:12]}"
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return make_url(pg_admin_dsn).set(database=name).render_as_string(hide_password=False)

    try:
        yield create
    finally:
        with admin.connect() as conn:
            for name in created:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def migrated_dsn(pg_database_factory: Callable[[], str]) -> str:
    """A throwaway database upgraded to the head migration (shared by read-mostly tests)."""
    from ag_server.migrations.runner import upgrade

    dsn = pg_database_factory()
    upgrade(dsn, "head")
    return dsn


@pytest.fixture
def migrated_engine(migrated_dsn: str) -> Iterator[Engine]:
    engine = create_engine(migrated_dsn)
    try:
        yield engine
    finally:
        engine.dispose()
