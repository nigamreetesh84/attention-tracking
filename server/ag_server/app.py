"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from ag_shared.i18n import DEFAULT_LOCALE, Translator
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ag_server.auth.passwords import default_password_hasher
from ag_server.auth.provider import LocalAuthProvider
from ag_server.auth.routes import install_auth
from ag_server.auth.service import AuthPolicy, AuthRuntime, Clock
from ag_server.crypto import KeyRing, derive_subkey
from ag_server.db import database_is_up, make_engine
from ag_server.devices import DevicePolicy, install_devices
from ag_server.rbac import PUBLIC, install_rbac
from ag_server.settings import Settings, load_settings


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    password_hasher: PasswordHasher | None = None,
    device_policy: DevicePolicy | None = None,
) -> FastAPI:
    """Build the app. Settings are validated here, so an unsafe production config never starts.

    `clock`, `password_hasher` and `device_policy` are injection points for
    tests (a fake clock, cheaper argon2 parameters, other rate limits);
    production uses UTC now and the defaults.
    """
    cfg = settings if settings is not None else load_settings()
    engine = make_engine(cfg)
    hasher = password_hasher if password_hasher is not None else default_password_hasher()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="AttentionGuard",
        lifespan=lifespan,
        docs_url=None if cfg.env == "production" else "/docs",
        redoc_url=None,
        openapi_url="/openapi.json" if cfg.env != "production" else None,
    )
    app.state.settings = cfg
    app.state.engine = engine
    app.state.auth = AuthRuntime(
        policy=AuthPolicy.from_settings(cfg),
        provider=LocalAuthProvider(hasher),
        ring=KeyRing.from_settings(cfg),
        csrf_key=derive_subkey(cfg.kek.get_secret_value(), "csrf"),
        hasher=hasher,
        clock=clock if clock is not None else _utc_now,
    )
    install_auth(app, Translator(DEFAULT_LOCALE, strict=cfg.env == "test"))
    install_rbac(app)
    install_devices(
        app,
        jwt_key=cfg.jwt_signing_key.get_secret_value(),
        clock=app.state.auth.clock,
        policy=device_policy if device_policy is not None else DevicePolicy(),
    )

    @app.get("/healthz", dependencies=[PUBLIC])
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Never touches the database. Public: no data."""
        return {"status": "ok"}

    @app.get("/readyz", dependencies=[PUBLIC])
    def readyz() -> JSONResponse:
        """Readiness: the database answers."""
        if database_is_up(engine):
            return JSONResponse({"status": "ok", "database": "up"})
        return JSONResponse({"status": "unavailable", "database": "down"}, status_code=503)

    return app
