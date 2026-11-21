"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ag_server.db import database_is_up, make_engine
from ag_server.settings import Settings, load_settings

# Marks a route as intentionally unauthenticated. The permission registry and
# route dependency (task 3.5) will read this; health probes carry no data.
PUBLIC_MARKER = {"x-ag-public": True}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Settings are validated here, so an unsafe production config never starts."""
    cfg = settings if settings is not None else load_settings()
    engine = make_engine(cfg)

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

    @app.get("/healthz", openapi_extra=PUBLIC_MARKER)
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Never touches the database."""
        return {"status": "ok"}

    @app.get("/readyz", openapi_extra=PUBLIC_MARKER)
    def readyz() -> JSONResponse:
        """Readiness: the database answers."""
        if database_is_up(engine):
            return JSONResponse({"status": "ok", "database": "up"})
        return JSONResponse({"status": "unavailable", "database": "down"}, status_code=503)

    return app
