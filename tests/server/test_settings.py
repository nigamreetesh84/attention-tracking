"""Settings loading, validation and the production secrets guard."""

from __future__ import annotations

from pathlib import Path

import pytest
from ag_server.settings import PLACEHOLDER_MARKERS, Settings, SettingsError, load_settings

GOOD_KEK = "k" * 8 + "Zx9" * 12
GOOD_JWT = "j" * 8 + "Qw3" * 12
GOOD_DB = "postgresql+psycopg://ag:" + "Rt7" * 10 + "@db:5432/ag"


def prod_env(**over: str) -> dict[str, str]:
    env = {
        "AG_ENV": "production",
        "AG_DATABASE_URL": GOOD_DB,
        "AG_KEK": GOOD_KEK,
        "AG_JWT_SIGNING_KEY": GOOD_JWT,
    }
    env.update(over)
    return env


def test_defaults_are_development() -> None:
    s = load_settings({})
    assert isinstance(s, Settings)
    assert s.env == "development"
    assert s.blocking_mode is False
    assert s.database_url.get_secret_value().startswith("postgresql")


def test_env_overrides() -> None:
    s = load_settings({"AG_ENV": "test", "AG_DATABASE_URL": "postgresql+psycopg://x@h/d"})
    assert s.env == "test"
    assert s.database_url.get_secret_value() == "postgresql+psycopg://x@h/d"


def test_unknown_env_value_rejected() -> None:
    with pytest.raises(SettingsError, match="AG_ENV"):
        load_settings({"AG_ENV": "staging"})


def test_blocking_mode_refused_in_release_1() -> None:
    with pytest.raises(SettingsError, match="blocking"):
        load_settings({"AG_BLOCKING_MODE": "true"})


def test_non_postgres_database_url_rejected() -> None:
    with pytest.raises(SettingsError, match="PostgreSQL"):
        load_settings({"AG_DATABASE_URL": "sqlite:///x.db"})


def test_secret_read_from_file(tmp_path: Path) -> None:
    f = tmp_path / "kek"
    f.write_text(GOOD_KEK + "\n")
    s = load_settings(prod_env(AG_KEK="", AG_KEK_FILE=str(f)))
    assert s.kek.get_secret_value() == GOOD_KEK


def test_secret_file_missing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="AG_KEK_FILE"):
        load_settings(prod_env(AG_KEK_FILE=str(tmp_path / "nope")))


def test_secret_and_file_both_set_is_an_error(tmp_path: Path) -> None:
    f = tmp_path / "kek"
    f.write_text(GOOD_KEK)
    with pytest.raises(SettingsError, match="AG_KEK"):
        load_settings(prod_env(AG_KEK_FILE=str(f)))


def test_secrets_never_in_repr() -> None:
    s = load_settings(prod_env())
    text = repr(s) + str(s) + s.model_dump_json()
    assert GOOD_KEK not in text
    assert GOOD_JWT not in text
    assert "Rt7Rt7" not in text


def test_production_with_good_secrets_starts() -> None:
    s = load_settings(prod_env())
    assert s.env == "production"


@pytest.mark.parametrize("name", ["AG_KEK", "AG_JWT_SIGNING_KEY", "AG_DATABASE_URL"])
def test_production_refuses_missing_secret(name: str) -> None:
    env = prod_env()
    del env[name]
    with pytest.raises(SettingsError) as exc:
        load_settings(env)
    assert name in str(exc.value)


@pytest.mark.parametrize("marker", PLACEHOLDER_MARKERS)
@pytest.mark.parametrize("name", ["AG_KEK", "AG_JWT_SIGNING_KEY"])
def test_production_refuses_placeholder_secret(name: str, marker: str) -> None:
    with pytest.raises(SettingsError) as exc:
        load_settings(prod_env(**{name: marker + "x" * 40}))
    assert name in str(exc.value)


def test_production_refuses_short_secret() -> None:
    with pytest.raises(SettingsError, match="AG_KEK"):
        load_settings(prod_env(AG_KEK="short"))


def test_production_refuses_placeholder_database_password() -> None:
    with pytest.raises(SettingsError, match="AG_DATABASE_URL"):
        load_settings(prod_env(AG_DATABASE_URL="postgresql+psycopg://ag:changeme@db/ag"))


def test_production_refuses_database_url_without_password() -> None:
    with pytest.raises(SettingsError, match="AG_DATABASE_URL"):
        load_settings(prod_env(AG_DATABASE_URL="postgresql+psycopg://ag@db/ag"))


def test_production_refuses_same_kek_and_jwt_key() -> None:
    with pytest.raises(SettingsError, match="differ"):
        load_settings(prod_env(AG_JWT_SIGNING_KEY=GOOD_KEK))


def test_production_reports_every_problem_without_secret_values() -> None:
    with pytest.raises(SettingsError) as exc:
        load_settings(prod_env(AG_KEK="changeme-changeme-changeme-changeme", AG_JWT_SIGNING_KEY=""))
    msg = str(exc.value)
    assert "AG_KEK" in msg
    assert "AG_JWT_SIGNING_KEY" in msg
    assert "changeme-changeme" not in msg


def test_development_allows_placeholders() -> None:
    s = load_settings({"AG_ENV": "development", "AG_KEK": "changeme"})
    assert s.kek.get_secret_value() == "changeme"
