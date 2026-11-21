"""`application/problem+json` errors for the device API (`api-contract.md` 7).

Problem codes used here (the client reads `code`): `update_required` (426),
`schema_header_invalid`, `app_version_invalid` (400), `invalid_request`
(422), `enrollment_invalid`, `invalid_credentials`, `token_invalid`,
`device_revoked` (401), `rate_limited` (429).

Validation errors on device paths never echo the request: FastAPI's default
422 body repeats the input, which here would be an enrollment token or a
device secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ag_shared.api import API_PREFIX, PROBLEM_CONTENT_TYPE, Problem
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

UPDATE_REQUIRED: Final = "update_required"
SCHEMA_HEADER_INVALID: Final = "schema_header_invalid"
APP_VERSION_INVALID: Final = "app_version_invalid"
INVALID_REQUEST: Final = "invalid_request"
ENROLLMENT_INVALID: Final = "enrollment_invalid"
INVALID_CREDENTIALS: Final = "invalid_credentials"
TOKEN_INVALID: Final = "token_invalid"  # noqa: S105 - a problem code, not a credential
DEVICE_REVOKED: Final = "device_revoked"
RATE_LIMITED: Final = "rate_limited"

_TITLES: Final = {
    400: "Bad request",
    401: "Unauthorized",
    422: "Invalid request",
    426: "Upgrade required",
    429: "Too many requests",
}


class DeviceProblemError(Exception):
    """Raised anywhere in device request handling; becomes a problem response."""

    def __init__(self, status: int, code: str, *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.headers = dict(headers or {})


def problem_response(
    status: int, code: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """A problem body with `no-store` (device answers can carry credentials)."""
    body = Problem(status=status, title=_TITLES.get(status, "Error"), code=code)
    response = JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=dict(headers or {}),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def unauthorized(code: str) -> DeviceProblemError:
    """A 401 for a bearer-authenticated route."""
    return DeviceProblemError(401, code, headers={"WWW-Authenticate": f'Bearer error="{code}"'})


def is_device_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


async def _device_problem(_request: Request, exc: Exception) -> Response:
    if not isinstance(exc, DeviceProblemError):  # pragma: no cover - registered for this type
        raise exc
    return problem_response(exc.status, exc.code, exc.headers)


async def _validation(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registered for this type
        raise exc
    if is_device_path(request.url.path):
        return problem_response(422, INVALID_REQUEST)
    return await request_validation_exception_handler(request, exc)


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DeviceProblemError, _device_problem)
    app.add_exception_handler(RequestValidationError, _validation)
