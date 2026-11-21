"""Device enrollment, tokens, rotation and revocation against the database.

Every function runs in the caller's transaction and writes its audit entry
in it (security-privacy 5: device enrollment, rotation, revocation; the
issue of an enrollment token is audited too). Secrets and tokens never
reach the audit detail or a log line.

Console callers pass a `Workstation` they loaded through `get_scoped`;
device callers are identified by their token, never by the body.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ag_server.audit import AuditEntry, write_audit
from ag_server.devices.credentials import (
    SecretState,
    credential_id,
    hash_enrollment_token,
    hash_secret,
    new_secret,
)
from ag_server.devices.policy import Clock, DevicePolicy
from ag_server.models import Line, Workstation
from ag_server.models.enums import WorkstationStatus

OBJECT_TYPE = "workstation"


@dataclass(frozen=True)
class DeviceRow:
    """What authentication needs to know about a workstation now."""

    workstation_id: uuid.UUID
    status: WorkstationStatus
    line_id: uuid.UUID
    site_id: uuid.UUID
    secrets: SecretState | None


@dataclass(frozen=True)
class IssuedEnrollmentToken:
    workstation_id: uuid.UUID
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class Enrolled:
    workstation_id: uuid.UUID
    device_secret: str


class TokenRefusal(enum.Enum):
    INVALID = "invalid"
    REVOKED = "revoked"


@dataclass(frozen=True)
class TokenGrant:
    workstation_id: uuid.UUID
    site_id: uuid.UUID
    credential: str


@dataclass(frozen=True)
class Rotated:
    device_secret: str
    previous_valid_until: datetime


def site_of(db: Session, line_id: uuid.UUID) -> uuid.UUID:
    site: uuid.UUID = db.execute(select(Line.site_id).where(Line.id == line_id)).scalar_one()
    return site


def load_device(db: Session, workstation_id: uuid.UUID) -> DeviceRow | None:
    """The workstation's status, line, site and secret state, or None."""
    row = db.execute(
        select(
            Workstation.status, Workstation.line_id, Line.site_id, Workstation.device_secret_hash
        )
        .join(Line, Line.id == Workstation.line_id)
        .where(Workstation.id == workstation_id)
    ).one_or_none()
    if row is None:
        return None
    return DeviceRow(
        workstation_id, WorkstationStatus(row[0]), row[1], row[2], SecretState.decode(row[3])
    )


def _lock(db: Session, workstation_id: uuid.UUID) -> Workstation | None:
    return db.execute(
        select(Workstation).where(Workstation.id == workstation_id).with_for_update()
    ).scalar_one_or_none()


# --- console side --------------------------------------------------------------------------


def issue_enrollment_token(
    db: Session,
    workstation: Workstation,
    *,
    actor_user_id: uuid.UUID,
    ip: str | None,
    clock: Clock,
    policy: DevicePolicy,
) -> IssuedEnrollmentToken:
    """A new one-time token for `workstation` (replaces any earlier one). Shown once."""
    now = clock()
    token = new_secret()
    workstation.enrollment_token_hash = hash_enrollment_token(token)
    workstation.enrollment_expires_at = now + policy.enrollment_ttl
    db.flush()
    write_audit(
        db,
        AuditEntry(
            action="device.enrollment_token_issued",
            actor_user_id=actor_user_id,
            site_id=site_of(db, workstation.line_id),
            object_type=OBJECT_TYPE,
            object_id=str(workstation.id),
            ip=ip,
            detail={
                "expires_at": workstation.enrollment_expires_at.isoformat(),
                "status": workstation.status.value,
            },
        ),
        now=clock,
    )
    return IssuedEnrollmentToken(workstation.id, token, workstation.enrollment_expires_at)


def revoke_workstation(
    db: Session, workstation: Workstation, *, actor_user_id: uuid.UUID, ip: str | None, clock: Clock
) -> None:
    """Revoke the device: its tokens fail with `device_revoked` from the next request.

    A pending enrollment token is cancelled too. The secret hash is kept so
    the device, presenting its secret, learns that it is revoked (contract
    3); re-enrollment with a new token replaces it.
    """
    previous = workstation.status
    workstation.status = WorkstationStatus.REVOKED
    workstation.enrollment_token_hash = None
    workstation.enrollment_expires_at = None
    db.flush()
    write_audit(
        db,
        AuditEntry(
            action="device.revoked",
            actor_user_id=actor_user_id,
            site_id=site_of(db, workstation.line_id),
            object_type=OBJECT_TYPE,
            object_id=str(workstation.id),
            ip=ip,
            detail={"previous_status": previous.value},
        ),
        now=clock,
    )


