"""The throwaway PostgreSQL database fixture works against a real server."""

from __future__ import annotations

from sqlalchemy import Engine, text


def test_fixture_gives_postgres_throwaway_database(pg_engine: Engine) -> None:
    with pg_engine.connect() as conn:
        assert "PostgreSQL" in str(conn.execute(text("select version()")).scalar())
        name = conn.execute(text("select current_database()")).scalar()
    assert str(name).startswith("ag_test_")


def test_fixture_database_is_empty_and_writable(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        conn.execute(text("create table t (id int primary key)"))
        conn.execute(text("insert into t values (1)"))
    with pg_engine.connect() as conn:
        assert conn.execute(text("select count(*) from t")).scalar() == 1
