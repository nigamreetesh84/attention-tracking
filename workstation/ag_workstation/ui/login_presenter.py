"""Login screen logic (D7; risks Q3/Q4 defaults: on-screen keypad and keyboard).

Employee ID (1-16 letters or digits, shown upper case; matching is
case-insensitive) and PIN (4-6 digits, masked). The on-screen keypad and the
physical keyboard call the same methods, so both behave identically. The
login itself (argon2id, possibly hundreds of ms) runs on a runner; the result
is applied by `tick()`.

Failure messages are the auth layer's: one generic message for every kind of
failure, and the lockout message counts the remaining minutes down. The PIN
is never logged and is cleared after every attempt.
"""

import logging
import string
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

from ag_shared.i18n import TranslateFn, t

from ag_workstation.auth import (
    AuthOutcome,
    CurrentSession,
    LoginResult,
    lock_minutes,
    normalize_employee_id,
    valid_pin,
)
from ag_workstation.clock import Clock
from ag_workstation.ui.backends import LoginBackend
from ag_workstation.ui.runner import Runner

logger = logging.getLogger(__name__)

MAX_EMPLOYEE_ID: Final = 16
MAX_PIN: Final = 6
PIN_MASK_CHAR: Final = "•"
KEYPAD_DIGITS: Final = tuple("1234567890")
KEYPAD_LETTERS: Final = tuple(string.ascii_uppercase)

_ENTER: Final = frozenset({"\r", "\n"})
_TAB: Final = "\t"
_BACKSPACE: Final = frozenset({"\b", "\x7f"})
_ESCAPE: Final = "\x1b"


class LoginField(StrEnum):
    """The field that keypad and keyboard input goes to."""

    EMPLOYEE_ID = "employee_id"
    PIN = "pin"


class MessageKind(StrEnum):
    """How the login message is styled."""

    NONE = "none"
    INFO = "info"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LoginPresenterConfig:
    """Login screen settings."""

    idle_timeout_s: float = 60.0
    """Untouched for this long: the app returns to the scan screen."""


@dataclass(frozen=True, slots=True)
class LoginViewModel:
    """Everything the login screen shows."""

    employee_id: str
    pin_mask: str
    active_field: LoginField
    letters_mode: bool
    keypad_keys: tuple[str, ...]
    message_text: str
    message_kind: MessageKind
    busy: bool
    """A login is being checked; input is ignored."""
    locked: bool


