"""Device-auth parameters (`api-contract.md` 3) and the per-app runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from ag_server.devices.ratelimit import DEFAULT_MAX_KEYS, Limit, SlidingWindowLimiter
from ag_server.devices.tokens import DeviceTokenCodec

Clock = Callable[[], datetime]


DEFAULT_TOKEN_PER_IP: Final = Limit(120, 60.0)
DEFAULT_TOKEN_PER_WORKSTATION: Final = Limit(10, 60.0)
DEFAULT_ENROLL_PER_IP: Final = Limit(30, 600.0)


@dataclass(frozen=True)
class RateLimits:
    """Defaults sized for a site behind one NAT address.

    A device asks for a token about every 14 minutes, so 10 per minute per
    workstation is generous; 120 per minute per IP lets a site of ~100
    stations reconnect at once after an outage (the rest back off and
    retry, contract 7). Enrollment is a one-off installer step.
    """

    token_per_ip: Limit = DEFAULT_TOKEN_PER_IP
    token_per_workstation: Limit = DEFAULT_TOKEN_PER_WORKSTATION
    enroll_per_ip: Limit = DEFAULT_ENROLL_PER_IP


@dataclass(frozen=True)
class DevicePolicy:
    token_ttl: timedelta = timedelta(minutes=15)
    secret_overlap: timedelta = timedelta(minutes=10)
    enrollment_ttl: timedelta = timedelta(hours=24)
    min_schema: int = 1
    """Oldest `X-AG-Schema` still served; revision-1 queue records stay uploadable."""
    limits: RateLimits = field(default_factory=RateLimits)
    max_rate_keys: int = DEFAULT_MAX_KEYS


@dataclass(frozen=True)
class DeviceRuntime:
    """Everything device routes need that is not per request (`app.state.devices`)."""

    policy: DevicePolicy
    codec: DeviceTokenCodec
    limiter: SlidingWindowLimiter
    clock: Clock

    @classmethod
    def build(cls, policy: DevicePolicy, jwt_key: str, clock: Clock) -> DeviceRuntime:
        limiter = SlidingWindowLimiter(lambda: clock().timestamp(), max_keys=policy.max_rate_keys)
        return cls(policy, DeviceTokenCodec(jwt_key, ttl=policy.token_ttl), limiter, clock)
