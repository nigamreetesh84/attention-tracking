"""Scan screen and login screen (PLAN 2.8; scope 3.1, 4.1, 4.4; risks A1, A12, D7, Q3/Q4).

All UI logic is in pure-Python presenters that produce immutable view
models; they never import Qt and never block:

- `ScanPresenter`: status indicator, last item, message area, warning banner
  and sound, "monitoring in progress" label, "Monitoring unavailable" banner,
  live-view indicator, worker name, logout.
- `LoginPresenter`: employee ID + PIN with an on-screen keypad and the
  physical keyboard feeding the same model.
- `AppPresenter`: screen switching and keystroke routing (barcode wedge vs
  login typing).

Engine, scan flow and login calls run on `Runner`s, never on the UI thread.
`qt_views` (PySide6) renders the view models; `app:main` launches it.
"""

from ag_workstation.ui.app_presenter import AppPresenter, AppViewModel, Screen
from ag_workstation.ui.backends import LoginBackend, SessionBackend, SessionManagerBackend
from ag_workstation.ui.login_presenter import (
    KEYPAD_DIGITS,
    KEYPAD_LETTERS,
    LoginField,
    LoginPresenter,
    LoginPresenterConfig,
    LoginViewModel,
    MessageKind,
)
from ag_workstation.ui.runner import InlineRunner, Runner, ThreadRunner
from ag_workstation.ui.scan_presenter import (
    BannerKind,
    ScanHandler,
    ScanPresenter,
    ScanPresenterConfig,
    ScanViewModel,
    StatusLevel,
)
from ag_workstation.ui.status import StatusPoller, StatusReading, StatusSource

__all__ = [
    "KEYPAD_DIGITS",
    "KEYPAD_LETTERS",
    "AppPresenter",
    "AppViewModel",
    "BannerKind",
    "InlineRunner",
    "LoginBackend",
    "LoginField",
    "LoginPresenter",
    "LoginPresenterConfig",
    "LoginViewModel",
    "MessageKind",
    "Runner",
    "ScanHandler",
    "ScanPresenter",
    "ScanPresenterConfig",
    "ScanViewModel",
    "Screen",
    "SessionBackend",
    "SessionManagerBackend",
    "StatusLevel",
    "StatusPoller",
    "StatusReading",
    "StatusSource",
    "ThreadRunner",
]
