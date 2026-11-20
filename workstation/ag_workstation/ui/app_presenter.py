"""Screen switching and keystroke routing for the workstation window.

Every physical key press goes to the keyboard-wedge parser first, so a scan
is recorded whichever screen is up (CLAUDE.md rule 1). On the login screen
the key is also typed into the login form, unless it completed a barcode;
when it did, the scanner's characters are removed from the form again.

An unattributed scan opens the login screen (A1). A successful login, a
session started by a supervisor assignment, Back, or an idle login screen
return to the scan screen.
"""

from dataclasses import dataclass
from enum import StrEnum

from ag_workstation.scan import KeyboardWedgeParser
from ag_workstation.scan.messages import ScanMessageKey
from ag_workstation.ui.login_presenter import LoginPresenter, LoginViewModel
from ag_workstation.ui.scan_presenter import ScanPresenter, ScanViewModel


class Screen(StrEnum):
    """Which screen the window shows."""

    SCAN = "scan"
    LOGIN = "login"


@dataclass(frozen=True, slots=True)
class AppViewModel:
    """The whole window."""

    screen: Screen
    scan: ScanViewModel
    login: LoginViewModel


class AppPresenter:
    """Owns the scan and login presenters. UI thread only."""

    def __init__(
        self, *, scan: ScanPresenter, login: LoginPresenter, parser: KeyboardWedgeParser
    ) -> None:
        self.scan = scan
        self.login = login
        self._parser = parser
        self._parser.set_handler(None)  # barcodes are taken from `feed`'s return value
        self._screen = Screen.SCAN

    @property
    def screen(self) -> Screen:
        """The screen shown now."""
        return self._screen

    def key(self, text: str) -> None:
        """The text of one physical key press (may be empty for modifier keys)."""
        if not text:
            return
        codes = self._parser.feed(text)
        for code in codes:
            self.scan.on_barcode(code)
        if self._screen is Screen.LOGIN:
            if codes:
                self.login.discard_scanned_input()
            else:
                self.login.key(text)

    def show_login(self) -> None:
        """The login action on the scan screen."""
        self._switch(Screen.LOGIN)
        self.login.reset()

    def cancel_login(self) -> None:
        """Back from the login screen."""
        self.login.reset()
        self._switch(Screen.SCAN)

    def logout(self) -> None:
        """The logout action on the scan screen."""
        self.scan.logout()

    def _switch(self, screen: Screen) -> None:
        if screen is not self._screen:
            self._parser.reset()
            self._screen = screen

    def tick(self) -> AppViewModel:
        """Advance both presenters and switch screens as needed."""
        scan = self.scan.tick()
        login = self.login.tick()
        session = self.login.take_success()
        if session is not None:
            self.scan.show_welcome(session)
            self._switch(Screen.SCAN)
            scan = self.scan.tick()
        if self.scan.take_login_prompt() and not self.scan.logged_in:
            if self._screen is not Screen.LOGIN:
                self.login.reset(message_key=ScanMessageKey.SCAN_LOGIN_REQUIRED.value)
            self._switch(Screen.LOGIN)
            login = self.login.tick()
        elif self._screen is Screen.LOGIN and (self.scan.logged_in or self.login.idle()):
            self.login.reset()
            self._switch(Screen.SCAN)
            login = self.login.tick()
        return AppViewModel(self._screen, scan, login)