# --- device side ---------------------------------------------------------------------------


def enroll(
    db: Session,
    token: str,
    *,
    hostname: str,
    hardware_fingerprint: str,
    app_version: str,
    ip: str | None,
    clock: Clock,
) -> Enrolled | None:
    """Exchange a valid, unexpired enrollment token for a new device secret; else None.

    Single use: the row is locked and the token hash cleared in the same
    transaction, so a concurrent second attempt finds nothing.
    """
    now = clock()
    workstation = db.execute(
        select(Workstation)
        .where(Workstation.enrollment_token_hash == hash_enrollment_token(token))
        .with_for_update()
    ).scalar_one_or_none()
    expires = workstation.enrollment_expires_at if workstation is not None else None
    if workstation is None or expires is None or now >= expires:
        return None
    secret = new_secret()
    previous = workstation.status
    workstation.device_secret_hash = SecretState(hash_secret(secret)).encode()
    workstation.enrollment_token_hash = None
    workstation.enrollment_expires_at = None
    workstation.status = WorkstationStatus.ACTIVE
    workstation.enrolled_at = now
    workstation.app_version = app_version
    db.flush()
    write_audit(
        db,
        AuditEntry(
            action="device.enrolled",
            actor_workstation_id=workstation.id,
            site_id=site_of(db, workstation.line_id),
            object_type=OBJECT_TYPE,
            object_id=str(workstation.id),
            ip=ip,
            detail={
                "hostname": hostname,
                "hardware_fingerprint": hardware_fingerprint,
                "app_version": app_version,
                "previous_status": previous.value,
            },
        ),
        now=clock,
    )
    return Enrolled(workstation.id, secret)


def grant_token(
    db: Session, workstation_id: uuid.UUID, secret: str, *, app_version: str, clock: Clock
) -> TokenGrant | TokenRefusal:
    """Check a device secret. Unknown, pending and wrong-secret all give `INVALID`.

    `REVOKED` only when the secret is right, so the revocation is not
    disclosed to someone without it.
    """
    now = clock()
    workstation = db.get(Workstation, workstation_id)
    state = SecretState.decode(workstation.device_secret_hash if workstation else None)
    # Hash even when there is nothing to compare, so the paths cost the same.
    matched = (state or SecretState("0" * 64)).match(secret, now)
    if workstation is None or state is None or matched is None:
        return TokenRefusal.INVALID
    if workstation.status is WorkstationStatus.REVOKED:
        return TokenRefusal.REVOKED
    if workstation.status is not WorkstationStatus.ACTIVE:
        return TokenRefusal.INVALID
    if workstation.app_version != app_version:
        workstation.app_version = app_version
    return TokenGrant(workstation.id, site_of(db, workstation.line_id), credential_id(matched))


def rotate_secret(
    db: Session,
    workstation_id: uuid.UUID,
    proven_credential: str,
    *,
    ip: str | None,
    clock: Clock,
    policy: DevicePolicy,
) -> Rotated | TokenRefusal:
    """Issue a new secret; the one the caller proved stays valid for the overlap."""
    now = clock()
    workstation = _lock(db, workstation_id)
    if workstation is None:
        return TokenRefusal.INVALID
    if workstation.status is WorkstationStatus.REVOKED:
        return TokenRefusal.REVOKED
    state = SecretState.decode(workstation.device_secret_hash)
    if workstation.status is not WorkstationStatus.ACTIVE or state is None:
        return TokenRefusal.INVALID
    secret = new_secret()
    rotated = state.rotate(
        hash_secret(secret), proven=proven_credential, now=now, overlap=policy.secret_overlap
    )
    if rotated is None or rotated.previous_until is None:
        return TokenRefusal.INVALID
    workstation.device_secret_hash = rotated.encode()
    db.flush()
    write_audit(
        db,
        AuditEntry(
            action="device.secret_rotated",
            actor_workstation_id=workstation.id,
            site_id=site_of(db, workstation.line_id),
            object_type=OBJECT_TYPE,
            object_id=str(workstation.id),
            ip=ip,
            detail={"previous_valid_until": rotated.previous_until.isoformat()},
        ),
        now=clock,
    )
    return Rotated(secret, rotated.previous_until)
