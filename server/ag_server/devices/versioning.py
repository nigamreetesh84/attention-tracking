"""`X-AG-Schema` / `X-AG-App-Version` on every device request (`api-contract.md` 2).

Checked in an ASGI middleware for every path under `/api/v1/device`, so the
answer comes before authentication and before the body is parsed (FastAPI
rejects an unparseable body before any dependency runs, which would turn
an old client's `426` into a `422`).

- Schema below `min_schema`: `426 update_required` (the client keeps
  buffering and raises its health flag).
- A newer schema is accepted: changes within a major version are additive
  and unknown fields are ignored.
- Missing or malformed headers: `400`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ag_shared.api import HEADER_APP_VERSION, HEADER_SCHEMA
from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from ag_server.devices.problems import (
    APP_VERSION_INVALID,
    SCHEMA_HEADER_INVALID,
    UPDATE_REQUIRED,
    DeviceProblemError,
    is_device_path,
    problem_response,
)

_APP_VERSION: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_SCHEMA: Final = re.compile(r"[0-9]{1,6}")


@dataclass(frozen=True)
class ClientVersion:
    schema: int
    app_version: str


def parse_client_version(
    schema: str | None, app_version: str | None, min_schema: int
) -> ClientVersion:
    """Validate the two headers; raises `DeviceProblemError` (400 or 426)."""
    if schema is None or not _SCHEMA.fullmatch(schema.strip()):
        raise DeviceProblemError(400, SCHEMA_HEADER_INVALID)
    revision = int(schema)
    if revision < min_schema:
        raise DeviceProblemError(426, UPDATE_REQUIRED)
    if app_version is None or not _APP_VERSION.fullmatch(app_version.strip()):
        raise DeviceProblemError(400, APP_VERSION_INVALID)
    return ClientVersion(revision, app_version.strip())


def client_version(request: Request) -> ClientVersion:
    """The request's versions (the middleware already refused bad ones)."""
    return parse_client_version(
        request.headers.get(HEADER_SCHEMA), request.headers.get(HEADER_APP_VERSION), 0
    )


class VersionHeaderMiddleware:
    """Refuses device requests with missing, malformed or unsupported versions."""

    def __init__(self, app: ASGIApp, *, min_schema: int) -> None:
        self.app = app
        self.min_schema = min_schema

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_device_path(scope.get("path", "")):
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]
            }
            try:
                parse_client_version(
                    headers.get(HEADER_SCHEMA.lower()),
                    headers.get(HEADER_APP_VERSION.lower()),
                    self.min_schema,
                )
            except DeviceProblemError as exc:
                await problem_response(exc.status, exc.code)(scope, receive, send)
                return
        await self.app(scope, receive, send)