class LoginPresenter:
    """Login screen presenter. Call its methods from the UI thread only."""

    def __init__(
        self,
        *,
        backend: LoginBackend,
        runner: Runner,
        clock: Clock,
        translate: TranslateFn = t,
        config: LoginPresenterConfig | None = None,
    ) -> None:
        self._backend = backend
        self._runner = runner
        self._clock = clock
        self._t = translate
        self._config = config or LoginPresenterConfig()
        self._inbox: deque[LoginResult] = deque()
        self._employee_id = ""
        self._pin = ""
        self._field = LoginField.EMPLOYEE_ID
        self._letters = False
        self._message_key: str | None = None
        self._message_kind = MessageKind.NONE
        self._busy = False
        self._lock_until: float | None = None
        self._locked_id: str | None = None
        self._success: CurrentSession | None = None
        self._last_input = clock.monotonic()

    # -- editing --------------------------------------------------------------

    def reset(self, *, message_key: str | None = None) -> None:
        """Empty both fields (screen shown or left); optionally show a message."""
        self._employee_id = ""
        self._pin = ""
        self._field = LoginField.EMPLOYEE_ID
        self._letters = False
        self._lock_until = None
        self._locked_id = None
        self._message_key = message_key
        self._message_kind = MessageKind.INFO if message_key else MessageKind.NONE
        self._touch()

    def select_field(self, field: LoginField) -> None:
        """Send further input to `field`."""
        self._touch()
        self._field = field

    def toggle_letters(self) -> None:
        """Switch the keypad between digits and letters (employee IDs may have letters)."""
        self._touch()
        self._letters = not self._letters

    def press(self, char: str) -> None:
        """One keypad or keyboard character for the active field."""
        self._touch()
        if self._busy or len(char) != 1 or not (char.isascii() and char.isalnum()):
            return
        if self._field is LoginField.EMPLOYEE_ID:
            if len(self._employee_id) < MAX_EMPLOYEE_ID:
                self._employee_id += char.upper()
                self._input_changed()
        elif char.isdigit() and len(self._pin) < MAX_PIN:
            self._pin += char
            self._input_changed()

    def backspace(self) -> None:
        """Delete the last character of the active field."""
        self._touch()
        if self._busy:
            return
        if self._field is LoginField.EMPLOYEE_ID:
            self._employee_id = self._employee_id[:-1]
        else:
            self._pin = self._pin[:-1]
        self._input_changed()

    def clear(self) -> None:
        """Empty the active field."""
        self._touch()
        if self._busy:
            return
        if self._field is LoginField.EMPLOYEE_ID:
            self._employee_id = ""
        else:
            self._pin = ""
        self._input_changed()

    def key(self, text: str) -> None:
        """Text of one physical key press: Enter submits, Tab switches field,
        Backspace deletes, Escape clears, anything else is typed."""
        for char in text:
            if char in _ENTER:
                self.submit()
            elif char == _TAB:
                self.select_field(
                    LoginField.PIN
                    if self._field is LoginField.EMPLOYEE_ID
                    else LoginField.EMPLOYEE_ID
                )
            elif char in _BACKSPACE:
                self.backspace()
            elif char == _ESCAPE:
                self.clear()
            else:
                self.press(char)

    def discard_scanned_input(self) -> None:
        """A barcode was scanned while this screen was up: its characters went
        into the active field too, so empty that field."""
        if not self._busy:
            self.clear()

    def _input_changed(self) -> None:
        if self._locked_id is not None and self._employee_id != self._locked_id:
            self._lock_until = None
            self._locked_id = None
        if self._lock_until is None and self._message_kind is MessageKind.ERROR:
            self._message_key = None
            self._message_kind = MessageKind.NONE

    def _touch(self) -> None:
        self._last_input = self._clock.monotonic()

    # -- submit -----------------------------------------------------------------

    def submit(self) -> None:
        """Validate locally, then check the login on the runner."""
        self._touch()
        if self._busy:
            return
        problem = self._validate()
        if problem is not None:
            key, field = problem
            self._error(key)
            self._field = field
            return
        employee_id, pin = self._employee_id, self._pin
        self._busy = True
        self._message_key, self._message_kind = "ui.login.checking", MessageKind.INFO
        try:
            self._runner.submit(lambda: self._login(employee_id, pin))
        except Exception as exc:
            logger.error("login not started: %s", type(exc).__name__)
            self._inbox.append(LoginResult(AuthOutcome.UNAVAILABLE))

    def _validate(self) -> tuple[str, LoginField] | None:
        if not self._employee_id:
            return "ui.login.id_required", LoginField.EMPLOYEE_ID
        if normalize_employee_id(self._employee_id) is None:
            return "ui.login.id_format", LoginField.EMPLOYEE_ID
        if not valid_pin(self._pin):
            return "ui.login.pin_format", LoginField.PIN
        return None

    def _login(self, employee_id: str, pin: str) -> None:
        try:
            result = self._backend.login(employee_id, pin)
        except Exception as exc:
            # Type name only: the message could echo the credentials.
            logger.error("login backend failed: %s", type(exc).__name__)
            result = LoginResult(AuthOutcome.UNAVAILABLE)
        self._inbox.append(result)

    def _error(self, key: str) -> None:
        self._message_key, self._message_kind = key, MessageKind.ERROR

    def _apply(self, result: LoginResult) -> None:
        self._busy = False
        self._pin = ""
        outcome = result.outcome
        if outcome is AuthOutcome.SUCCESS:
            if result.session is None:  # a broken backend; never a silent success
                self._error(AuthOutcome.UNAVAILABLE.message_key.value)
                return
            self.reset()
            self._success = result.session
            return
        self._field = LoginField.PIN if outcome is AuthOutcome.FAILED else self._field
        if outcome is AuthOutcome.LOCKED:
            retry = result.retry_after or timedelta(minutes=1)
            self._lock_until = self._clock.monotonic() + retry.total_seconds()
            self._locked_id = self._employee_id
            self._message_kind = MessageKind.ERROR
            return
        self._error(result.message_key.value)

    # -- tick ---------------------------------------------------------------------

    def take_success(self) -> CurrentSession | None:
        """The session of a login that just succeeded, once."""
        session, self._success = self._success, None
        return session

    def idle(self) -> bool:
        """Untouched for `idle_timeout_s` and not checking a login."""
        return (
            not self._busy
            and self._clock.monotonic() - self._last_input >= self._config.idle_timeout_s
        )

    def tick(self) -> LoginViewModel:
        """Apply finished logins and the lock countdown; return what to show."""
        while self._inbox:
            self._apply(self._inbox.popleft())
        locked = False
        text = ""
        if self._lock_until is not None:
            remaining = self._lock_until - self._clock.monotonic()
            if remaining > 0:
                locked = True
                text = self._t("login.locked", minutes=lock_minutes(timedelta(seconds=remaining)))
            else:
                self._lock_until = None
                self._locked_id = None
                self._message_key, self._message_kind = None, MessageKind.NONE
        if not locked and self._message_key is not None:
            text = self._t(self._message_key)
        return LoginViewModel(
            employee_id=self._employee_id,
            pin_mask=PIN_MASK_CHAR * len(self._pin),
            active_field=self._field,
            letters_mode=self._letters,
            keypad_keys=KEYPAD_LETTERS if self._letters else KEYPAD_DIGITS,
            message_text=text,
            message_kind=MessageKind.ERROR if locked else self._message_kind,
            busy=self._busy,
            locked=locked,
        )
