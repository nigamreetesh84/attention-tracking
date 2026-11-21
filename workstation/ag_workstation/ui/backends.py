"""What the presenters need from the auth layer, and the `SessionManager` adapter."""

import logging
from collections.abc import Callable
from typing import Protocol

from ag_workstation.auth import PIN_METHOD_NAME, CurrentSession, LoginResult, PinCredentials
from ag_workstation.auth.sessions import SessionManager


class SessionBackend(Protocol):
    """The open session and logout."""

    def current(self) -> CurrentSession | None:
        """The open session, or None. Must be fast (in memory)."""
        ...

    def logout(self) -> bool:
        """End the open session; False if none was open."""
        ...


class LoginBackend(Protocol):
    """Employee ID + PIN login (D7)."""

    def login(self, employee_id: str, pin: str) -> LoginResult:
        """Authenticate and start a session. May be slow (argon2id); never on the UI thread."""
        ...


logger = logging.getLogger(__name__)


class SessionManagerBackend:
    """`SessionBackend` and `LoginBackend` over the station's `SessionManager`.

    `on_change` runs after every login attempt and logout (for example to
    push the session to the engine child at once); it must not block and its
    errors are logged, never raised into the login."""

    def __init__(
        self, manager: SessionManager, *, on_change: Callable[[], None] | None = None
    ) -> None:
        self._manager = manager
        self._on_change = on_change

    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception as exc:
            logger.error("session change hook failed (%s)", type(exc).__name__)

    def current(self) -> CurrentSession | None:
        """`SessionManager.current`."""
        return self._manager.current()

    def logout(self) -> bool:
        """`SessionManager.logout`."""
        ended = self._manager.logout()
        self._changed()
        return ended

    def login(self, employee_id: str, pin: str) -> LoginResult:
        """Login with the built-in employee ID + PIN method."""
        result = self._manager.login(PIN_METHOD_NAME, PinCredentials(employee_id, pin))
        self._changed()
        return result
