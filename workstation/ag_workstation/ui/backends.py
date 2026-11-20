"""What the presenters need from the auth layer, and the `SessionManager` adapter."""

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


class SessionManagerBackend:
    """`SessionBackend` and `LoginBackend` over the station's `SessionManager`."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager

    def current(self) -> CurrentSession | None:
        """`SessionManager.current`."""
        return self._manager.current()

    def logout(self) -> bool:
        """`SessionManager.logout`."""
        return self._manager.logout()

    def login(self, employee_id: str, pin: str) -> LoginResult:
        """Login with the built-in employee ID + PIN method."""
        return self._manager.login(PIN_METHOD_NAME, PinCredentials(employee_id, pin))
