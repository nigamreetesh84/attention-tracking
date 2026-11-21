"""Device access tokens: HS256 JWTs with the claims of `api-contract.md` 3.

Claims: `sub` = workstation id, `site_id`, `scope = device`, `iss`, `aud`,
`iat`, `exp` (15 minutes), `jti`, and `cred` (the id of the secret the token
was obtained with, used by rotation). Time is checked against the injected
clock, not PyJWT's wall clock, so tests and the server agree.

A token is only half the check: `authenticate_device` also reads the
workstation row on every request (revocation, site moves).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import jwt

ISSUER: Final = "attentionguard"
AUDIENCE: Final = "attentionguard:device"
SCOPE: Final = "device"
ALGORITHM: Final = "HS256"
IAT_LEEWAY_S: Final = 30
"""Tolerance for a token issued slightly in the future (several app processes)."""
_CRED: Final = re.compile(r"[0-9a-f]{16}")


class DeviceTokenError(Exception):
    """The token is not a valid, current device token. The message never holds the token."""


@dataclass(frozen=True)
class DeviceClaims:
    """What a verified token says; the row still has to agree."""

    workstation_id: uuid.UUID
    site_id: uuid.UUID
    credential: str
    expires_at: datetime


class DeviceTokenCodec:
    """Issues and verifies device tokens with the settings' JWT signing key."""

    def __init__(self, key: str, *, ttl: timedelta) -> None:
        if len(key.encode("utf-8")) < 32:
            raise ValueError("the JWT signing key must be at least 32 bytes")
        if ttl <= timedelta(0):
            raise ValueError("token lifetime must be positive")
        self._key = key
        self._ttl_s = int(ttl.total_seconds())

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_s

    def issue(
        self, workstation_id: uuid.UUID, site_id: uuid.UUID, credential: str, now: datetime
    ) -> tuple[str, int]:
        """A signed token and its lifetime in seconds (`expires_in`)."""
        iat = int(now.timestamp())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(workstation_id),
            "site_id": str(site_id),
            "scope": SCOPE,
            "cred": credential,
            "iat": iat,
            "exp": iat + self._ttl_s,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self._key, algorithm=ALGORITHM), self._ttl_s

    def verify(self, token: str, now: datetime) -> DeviceClaims:
        """The claims of a valid device token at `now`, else `DeviceTokenError`."""
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=ISSUER,
                options={
                    "verify_exp": False,  # checked below against the injected clock
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": ["iss", "aud", "sub", "iat", "exp"],
                },
            )
        except jwt.PyJWTError:
            raise DeviceTokenError("invalid token") from None
        ts = now.timestamp()
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, int) or not isinstance(iat, int):
            raise DeviceTokenError("bad time claims")
        if ts >= exp or iat > ts + IAT_LEEWAY_S:
            raise DeviceTokenError("expired or not yet valid")
        if claims.get("scope") != SCOPE:
            raise DeviceTokenError("not a device token")
        cred = claims.get("cred")
        if not isinstance(cred, str) or not _CRED.fullmatch(cred):
            raise DeviceTokenError("bad cred claim")
        try:
            workstation_id = uuid.UUID(str(claims["sub"]))
            site_id = uuid.UUID(str(claims.get("site_id")))
        except ValueError:
            raise DeviceTokenError("bad id claims") from None
        return DeviceClaims(workstation_id, site_id, cred, datetime.fromtimestamp(exp, UTC))
