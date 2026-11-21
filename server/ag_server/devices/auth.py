"""The device authenticator behind 3.5's `device(...)` marker.

`current_device` is the dependency every device route (3.7 ingest, 3.8
config and heartbeat) uses; `CurrentDevice` is its annotated form. It
returns a `DeviceIdentity` whose `workstation_id` and `site_id` come from
the verified token and the workstation row, never from the request body.

Per request: bearer JWT verified (signature, audience, scope, expiry on
the injected clock), then the row is read: revoked -> `401 device_revoked`;
missing, not active, in another site than the token says (moved), or the
secret the token was obtained with no longer valid (replaced by a
re-enrollment, or past its rotation overlap) -> `401 token_invalid`. The
last rule means a token never outlives its secret: after a revoked device
is re-enrolled, tokens of the old PC do not come back to life.

Console cookies are never looked at, so a console session cannot reach a
device route; console routes only read the session cookie, so a device
token cannot reach them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Request

from ag_server.auth.deps import DbSession
from ag_server.devices.policy import DeviceRuntime
from ag_server.devices.problems import DEVICE_REVOKED, TOKEN_INVALID, unauthorized
from ag_server.devices.service import load_device
from ag_server.devices.tokens import DeviceTokenError
from ag_server.models.enums import WorkstationStatus
from ag_server.rbac import device

_SCHEME: Final = "bearer"


@dataclass(frozen=True)
class DeviceIdentity:
    """The authenticated workstation of a device request."""

    workstation_id: uuid.UUID
    site_id: uuid.UUID
    line_id: uuid.UUID
    credential: str
    """Id of the secret the token was obtained with (rotation uses it)."""


def device_runtime(request: Request) -> DeviceRuntime:
    runtime: DeviceRuntime = request.app.state.devices
    return runtime


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization")
    if header is None:
        raise unauthorized(TOKEN_INVALID)
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != _SCHEME or not value:
        raise unauthorized(TOKEN_INVALID)
    return value


def authenticate_device(request: Request, db: DbSession) -> DeviceIdentity:
    """Verify the bearer token and the workstation row; raise a 401 problem otherwise."""
    runtime = device_runtime(request)
    try:
        claims = runtime.codec.verify(_bearer(request), runtime.clock())
    except DeviceTokenError:
        raise unauthorized(TOKEN_INVALID) from None
    with db.begin():
        row = load_device(db, claims.workstation_id)
    if row is None:
        raise unauthorized(TOKEN_INVALID)
    if row.status is WorkstationStatus.REVOKED:
        raise unauthorized(DEVICE_REVOKED)
    if row.status is not WorkstationStatus.ACTIVE or row.site_id != claims.site_id:
        raise unauthorized(TOKEN_INVALID)
    if row.secrets is None or not row.secrets.holds(claims.credential, runtime.clock()):
        raise unauthorized(TOKEN_INVALID)
    return DeviceIdentity(row.workstation_id, row.site_id, row.line_id, claims.credential)


current_device = device(authenticate_device)
"""The marked dependency: `device: CurrentDevice` on every device route."""

CurrentDevice = Annotated[DeviceIdentity, Depends(current_device)]
