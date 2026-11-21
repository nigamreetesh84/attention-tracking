"""Device enrollment API (`/api/v1/device`) and the console's device-admin routes.

Device routes (`api-contract.md` 3):
- `POST /enroll` (public: the enrollment token in the body is the credential)
- `POST /token` (public: workstation id + device secret in the body)
- `POST /secret/rotate` (device token)

Console routes (`config.manage`, scoped; the screens come in 3.10):
- `POST /admin/workstations/{id}/enrollment-token`: issue, shown once
- `POST /admin/workstations/{id}/revoke`

Every answer that carries a credential is `Cache-Control: no-store`.
"""

from __future__ import annotations

import math
import uuid
from typing import Annotated

from ag_shared.api import (
    API_PREFIX,
    EnrollRequest,
    EnrollResponse,
    RotateSecretRequest,
    RotateSecretResponse,
    TokenRequest,
    TokenResponse,
)
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, SecretStr

from ag_server.auth.deps import DbSession
from ag_server.auth.service import clean_ip
from ag_server.devices.auth import CurrentDevice, device_runtime
from ag_server.devices.policy import DeviceRuntime
from ag_server.devices.problems import (
    DEVICE_REVOKED,
    ENROLLMENT_INVALID,
    INVALID_CREDENTIALS,
    RATE_LIMITED,
    TOKEN_INVALID,
    DeviceProblemError,
    unauthorized,
)
from ag_server.devices.ratelimit import Limit
from ag_server.devices.service import (
    TokenRefusal,
    enroll,
    grant_token,
    issue_enrollment_token,
    revoke_workstation,
    rotate_secret,
)
from ag_server.devices.versioning import ClientVersion, client_version
from ag_server.models import Workstation
from ag_server.rbac import PUBLIC, Permission, Principal, get_scoped, require

Runtime = Annotated[DeviceRuntime, Depends(device_runtime)]
Version = Annotated[ClientVersion, Depends(client_version)]
ConfigManage = Annotated[Principal, Depends(require(Permission.CONFIG_MANAGE))]

device_router = APIRouter(prefix=API_PREFIX, tags=["device"])
admin_router = APIRouter(prefix="/admin/workstations", tags=["devices-admin"])


def _peer(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _limit(runtime: DeviceRuntime, key: str, limit: Limit) -> None:
    retry = runtime.limiter.hit(key, limit)
    if retry is not None:
        raise DeviceProblemError(429, RATE_LIMITED, headers={"Retry-After": str(math.ceil(retry))})


def _no_store(model: BaseModel, status: int) -> JSONResponse:
    response = JSONResponse(model.model_dump(mode="json"), status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


@device_router.post(
    "/enroll", dependencies=[PUBLIC], status_code=201, response_model=EnrollResponse
)
def post_enroll(
    body: EnrollRequest, request: Request, db: DbSession, runtime: Runtime, version: Version
) -> JSONResponse:
    """One-time enrollment: the token becomes a device secret (shown once)."""
    _limit(runtime, f"enroll-ip:{_peer(request)}", runtime.policy.limits.enroll_per_ip)
    with db.begin():
        done = enroll(
            db,
            body.enrollment_token.get_secret_value(),
            hostname=body.hostname,
            hardware_fingerprint=body.hardware_fingerprint,
            app_version=version.app_version,
            ip=clean_ip(_peer(request)),
            clock=runtime.clock,
        )
    if done is None:
        raise DeviceProblemError(401, ENROLLMENT_INVALID)
    reply = EnrollResponse(
        workstation_id=done.workstation_id,
        device_secret=SecretStr(done.device_secret),
        server_time=runtime.clock(),
    )
    return _no_store(reply, 201)


@device_router.post("/token", dependencies=[PUBLIC], response_model=TokenResponse)
def post_token(
    body: TokenRequest, request: Request, db: DbSession, runtime: Runtime, version: Version
) -> JSONResponse:
    """A 15-minute device JWT for a valid workstation id and secret."""
    limits = runtime.policy.limits
    _limit(runtime, f"token-ip:{_peer(request)}", limits.token_per_ip)
    _limit(runtime, f"token-ws:{body.workstation_id}", limits.token_per_workstation)
    with db.begin():
        result = grant_token(
            db,
            body.workstation_id,
            body.device_secret.get_secret_value(),
            app_version=version.app_version,
            clock=runtime.clock,
        )
    if result is TokenRefusal.REVOKED:
        raise DeviceProblemError(401, DEVICE_REVOKED)
    if isinstance(result, TokenRefusal):
        raise DeviceProblemError(401, INVALID_CREDENTIALS)
    now = runtime.clock()
    token, expires_in = runtime.codec.issue(
        result.workstation_id, result.site_id, result.credential, now
    )
    reply = TokenResponse(access_token=SecretStr(token), expires_in=expires_in, server_time=now)
    return _no_store(reply, 200)


@device_router.post("/secret/rotate", response_model=RotateSecretResponse)
def post_rotate(
    device: CurrentDevice,
    request: Request,
    db: DbSession,
    runtime: Runtime,
    _body: Annotated[RotateSecretRequest | None, Body()] = None,
) -> JSONResponse:
    """A new device secret; the one this token was obtained with stays valid 10 minutes."""
    with db.begin():
        result = rotate_secret(
            db,
            device.workstation_id,
            device.credential,
            ip=clean_ip(_peer(request)),
            clock=runtime.clock,
            policy=runtime.policy,
        )
    if result is TokenRefusal.REVOKED:
        raise unauthorized(DEVICE_REVOKED)
    if isinstance(result, TokenRefusal):
        raise unauthorized(TOKEN_INVALID)
    reply = RotateSecretResponse(
        device_secret=SecretStr(result.device_secret),
        previous_valid_until=result.previous_valid_until,
        server_time=runtime.clock(),
    )
    return _no_store(reply, 200)


# --- console -------------------------------------------------------------------------------


class EnrollmentTokenOut(BaseModel):
    workstation_id: uuid.UUID
    enrollment_token: str
    expires_at: str


class WorkstationStatusOut(BaseModel):
    workstation_id: uuid.UUID
    status: str


@admin_router.post("/{workstation_id}/enrollment-token", status_code=201)
def post_enrollment_token(
    workstation_id: uuid.UUID, p: ConfigManage, request: Request, db: DbSession, runtime: Runtime
) -> JSONResponse:
    """Issue a one-time enrollment token (24 h). Shown in this answer only."""
    with db.begin():
        ws = get_scoped(db, Workstation, workstation_id, p.scope)
        issued = issue_enrollment_token(
            db,
            ws,
            actor_user_id=p.session.user_id,
            ip=clean_ip(_peer(request)),
            clock=runtime.clock,
            policy=runtime.policy,
        )
    out = EnrollmentTokenOut(
        workstation_id=issued.workstation_id,
        enrollment_token=issued.token,
        expires_at=issued.expires_at.isoformat(),
    )
    return _no_store(out, 201)


@admin_router.post("/{workstation_id}/revoke")
def post_revoke(
    workstation_id: uuid.UUID, p: ConfigManage, request: Request, db: DbSession, runtime: Runtime
) -> JSONResponse:
    """Revoke a device; its tokens fail with `device_revoked` from the next request."""
    with db.begin():
        ws = get_scoped(db, Workstation, workstation_id, p.scope)
        revoke_workstation(
            db,
            ws,
            actor_user_id=p.session.user_id,
            ip=clean_ip(_peer(request)),
            clock=runtime.clock,
        )
        status = ws.status.value
    return _no_store(WorkstationStatusOut(workstation_id=workstation_id, status=status), 200)
