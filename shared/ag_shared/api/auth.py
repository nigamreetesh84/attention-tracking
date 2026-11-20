"""Enrollment, access token and secret rotation (`api-contract.md` section 3)."""

from typing import Annotated
from uuid import UUID

from pydantic import Field

from ag_shared.api.common import ApiModel, UtcDatetime, WireSecret


class EnrollRequest(ApiModel):
    """`POST /enroll`: exchange the one-time enrollment token for a device secret."""

    enrollment_token: WireSecret
    hostname: Annotated[str, Field(min_length=1, max_length=255)]
    hardware_fingerprint: Annotated[str, Field(min_length=1, max_length=256)]


class EnrollResponse(ApiModel):
    """The device secret is shown once; the device stores it with DPAPI."""

    workstation_id: UUID
    device_secret: WireSecret
    server_time: UtcDatetime


class TokenRequest(ApiModel):
    """`POST /token`."""

    workstation_id: UUID
    device_secret: WireSecret


class TokenResponse(ApiModel):
    """A device JWT (15 minutes). `server_time` is optional for older servers."""

    access_token: WireSecret
    expires_in: Annotated[int, Field(gt=0)]
    """Seconds until the token expires."""
    server_time: UtcDatetime | None = None


class RotateSecretRequest(ApiModel):
    """`POST /secret/rotate`: no body fields; the device is taken from the token."""


class RotateSecretResponse(ApiModel):
    """The new secret. The old one stays valid for 10 minutes."""

    device_secret: WireSecret
    previous_valid_until: UtcDatetime | None = None
    server_time: UtcDatetime | None = None
