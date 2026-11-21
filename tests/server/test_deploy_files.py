"""Deployment files parse and keep the safety properties we rely on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def compose() -> dict[str, Any]:
    data = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    assert isinstance(data, dict)
    return data


def test_compose_has_the_five_services() -> None:
    assert set(compose()["services"]) == {"postgres", "migrate", "app", "worker", "proxy"}


def test_only_the_proxy_publishes_ports() -> None:
    services = compose()["services"]
    for name, svc in services.items():
        assert bool(svc.get("ports")) == (name == "proxy"), name


def test_app_and_worker_run_in_production_mode_with_secret_files() -> None:
    for name in ("app", "worker"):
        env = compose()["services"][name]["environment"]
        assert env["AG_ENV"] == "production"
        assert env["AG_KEK_FILE"].startswith("/run/secrets/")
        assert env["AG_JWT_SIGNING_KEY_FILE"].startswith("/run/secrets/")
        assert env["AG_DATABASE_URL_FILE"].startswith("/run/secrets/")


def test_secrets_are_file_based_and_not_in_git() -> None:
    secrets = compose()["secrets"]
    assert {"ag_kek", "ag_jwt_signing_key", "ag_database_url", "ag_db_password"} <= set(secrets)
    for spec in secrets.values():
        assert spec["file"].startswith("./secrets/")
    assert "deploy/secrets/" in (DEPLOY.parent / ".gitignore").read_text()


def test_app_waits_for_healthy_database_and_has_healthcheck() -> None:
    app = compose()["services"]["app"]
    assert app["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "healthcheck" in app
    assert "healthcheck" in compose()["services"]["postgres"]


def test_proxy_config_terminates_tls_and_sets_hsts() -> None:
    text = (DEPLOY / "Caddyfile").read_text()
    assert "reverse_proxy app:8000" in text
    assert "Strict-Transport-Security" in text


def test_dockerfile_runs_unprivileged() -> None:
    text = (DEPLOY / "Dockerfile").read_text()
    assert "USER " in text
    assert "USER root" not in text


def test_migrate_service_runs_alembic_once_before_app_and_worker() -> None:
    services = compose()["services"]
    migrate = services["migrate"]
    assert migrate["image"] == services["app"]["image"]
    assert migrate["restart"] == "no"
    assert migrate["command"] == ["alembic", "-c", "server/alembic.ini", "upgrade", "head"]
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert migrate["environment"]["AG_DATABASE_URL_FILE"].startswith("/run/secrets/")
    assert "ag_database_url" in migrate["secrets"]
    for name in ("app", "worker"):
        dep = services[name]["depends_on"]["migrate"]
        assert dep["condition"] == "service_completed_successfully", name


def test_image_ships_the_alembic_config_and_migrations() -> None:
    text = (DEPLOY / "Dockerfile").read_text()
    assert "COPY server ./server" in text
    root = DEPLOY.parent
    assert (root / "server" / "alembic.ini").is_file()
    assert (root / "server" / "ag_server" / "migrations" / "env.py").is_file()


def test_readme_documents_the_migration_step() -> None:
    text = (DEPLOY / "README.md").read_text()
    assert "alembic" in text and "upgrade head" in text and "migrate" in text
