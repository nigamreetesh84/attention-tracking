"""The `LoginMethod` plug-in interface and its registry (D7, security-privacy 3).

A login method turns credentials into a roster entry. It knows nothing about
sessions, the queue or the engine; `SessionManager` does the rest. Employee ID
+ PIN is the only built-in method. Another method (for example face match,
which would need its own consent) is added by implementing `LoginMethod` and
registering it; nothing in the session code changes.
"""

import threading
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from ag_shared.api import RosterEntry, SessionMethod

from ag_workstation.auth.messages import MessageKey


class LoginRequest:
    """Base class of the credentials a method accepts. Subclasses must keep
    secrets out of `repr` (they may end up in a log line)."""


class AuthOutcome(StrEnum):
    """Result of one authentication attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    """Generic failure: unknown ID, wrong secret or inactive worker look the same."""
    LOCKED = "locked"
    INVALID_INPUT = "invalid_input"
    """The input does not have the required format; no attempt was counted."""
    UNAVAILABLE = "unavailable"
    """No roster yet, unknown method or an internal error."""

    @property
    def message_key(self) -> MessageKey:
        """The UI message for this outcome."""
        return _MESSAGES[self]


_MESSAGES = {
    AuthOutcome.SUCCESS: MessageKey.LOGIN_OK,
    AuthOutcome.FAILED: MessageKey.LOGIN_FAILED,
    AuthOutcome.LOCKED: MessageKey.LOGIN_LOCKED,
    AuthOutcome.INVALID_INPUT: MessageKey.LOGIN_INVALID_INPUT,
    AuthOutcome.UNAVAILABLE: MessageKey.LOGIN_UNAVAILABLE,
}


@dataclass(frozen=True, slots=True)
class AuthResult:
    """What a `LoginMethod` decided."""

    outcome: AuthOutcome
    user: RosterEntry | None = None
    """Set only on success."""
    subject: str | None = None
    """The normalized identifier the attempt was for (e.g. employee ID), for audit."""
    retry_after: timedelta | None = None
    """Set when locked."""
    attempts_remaining: int | None = None
    """Set on a counted failure that did not lock."""
    newly_locked: bool = False
    """This attempt caused the lockout."""

    def __post_init__(self) -> None:
        if (self.outcome is AuthOutcome.SUCCESS) != (self.user is not None):
            raise ValueError("a user is set exactly on success")


class LoginMethod(Protocol):
    """A way for a worker to log in at the station."""

    @property
    def name(self) -> str:
        """Registry key, e.g. `employee_id_pin`."""
        ...

    @property
    def session_method(self) -> SessionMethod:
        """The `worker_session.method` recorded for sessions it starts."""
        ...

    def authenticate(self, request: LoginRequest) -> AuthResult:
        """Check `request` offline against the cached roster.

        Wrong request type or format gives `INVALID_INPUT`. Should not raise;
        the caller still treats an exception as `UNAVAILABLE`.
        """
        ...


class LoginMethodRegistry:
    """The login methods enabled on this station. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._methods: dict[str, LoginMethod] = {}

    def register(self, method: LoginMethod) -> None:
        """Add `method`; raises `ValueError` if its name is taken."""
        with self._lock:
            if method.name in self._methods:
                raise ValueError(f"login method {method.name!r} is already registered")
            self._methods[method.name] = method

    def get(self, name: str) -> LoginMethod | None:
        """The method registered as `name`, if any."""
        with self._lock:
            return self._methods.get(name)

    def names(self) -> tuple[str, ...]:
        """Registered method names, in registration order."""
        with self._lock:
            return tuple(self._methods)
