"""Console sign-in, MFA, sessions and lockout (security-privacy 3).

`ConsoleAuth` works on one SQLAlchemy session per request and commits its own
transactions, so a failed sign-in still records its counter and audit row.
Every audit entry goes through `write_audit` in the same transaction as the
change it describes and never contains a password, code, secret or token.

Session states:

- *partial*: password accepted, MFA still owed (`next_step` is
  `mfa_verify` or `mfa_enroll`). Reaches only the MFA routes and logout.
  Lives `mfa_pending_minutes`.
- *complete*: `next_step` is `home`.

Whether MFA is owed is decided on every request, not only at sign-in: a
user who enabled MFA, or whose role now requires it (for example a
Supervisor promoted to Admin), is sent to MFA on the next request.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from argon2 import PasswordHasher
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ag_server.audit import AuditEntry, write_audit
from ag_server.auth.mfa import (
    MfaState,
    generate_recovery_codes,
    new_pending_state,
    open_mfa,
    provisioning_uri,
    seal_mfa,
    use_recovery_code,
    verify_totp,
)
from ag_server.auth.passwords import PolicyViolation, check_password_policy
from ag_server.auth.provider import AuthProvider
from ag_server.auth.sessions import csrf_token_for, new_token, plausible_token, session_key
from ag_server.crypto import EnvelopeError, KeyRing
from ag_server.models import Role, User, WebSession
from ag_server.settings import CONSOLE_ROLES, Settings

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
LOCKOUT_THRESHOLD: Final = 5
LOCKOUT_DURATION: Final = timedelta(minutes=15)
_USER_AGENT_MAX: Final = 512


class NextStep(StrEnum):
    HOME = "home"
    MFA_VERIFY = "mfa_verify"
    MFA_ENROLL = "mfa_enroll"


@dataclass(frozen=True)
class AuthPolicy:
    idle: timedelta
    absolute: timedelta
    mfa_pending: timedelta
    mfa_required_roles: frozenset[str]
    console_roles: frozenset[str] = frozenset(CONSOLE_ROLES)
    lockout_threshold: int = LOCKOUT_THRESHOLD
    lockout_duration: timedelta = LOCKOUT_DURATION

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthPolicy:
        return cls(
            idle=timedelta(minutes=settings.session_idle_minutes),
            absolute=timedelta(minutes=settings.session_absolute_minutes),
            mfa_pending=timedelta(minutes=settings.mfa_pending_minutes),
            mfa_required_roles=frozenset(settings.mfa_required_roles),
        )


@dataclass(frozen=True)
class AuthRuntime:
    """Everything the auth code needs that is not per request."""

    policy: AuthPolicy
    provider: AuthProvider
    ring: KeyRing
    csrf_key: bytes = field(repr=False)
    hasher: PasswordHasher
    clock: Clock


@dataclass(frozen=True)
class ConsoleSession:
    """The signed-in console user of a request (what `current_session` returns)."""

    token: str = field(repr=False)
    key: bytes = field(repr=False)
    user_id: uuid.UUID
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    site_id: uuid.UUID | None
    mfa_passed: bool
    next_step: NextStep
    csrf_token: str = field(repr=False)
    created_at: datetime
    expires_at: datetime

    @property
    def complete(self) -> bool:
        return self.next_step is NextStep.HOME


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    token: str | None = field(default=None, repr=False)
    next_step: NextStep = NextStep.HOME


class MfaOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    LOCKED = "locked"


@dataclass(frozen=True)
class MfaResult:
    outcome: MfaOutcome
    token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Enrollment:
    secret: str = field(repr=False)
    uri: str = field(repr=False)


@dataclass(frozen=True)
class EnrollmentResult:
    token: str = field(repr=False)
    recovery_codes: tuple[str, ...] = field(repr=False)


def clean_ip(raw: str | None) -> str | None:
    """A single IP address or None (audit and `web_session.ip` are INET)."""
    if raw is None:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


class ConsoleAuth:
    """Sign-in and session operations for one request's database session."""

    def __init__(self, runtime: AuthRuntime, db: Session) -> None:
        self._rt = runtime
        self._db = db

    # --- helpers --------------------------------------------------------------

    def _now(self) -> datetime:
        return self._rt.clock()

    def _audit(
        self,
        action: str,
        user: User | None,
        ip: str | None,
        detail: dict[str, object] | None = None,
        *,
        authenticated: bool = False,
    ) -> None:
        write_audit(
            self._db,
            AuditEntry(
                action=action,
                actor_user_id=user.id if user is not None and authenticated else None,
                site_id=user.site_id if user is not None else None,
                object_type="user" if user is not None else None,
                object_id=str(user.id) if user is not None else None,
                ip=clean_ip(ip),
                detail=detail or {},
            ),
            now=self._rt.clock,
        )

    def _role(self, user: User) -> Role:
        return self._db.execute(select(Role).where(Role.id == user.role_id)).scalar_one()

    def _mfa_owed(self, user: User, role: Role) -> bool:
        return user.mfa_enabled or role.name in self._rt.policy.mfa_required_roles

    def _next_step(self, user: User, role: Role, mfa_passed: bool) -> NextStep:
        if mfa_passed or not self._mfa_owed(user, role):
            return NextStep.HOME
        return NextStep.MFA_VERIFY if user.mfa_enabled else NextStep.MFA_ENROLL

    def _locked(self, user: User, now: datetime) -> bool:
        return user.locked_until is not None and user.locked_until > now

    def _register_failure(self, user: User, now: datetime, ip: str | None) -> bool:
        """Count a failed password or code; True if this failure locked the account."""
        if user.locked_until is not None and user.locked_until <= now:
            user.failed_attempts = 0
            user.locked_until = None
        user.failed_attempts += 1
        if user.failed_attempts >= self._rt.policy.lockout_threshold:
            user.locked_until = now + self._rt.policy.lockout_duration
            self._audit(
                "auth.lockout",
                user,
                ip,
                {"minutes": int(self._rt.policy.lockout_duration.total_seconds() // 60)},
            )
            return True
        return False

    def _clear_failures(self, user: User) -> None:
        user.failed_attempts = 0
        user.locked_until = None

    def _new_session(
        self,
        user: User,
        *,
        now: datetime,
        created_at: datetime,
        expires_at: datetime,
        mfa_passed: bool,
        ip: str | None,
        user_agent: str | None,
    ) -> str:
        token = new_token()
        self._db.add(
            WebSession(
                id=session_key(token),
                user_id=user.id,
                created_at=created_at,
                last_seen_at=now,
                expires_at=expires_at,
                mfa_passed=mfa_passed,
                ip=clean_ip(ip),
                user_agent=(user_agent or "")[:_USER_AGENT_MAX] or None,
            )
        )
        return token

    def _revoke_key(self, key: bytes, now: datetime) -> None:
        self._db.execute(
            update(WebSession)
            .where(WebSession.id == key, WebSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    def _lock_user(self, user_id: uuid.UUID) -> User:
        return self._db.execute(
            select(User).where(User.id == user_id).with_for_update()
        ).scalar_one()

    def _rotate(
        self,
        session: ConsoleSession,
        user: User,
        now: datetime,
        ip: str | None,
        ua: str | None,
        *,
        mfa_passed: bool,
    ) -> str:
        """Replace the session with a new id. The absolute limit still counts from the
        password sign-in; a session that just passed MFA gets the full lifetime from there."""
        row = self._db.get(WebSession, session.key)
        self._revoke_key(session.key, now)
        login_at = row.created_at if row is not None else now
        expires_at = (
            login_at + self._rt.policy.absolute
            if mfa_passed and not session.mfa_passed
            else session.expires_at
        )
        return self._new_session(
            user,
            now=now,
            created_at=login_at,
            expires_at=expires_at,
            mfa_passed=mfa_passed,
            ip=ip,
            user_agent=ua,
        )

    # --- sign-in -----------------------------------------------------------------

    def login(
        self,
        username: str,
        password: str,
        *,
        ip: str | None,
        user_agent: str | None,
        replaces: str | None = None,
    ) -> LoginResult:
        """Check credentials; on success create a (possibly partial) session.

        Every failure returns the same result, whatever the cause.
        """
        now = self._now()
        with self._db.begin():
            check = self._rt.provider.authenticate(self._db, username, password)
            user = check.user
            if user is None:
                self._audit("auth.login_failure", None, ip, {"reason": "unknown_user"})
                return LoginResult(ok=False)
            role = self._role(user)
            reason: str | None = None
            if self._locked(user, now):
                reason = "locked"
            elif not check.password_ok:
                reason = "bad_password" if user.password_hash else "no_password"
                self._register_failure(user, now, ip)
            elif not user.is_active:
                reason = "inactive"
            elif role.name not in self._rt.policy.console_roles:
                reason = "no_console_access"
            if reason is not None:
                self._audit("auth.login_failure", user, ip, {"reason": reason})
                return LoginResult(ok=False)

            self._clear_failures(user)
            if replaces is not None and plausible_token(replaces):
                self._revoke_key(session_key(replaces), now)
            step = self._next_step(user, role, mfa_passed=False)
            lifetime = (
                self._rt.policy.absolute if step is NextStep.HOME else self._rt.policy.mfa_pending
            )
            token = self._new_session(
                user,
                now=now,
                created_at=now,
                expires_at=now + lifetime,
                mfa_passed=False,
                ip=ip,
                user_agent=user_agent,
            )
            self._audit(
                "auth.login",
                user,
                ip,
                {"provider": self._rt.provider.name, "mfa": step.value},
                authenticated=True,
            )
        return LoginResult(ok=True, token=token, next_step=step)

    def load(self, token: str | None) -> ConsoleSession | None:
        """The live session for a cookie token, or None. Expired sessions are revoked.

        A valid session's `last_seen_at` is refreshed (idle timeout).
        """
        if token is None or not plausible_token(token):
            return None
        key = session_key(token)
        now = self._now()
        with self._db.begin():
            row = self._db.get(WebSession, key, with_for_update=True)
            if row is None or row.revoked_at is not None:
                return None
            if now >= row.expires_at or now - row.last_seen_at > self._rt.policy.idle:
                row.revoked_at = now
                return None
            user = self._db.get(User, row.user_id)
            role = self._role(user) if user is not None else None
            if (
                user is None
                or role is None
                or not user.is_active
                or role.name not in self._rt.policy.console_roles
            ):
                row.revoked_at = now
                return None
            row.last_seen_at = now
            return ConsoleSession(
                token=token,
                key=key,
                user_id=user.id,
                username=user.username or "",
                display_name=user.display_name,
                role=role.name,
                permissions=frozenset(str(p) for p in role.permissions),
                site_id=user.site_id,
                mfa_passed=row.mfa_passed,
                next_step=self._next_step(user, role, row.mfa_passed),
                csrf_token=csrf_token_for(self._rt.csrf_key, token),
                created_at=row.created_at,
                expires_at=row.expires_at,
            )

    def logout(self, session: ConsoleSession, *, ip: str | None) -> None:
        now = self._now()
        with self._db.begin():
            self._revoke_key(session.key, now)
            user = self._db.get(User, session.user_id)
            self._audit("auth.logout", user, ip, authenticated=True)

    # --- MFA --------------------------------------------------------------------------

    def _state(self, user: User) -> MfaState | None:
        if user.totp_secret_encrypted is None:
            return None
        try:
            return open_mfa(self._rt.ring, user.id, user.totp_secret_encrypted)
        except (EnvelopeError, ValueError, KeyError):
            # Wrong KEK or damaged value: MFA cannot pass. Ids only in the log.
            log.error("MFA state of user %s does not open", user.id)
            return None

    def verify_mfa(
        self, session: ConsoleSession, code: str, *, ip: str | None, user_agent: str | None
    ) -> MfaResult:
        """Check a TOTP or recovery code; on success rotate to a complete session."""
        now = self._now()
        with self._db.begin():
            user = self._lock_user(session.user_id)
            if self._locked(user, now):
                self._revoke_key(session.key, now)
                self._audit("auth.mfa_failure", user, ip, {"reason": "locked"})
                return MfaResult(MfaOutcome.LOCKED)
            state = self._state(user) if user.mfa_enabled else None
            new_state: MfaState | None = None
            detail: dict[str, object] = {}
            if state is not None and state.confirmed:
                step = verify_totp(state, code, now)
                if step is not None:
                    new_state = state.after_totp(step)
                    detail = {"method": "totp"}
                else:
                    new_state = use_recovery_code(state, code)
                    if new_state is not None:
                        detail = {
                            "method": "recovery_code",
                            "remaining": len(new_state.recovery_hashes),
                        }
            if new_state is None:
                locked = self._register_failure(user, now, ip)
                self._audit("auth.mfa_failure", user, ip, {"reason": "bad_code"})
                if locked:
                    self._revoke_key(session.key, now)
                    return MfaResult(MfaOutcome.LOCKED)
                return MfaResult(MfaOutcome.FAILED)
            user.totp_secret_encrypted = seal_mfa(self._rt.ring, user.id, new_state)
            self._clear_failures(user)
            token = self._rotate(session, user, now, ip, user_agent, mfa_passed=True)
            self._audit("auth.mfa_verified", user, ip, detail, authenticated=True)
        return MfaResult(MfaOutcome.OK, token=token)

    def begin_enrollment(self, session: ConsoleSession, *, ip: str | None) -> Enrollment | None:
        """Store a fresh, unconfirmed TOTP secret; None if the user already has MFA."""
        with self._db.begin():
            user = self._lock_user(session.user_id)
            if user.mfa_enabled:
                return None
            state = new_pending_state()
            user.totp_secret_encrypted = seal_mfa(self._rt.ring, user.id, state)
            self._audit("auth.mfa_enrollment_started", user, ip, authenticated=True)
        return Enrollment(secret=state.secret, uri=provisioning_uri(state, user.username or ""))

    def pending_enrollment(self, session: ConsoleSession) -> Enrollment | None:
        """The stored unconfirmed secret, to show the form again after a wrong code."""
        with self._db.begin():
            user = self._db.get(User, session.user_id)
            if user is None or user.mfa_enabled:
                return None
            state = self._state(user)
        if state is None or state.confirmed:
            return None
        return Enrollment(secret=state.secret, uri=provisioning_uri(state, user.username or ""))

    def complete_enrollment(
        self, session: ConsoleSession, code: str, *, ip: str | None, user_agent: str | None
    ) -> EnrollmentResult | None:
        """Confirm the pending secret with a code; issue recovery codes and rotate the session."""
        now = self._now()
        with self._db.begin():
            user = self._lock_user(session.user_id)
            state = None if user.mfa_enabled else self._state(user)
            step = verify_totp(state, code, now) if state is not None else None
            if state is None or step is None:
                self._audit(
                    "auth.mfa_failure", user, ip, {"reason": "bad_code", "stage": "enrollment"}
                )
                return None
            codes = generate_recovery_codes()
            confirmed = state.confirm().after_totp(step).with_recovery_codes(codes)
            user.totp_secret_encrypted = seal_mfa(self._rt.ring, user.id, confirmed)
            user.mfa_enabled = True
            self._clear_failures(user)
            token = self._rotate(session, user, now, ip, user_agent, mfa_passed=True)
            self._audit(
                "auth.mfa_enrolled", user, ip, {"codes_issued": len(codes)}, authenticated=True
            )
        return EnrollmentResult(token=token, recovery_codes=tuple(codes))

    # --- passwords and step-up -----------------------------------------------------------

    def set_password(
        self,
        user_id: uuid.UUID,
        new_password: str,
        *,
        actor: ConsoleSession | None,
        ip: str | None,
    ) -> list[PolicyViolation]:
        """Set a console password after the policy check; ends all of the user's sessions.

        Reuse is checked against the current hash only: schema.md has no
        password history yet (owner decision pending, see PROGRESS 3.4).
        """
        now = self._now()
        with self._db.begin():
            user = self._lock_user(user_id)
            previous = [user.password_hash] if user.password_hash else []
            problems = check_password_policy(
                new_password,
                username=user.username or "",
                previous_hashes=previous,
                hasher=self._rt.hasher,
            )
            if problems:
                return problems
            user.password_hash = self._rt.hasher.hash(new_password)
            user.password_changed_at = now
            self._clear_failures(user)
            self._db.execute(
                update(WebSession)
                .where(WebSession.user_id == user.id, WebSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            write_audit(
                self._db,
                AuditEntry(
                    action="auth.password_changed",
                    actor_user_id=actor.user_id if actor is not None else None,
                    site_id=user.site_id,
                    object_type="user",
                    object_id=str(user.id),
                    ip=clean_ip(ip),
                    detail={"by_self": actor is not None and actor.user_id == user.id},
                ),
                now=self._rt.clock,
            )
        return []

    def rotate(self, session: ConsoleSession, *, ip: str | None, user_agent: str | None) -> str:
        """Issue a new session id for a complete session (privilege step-up). Returns the token."""
        now = self._now()
        with self._db.begin():
            user = self._lock_user(session.user_id)
            token = self._rotate(session, user, now, ip, user_agent, mfa_passed=session.mfa_passed)
            self._audit("auth.session_rotated", user, ip, authenticated=True)
        return token
