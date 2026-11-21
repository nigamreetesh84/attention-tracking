"""Employee ID + PIN login against the cached roster (D7, Q3/Q4 defaults).

Input rules (owner defaults Q3/Q4):

- Employee ID: surrounding whitespace is stripped, then 1-16 ASCII letters
  or digits. Matching is case-insensitive (the roster is unique under
  case-folding), and the lockout is keyed by the case-folded ID, so `emp42`
  and `EMP42` share one counter.
- PIN: 4-6 ASCII digits, nothing stripped.

An unknown ID, an inactive worker and a worker without a PIN are verified
against a dummy argon2id hash with the roster's own cost parameters, so they
take about as long as a wrong PIN, count towards the lockout the same way and
get the same generic result. PINs and hashes are never logged.
"""

import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from ag_shared.api import ConfigBundle, SessionMethod
from argon2 import Parameters, PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ag_workstation.auth.lockout import LoginThrottle
from ag_workstation.auth.methods import AuthOutcome, AuthResult, LoginRequest
from ag_workstation.auth.roster import RosterSource
from ag_workstation.clock import Clock

logger = logging.getLogger(__name__)

PIN_METHOD_NAME: Final = "employee_id_pin"
EMPLOYEE_ID_RE: Final = re.compile(r"[A-Za-z0-9]{1,16}")
PIN_RE: Final = re.compile(r"[0-9]{4,6}")


@dataclass(frozen=True, slots=True)
class PinCredentials(LoginRequest):
    """What the worker typed. The PIN never appears in `repr`."""

    employee_id: str
    pin: str = field(repr=False)


def normalize_employee_id(raw: str) -> str | None:
    """The employee ID as matched (stripped), or None if its format is invalid."""
    value = raw.strip()
    return value if EMPLOYEE_ID_RE.fullmatch(value) else None


def valid_pin(pin: str) -> bool:
    """Whether `pin` is 4-6 ASCII digits."""
    return PIN_RE.fullmatch(pin) is not None


class PinLoginMethod:
    """The built-in `employee_id_pin` login method."""

    def __init__(
        self,
        roster: RosterSource,
        throttle: LoginThrottle,
        clock: Clock,
        *,
        hasher: PasswordHasher | None = None,
    ) -> None:
        """`hasher` only sets the dummy-hash cost when the roster has no hash to copy."""
        self._roster = roster
        self._throttle = throttle
        self._clock = clock
        self._hasher = hasher or PasswordHasher()
        self._dummy_lock = threading.Lock()
        self._dummies: dict[tuple[int, ...], str] = {}
        self._default_params: Parameters | None = None

    @property
    def name(self) -> str:
        """`employee_id_pin`."""
        return PIN_METHOD_NAME

    @property
    def session_method(self) -> SessionMethod:
        """`employee_id_pin`."""
        return SessionMethod.EMPLOYEE_ID_PIN

    def authenticate(self, request: LoginRequest) -> AuthResult:
        """Check an employee ID and PIN offline; see the module docstring."""
        if not isinstance(request, PinCredentials):
            return AuthResult(AuthOutcome.INVALID_INPUT)
        employee_id = normalize_employee_id(request.employee_id)
        if employee_id is None or not valid_pin(request.pin):
            return AuthResult(AuthOutcome.INVALID_INPUT)
        subject = employee_id.casefold()

        status = self._throttle.status(subject)
        if status.locked_until is not None:
            return AuthResult(
                AuthOutcome.LOCKED,
                subject=subject,
                retry_after=status.locked_until - self._clock.now(),
            )

        bundle = self._roster.current_bundle()
        if bundle is None:
            logger.warning("PIN login refused: no roster has been applied yet")
            return AuthResult(AuthOutcome.UNAVAILABLE, subject=subject)

        entry = bundle.roster_entry(employee_id)
        target = entry.pin_hash if entry is not None and entry.is_active else None
        matched = self._verify(target or self._dummy_hash(bundle), request.pin)
        if matched and target is not None and entry is not None:
            self._throttle.record_success(subject)
            return AuthResult(AuthOutcome.SUCCESS, user=entry, subject=subject)

        failure = self._throttle.record_failure(subject)
        named = entry.user_id if entry is not None else None
        if failure.locked_until is not None:
            logger.info("login locked on this station after repeated failures")
            return AuthResult(
                AuthOutcome.LOCKED,
                subject=subject,
                subject_user_id=named,
                retry_after=failure.locked_until - self._clock.now(),
                newly_locked=True,
            )
        return AuthResult(
            AuthOutcome.FAILED,
            subject=subject,
            subject_user_id=named,
            attempts_remaining=failure.attempts_remaining,
        )

    @staticmethod
    def _verify(pin_hash: str, pin: str) -> bool:
        try:
            return PasswordHasher().verify(pin_hash, pin)
        except VerifyMismatchError:
            return False
        except (InvalidHashError, VerificationError):
            # Message and hash stay out of the log.
            logger.warning("a roster PIN hash is malformed")
            return False

    def _dummy_hash(self, bundle: ConfigBundle) -> str:
        """A hash of a random secret with the cost of the roster's hashes."""
        params = self._roster_parameters(bundle)
        with self._dummy_lock:
            # `Parameters` is not hashable; key by its numbers.
            key = (
                params.version,
                params.salt_len,
                params.hash_len,
                params.time_cost,
                params.memory_cost,
                params.parallelism,
            )
            dummy = self._dummies.get(key)
            if dummy is None:
                dummy = PasswordHasher.from_parameters(params).hash(secrets.token_hex(16))
                self._dummies[key] = dummy
            return dummy

    def _roster_parameters(self, bundle: ConfigBundle) -> Parameters:
        for entry in bundle.roster:
            if entry.pin_hash is not None:
                try:
                    return extract_parameters(entry.pin_hash)
                except InvalidHashError:
                    continue
        with self._dummy_lock:
            if self._default_params is None:
                self._default_params = extract_parameters(self._hasher.hash(secrets.token_hex(16)))
            return self._default_params


def lock_minutes(retry_after: timedelta | None) -> int:
    """Whole minutes to show for a lock (rounded up, at least 1)."""
    if retry_after is None:
        return 1
    seconds = max(0, int(retry_after.total_seconds()))
    return max(1, -(-seconds // 60))
