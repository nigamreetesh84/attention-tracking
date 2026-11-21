"""Server settings from the environment and Docker secrets.

Every setting is ``AG_<NAME>``. Secrets may instead be given as ``AG_<NAME>_FILE``
pointing at a file (Docker secrets mount them under ``/run/secrets``). In
production mode the server refuses to start with missing, placeholder or weak
secrets (docs/security-privacy.md, "Secrets"). Error messages name the setting,
never its value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

Env = Literal["development", "test", "production"]
ENVS: Final[tuple[str, ...]] = ("development", "test", "production")

# Lower-case fragments that mark a value as a template, not a real secret.
PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = (
    "changeme",
    "change-me",
    "change_me",
    "replace-me",
    "placeholder",
    "default",
    "example",
    "dev-insecure",
    "todo",
)
MIN_SECRET_LENGTH: Final = 32

# The login role the app and worker use. Migrations (run as the schema owner)
# grant it DML on ordinary tables and only INSERT/SELECT on audit_log.
DEFAULT_APP_ROLE: Final = "attentionguard_app"
_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")

_DEV_DATABASE_URL: Final = "postgresql+psycopg://attentionguard@localhost:5432/attentionguard"
_DEV_KEK: Final = "dev-insecure-kek-do-not-use-in-production"
_DEV_JWT: Final = "dev-insecure-jwt-key-do-not-use-in-production"


class SettingsError(Exception):
    """Settings are invalid or unsafe. The message never contains secret values."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    env: Env = "development"
    database_url: SecretStr
    kek: SecretStr
    jwt_signing_key: SecretStr
    # Release 1 refuses to enable blocking (CLAUDE.md rule 1); the field exists
    # so the refusal is explicit.
    blocking_mode: bool = False


def _read_secret(env: Mapping[str, str], name: str, problems: list[str]) -> str | None:
    """Value of ``AG_<name>`` or the contents of ``AG_<name>_FILE``; None if unset."""
    direct = env.get(f"AG_{name}") or None
    file_var = f"AG_{name}_FILE"
    path = env.get(file_var) or None
    if direct is not None and path is not None:
        problems.append(f"AG_{name}: set either AG_{name} or {file_var}, not both")
        return None
    if path is not None:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError:
            problems.append(f"{file_var}: file cannot be read")
            return None
    return direct


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


def _check_secret(name: str, value: str | None, problems: list[str]) -> None:
    if value is None:
        problems.append(f"AG_{name}: missing")
    elif _is_placeholder(value):
        problems.append(f"AG_{name}: looks like a default or placeholder value")
    elif len(value) < MIN_SECRET_LENGTH:
        problems.append(f"AG_{name}: shorter than {MIN_SECRET_LENGTH} characters")


def _check_database_url(
    url: str | None, problems: list[str], production: bool, name: str = "DATABASE_URL"
) -> None:
    if url is None:
        if production:
            problems.append(f"AG_{name}: missing")
        return
    try:
        parsed = make_url(url)
    except ArgumentError:
        problems.append(f"AG_{name}: not a valid database URL")
        return
    if not parsed.drivername.startswith("postgresql"):
        problems.append(f"AG_{name}: must be a PostgreSQL URL")
    elif production:
        if not parsed.password:
            problems.append(f"AG_{name}: production needs a database password")
        elif _is_placeholder(parsed.password):
            problems.append(f"AG_{name}: database password looks like a placeholder")


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``env`` (default: the process environment) and validate them."""
    src: Mapping[str, str] = os.environ if env is None else env
    problems: list[str] = []

    mode = src.get("AG_ENV") or "development"
    if mode not in ENVS:
        raise SettingsError(f"AG_ENV: must be one of {', '.join(ENVS)}")
    production = mode == "production"

    if _parse_bool(src.get("AG_BLOCKING_MODE", "")):
        problems.append("AG_BLOCKING_MODE: blocking mode is not available in release 1")

    db_url = _read_secret(src, "DATABASE_URL", problems)
    kek = _read_secret(src, "KEK", problems)
    jwt_key = _read_secret(src, "JWT_SIGNING_KEY", problems)

    _check_database_url(db_url, problems, production)
    if production:
        _check_secret("KEK", kek, problems)
        _check_secret("JWT_SIGNING_KEY", jwt_key, problems)
        if kek is not None and kek == jwt_key:
            problems.append("AG_KEK and AG_JWT_SIGNING_KEY must differ")

    if problems:
        raise SettingsError("; ".join(problems))

    return Settings(
        env=cast(Env, mode),
        database_url=SecretStr(db_url or _DEV_DATABASE_URL),
        kek=SecretStr(kek or _DEV_KEK),
        jwt_signing_key=SecretStr(jwt_key or _DEV_JWT),
        blocking_mode=False,
    )


class MigrationSettings(BaseModel):
    """What the one-shot migrate step needs: the owner DSN and the app role name.

    Deliberately not `Settings`: the migrate container gets no KEK or JWT key.
    """

    model_config = ConfigDict(frozen=True)

    env: Env = "development"
    database_url: SecretStr
    app_role: str = DEFAULT_APP_ROLE


def app_role_name(env: Mapping[str, str] | None = None) -> str:
    """The app role from ``AG_DB_APP_ROLE`` (default `attentionguard_app`), validated.

    The name is interpolated into GRANT statements (identifiers cannot be bound
    parameters), so only lower-case unquoted PostgreSQL identifiers are allowed.
    """
    src: Mapping[str, str] = os.environ if env is None else env
    role = src.get("AG_DB_APP_ROLE") or DEFAULT_APP_ROLE
    if not _ROLE_NAME.fullmatch(role):
        raise SettingsError(
            "AG_DB_APP_ROLE: must be a lower-case identifier (a-z, 0-9, _; at most 63)"
        )
    return role


def load_migration_settings(env: Mapping[str, str] | None = None) -> MigrationSettings:
    """Settings for running migrations as the schema-owner role.

    ``AG_MIGRATE_DATABASE_URL`` (or ``_FILE``) is the owner DSN. Outside
    production it falls back to ``AG_DATABASE_URL`` and then the development
    default, so a single-role developer database keeps working.
    """
    src: Mapping[str, str] = os.environ if env is None else env
    problems: list[str] = []

    mode = src.get("AG_ENV") or "development"
    if mode not in ENVS:
        raise SettingsError(f"AG_ENV: must be one of {', '.join(ENVS)}")
    production = mode == "production"

    url = _read_secret(src, "MIGRATE_DATABASE_URL", problems)
    _check_database_url(url, problems, production, name="MIGRATE_DATABASE_URL")
    if url is None and not production:
        url = _read_secret(src, "DATABASE_URL", problems)
        _check_database_url(url, problems, production)
    try:
        role = app_role_name(src)
    except SettingsError as exc:
        problems.append(str(exc))
        role = DEFAULT_APP_ROLE

    if problems:
        raise SettingsError("; ".join(problems))
    return MigrationSettings(
        env=cast(Env, mode),
        database_url=SecretStr(url or _DEV_DATABASE_URL),
        app_role=role,
    )
