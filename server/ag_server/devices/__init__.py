"""Device enrollment and access tokens (task 3.6, `api-contract.md` 3).

- `install_devices(app, ...)`: device routes, console device-admin routes,
  version-header middleware, problem handlers, `app.state.devices`.
- `current_device` / `CurrentDevice`: the device authenticator for 3.7/3.8
  routes (marked through 3.5's `device(...)`); yields a `DeviceIdentity`
  with the token's workstation and its site and line.
- Services in `ag_server.devices.service` (enroll, tokens, rotation,
  revocation, enrollment token issue), all audited.
"""

from __future__ import annotations

from fastapi import FastAPI

from ag_server.devices.auth import (
    CurrentDevice,
    DeviceIdentity,
    authenticate_device,
    current_device,
)
from ag_server.devices.policy import Clock, DevicePolicy, DeviceRuntime, RateLimits
from ag_server.devices.problems import DeviceProblemError, install_problem_handlers
from ag_server.devices.ratelimit import Limit, SlidingWindowLimiter
from ag_server.devices.tokens import DeviceTokenCodec, DeviceTokenError
from ag_server.devices.versioning import VersionHeaderMiddleware


def install_devices(app: FastAPI, *, jwt_key: str, clock: Clock, policy: DevicePolicy) -> None:
    """Wire device auth into `app`."""
    from ag_server.devices.routes import admin_router, device_router

    app.state.devices = DeviceRuntime.build(policy, jwt_key, clock)
    install_problem_handlers(app)
    app.add_middleware(VersionHeaderMiddleware, min_schema=policy.min_schema)
    app.include_router(device_router)
    app.include_router(admin_router)


__all__ = [
    "CurrentDevice",
    "DeviceIdentity",
    "DevicePolicy",
    "DeviceProblemError",
    "DeviceRuntime",
    "DeviceTokenCodec",
    "DeviceTokenError",
    "Limit",
    "RateLimits",
    "SlidingWindowLimiter",
    "VersionHeaderMiddleware",
    "authenticate_device",
    "current_device",
    "install_devices",
]
