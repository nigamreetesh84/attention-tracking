"""Health endpoints and the app factory."""

from __future__ import annotations

import pytest
from ag_server.app import create_app
from ag_server.settings import SettingsError, load_settings
from fastapi.testclient import TestClient
from sqlalchemy import Engine

UNREACHABLE = "postgresql+psycopg://nobody@127.0.0.1:1/none?connect_timeout=1"


def test_healthz_needs_no_database() -> None:
    app = create_app(load_settings({"AG_ENV": "test", "AG_DATABASE_URL": UNREACHABLE}))
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_reports_unavailable_without_leaking_details() -> None:
    app = create_app(load_settings({"AG_ENV": "test", "AG_DATABASE_URL": UNREACHABLE}))
    with TestClient(app) as client:
        r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "database": "down"}
    assert "nobody" not in r.text
    assert "127.0.0.1" not in r.text


def test_readyz_ok_with_real_postgres(pg_dsn: str, pg_engine: Engine) -> None:
    app = create_app(load_settings({"AG_ENV": "test", "AG_DATABASE_URL": pg_dsn}))
    with TestClient(app) as client:
        r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "up"}


def test_health_routes_are_marked_public() -> None:
    # 3.5 replaced the placeholder `x-ag-public` extension with the `PUBLIC`
    # dependency marker; the OpenAPI document shows it as `x-ag-access`.
    from ag_server.rbac import Access, AccessKind, route_access

    app = create_app(load_settings({"AG_ENV": "test", "AG_DATABASE_URL": UNREACHABLE}))
    schema = app.openapi()
    declared = {r.path: r.declarations for r in route_access(app)}
    for path in ("/healthz", "/readyz"):
        assert schema["paths"][path]["get"]["x-ag-access"] == {"kind": "public"}
        assert declared[path] == (Access(AccessKind.PUBLIC),)


def test_production_start_refused_with_default_secrets() -> None:
    with pytest.raises(SettingsError):
        create_app(load_settings({"AG_ENV": "production"}))


def test_worker_refuses_unsafe_production_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from ag_server import worker

    monkeypatch.setenv("AG_ENV", "production")
    for name in ("AG_KEK", "AG_JWT_SIGNING_KEY", "AG_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SettingsError):
        worker.run()


def test_worker_runs_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from ag_server import worker

    monkeypatch.setenv("AG_ENV", "test")
    stop = threading.Event()
    stop.set()
    worker.run(stop)
