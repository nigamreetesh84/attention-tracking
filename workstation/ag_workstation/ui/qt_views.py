"""PySide6 views for the scan, login and "My record" screens (the only Qt module besides `app`).

The views hold no logic: a `QTimer` calls `AppPresenter.tick()` and the view
renders the returned view model. Key presses anywhere in the window are
caught by an application event filter and handed to `AppPresenter.key`,
which feeds the keyboard-wedge parser (scans) and the login form. Text comes
only from the view model or the translator (CLAUDE.md rule 13).
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

from ag_shared.i18n import TranslateFn, t
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ag_workstation.ui.app_presenter import AppPresenter, AppViewModel, Screen
from ag_workstation.ui.login_presenter import LoginField, LoginViewModel, MessageKind
from ag_workstation.ui.record_presenter import DisputeReason, RecordViewModel
from ag_workstation.ui.scan_presenter import BannerKind, ScanViewModel, StatusLevel

logger = logging.getLogger(__name__)

TICK_MS: Final = 100

_STATUS_COLOURS: Final = {
    StatusLevel.ATTENTIVE: "#1e8e3e",
    StatusLevel.CAUTION: "#f29900",
    StatusLevel.ALERT: "#d93025",
    StatusLevel.NEUTRAL: "#80868b",
    StatusLevel.UNAVAILABLE: "#5f6368",
}
_BANNER_COLOURS: Final = {
    BannerKind.NONE: "transparent",
    BannerKind.WARNING: "#f29900",
    BannerKind.ESCALATED: "#d93025",
}
_MESSAGE_COLOURS: Final = {
    MessageKind.NONE: "#202124",
    MessageKind.INFO: "#1a73e8",
    MessageKind.ERROR: "#d93025",
}


def _no_focus(button: QPushButton) -> QPushButton:
    # Keyboard focus stays on the window so every key reaches the event filter.
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return button


def _set_text(label: QLabel | QPushButton, text: str) -> None:
    """Set text and hide the widget when there is none."""
    label.setText(text)
    label.setVisible(bool(text))


class SoundEffect(Protocol):
    """The part of `QSoundEffect` the player uses."""

    def play(self) -> None:
        """Start playing."""
        ...


def qt_sound_effect(asset: Path) -> SoundEffect:
    """A `QSoundEffect` for `asset` (QtMultimedia is imported only when a WAV is configured)."""
    from PySide6.QtMultimedia import QSoundEffect

    effect = QSoundEffect()
    effect.setSource(QUrl.fromLocalFile(str(asset)))
    return effect


class SoundPlayer:
    """The warning sound (A12): a WAV via QSoundEffect if one is configured, else
    the system beep. No sound asset ships with release 1."""

    def __init__(
        self,
        asset: Path | None = None,
        *,
        effect_factory: Callable[[Path], SoundEffect] = qt_sound_effect,
    ) -> None:
        self._effect: SoundEffect | None = None
        if asset is not None and asset.is_file():
            try:
                self._effect = effect_factory(asset)
            except Exception as exc:  # QtMultimedia missing or no audio backend
                logger.warning("warning sound unavailable (%s); using beep", type(exc).__name__)
        self.played = 0

    def play(self) -> None:
        """Play once; never raises."""
        self.played += 1
        try:
            if self._effect is not None:
                self._effect.play()
            else:
                QApplication.beep()
        except Exception as exc:
            logger.warning("warning sound failed: %s", type(exc).__name__)


class KeypadWidget(QWidget):
    """On-screen keypad: characters, letters/digits toggle, delete, clear, enter."""

    def __init__(self, presenter: AppPresenter, translate: TranslateFn) -> None:
        super().__init__()
        self._presenter = presenter
        self._t = translate
        self._keys: tuple[str, ...] = ()
        self._grid = QGridLayout(self)
        self._buttons: list[QPushButton] = []
        self.toggle = _no_focus(QPushButton())
        self.backspace = _no_focus(QPushButton(translate("ui.keypad.backspace")))
        self.clear = _no_focus(QPushButton(translate("ui.keypad.clear")))
        self.enter = _no_focus(QPushButton(translate("ui.keypad.enter")))
        self.toggle.clicked.connect(lambda: self._presenter.login.toggle_letters())
        self.backspace.clicked.connect(lambda: self._presenter.login.backspace())
        self.clear.clicked.connect(lambda: self._presenter.login.clear())
        self.enter.clicked.connect(lambda: self._presenter.login.submit())

    def show_model(self, vm: LoginViewModel) -> None:
        """Rebuild the character buttons when the key set changes."""
        self.toggle.setText(self._t("ui.keypad.digits" if vm.letters_mode else "ui.keypad.letters"))
        for button in (self.toggle, self.backspace, self.clear, self.enter):
            button.setEnabled(not vm.busy)
        if vm.keypad_keys == self._keys:
            for button in self._buttons:
                button.setEnabled(not vm.busy)
            return
        self._keys = vm.keypad_keys
        for button in self._buttons:
            self._grid.removeWidget(button)
            button.deleteLater()
        self._buttons = []
        columns = 7 if len(vm.keypad_keys) > 10 else 3
        for index, char in enumerate(vm.keypad_keys):
            button = _no_focus(QPushButton(char))
            button.setMinimumSize(64, 64)
            button.clicked.connect(self._press(char))
            button.setEnabled(not vm.busy)
            self._grid.addWidget(button, index // columns, index % columns)
            self._buttons.append(button)
        row = (len(vm.keypad_keys) - 1) // columns + 1
        for col, button in enumerate((self.toggle, self.backspace, self.clear, self.enter)):
            self._grid.addWidget(button, row + col // columns, col % columns)

    def _press(self, char: str) -> Callable[[], None]:
        return lambda: self._presenter.login.press(char)

    def button_for(self, char: str) -> QPushButton | None:
        """The button typing `char` (tests)."""
        for button in self._buttons:
            if button.text() == char:
                return button
        return None


class ScanScreen(QWidget):
    """Status indicator, last item, message area, banners, worker and logout."""

    def __init__(self, presenter: AppPresenter, translate: TranslateFn) -> None:
        super().__init__()
        self.live_view = QLabel()
        self.unavailable = QLabel()
        self.status = QLabel()
        self.banner = QLabel()
        self.last_item = QLabel()
        self.message = QLabel()
        self.monitoring = QLabel()
        self.worker = QLabel()
        self.login_button = _no_focus(QPushButton(translate("ui.login_button")))
        self.logout_button = _no_focus(QPushButton(translate("ui.logout_button")))
        self.record_button = _no_focus(QPushButton(translate("ui.record_button")))
        self.record_button.setVisible(False)
        self.login_button.clicked.connect(presenter.show_login)
        self.record_button.clicked.connect(presenter.show_record)
        self.logout_button.clicked.connect(presenter.logout)
        for label in (self.status, self.banner, self.unavailable, self.message, self.last_item):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
        self.status.setMinimumHeight(160)
        self.status.setStyleSheet("font-size: 48px; font-weight: bold; color: white;")
        self.live_view.setStyleSheet("background: #1a73e8; color: white; padding: 6px;")
        self.unavailable.setStyleSheet(
            "background: #5f6368; color: white; font-size: 24px; padding: 8px;"
        )
        self.last_item.setStyleSheet("font-size: 28px;")
        self.message.setStyleSheet("font-size: 24px;")

        top = QHBoxLayout()
        top.addWidget(self.worker)
        top.addStretch(1)
        top.addWidget(self.monitoring)
        top.addWidget(self.live_view)
        top.addWidget(self.record_button)
        top.addWidget(self.login_button)
        top.addWidget(self.logout_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.unavailable)
        layout.addWidget(self.status, 2)
        layout.addWidget(self.banner)
        layout.addWidget(self.last_item, 1)
        layout.addWidget(self.message, 1)

    def show_model(self, vm: ScanViewModel) -> None:
        """Show `vm`."""
        self.status.setText(vm.status_text)
        self.status.setStyleSheet(
            f"background: {_STATUS_COLOURS[vm.status_level]}; color: white;"
            " font-size: 48px; font-weight: bold;"
        )
        self.banner.setStyleSheet(
            f"background: {_BANNER_COLOURS[vm.banner_kind]}; color: white;"
            " font-size: 32px; padding: 12px;"
        )
        _set_text(self.banner, vm.banner_text)
        _set_text(self.unavailable, vm.unavailable_text)
        _set_text(self.live_view, vm.live_view_text)
        _set_text(self.monitoring, vm.monitoring_text)
        self.last_item.setText(vm.last_item_text)
        self.message.setText(vm.message_text)
        self.worker.setText(vm.worker_text)
        self.login_button.setVisible(not vm.logged_in)
        self.logout_button.setVisible(vm.logged_in)

    def show_record_action(self, available: bool) -> None:
        """Show "My record" for a logged-in worker when the station keeps a record."""
        self.record_button.setVisible(available)


class LoginScreen(QWidget):
    """Employee ID and masked PIN with the on-screen keypad."""

    def __init__(self, presenter: AppPresenter, translate: TranslateFn) -> None:
        super().__init__()
        self.title = QLabel(translate("ui.login.title"))
        self.title.setStyleSheet("font-size: 32px; font-weight: bold;")
        self.id_label = QLabel(translate("ui.login.employee_id"))
        self.pin_label = QLabel(translate("ui.login.pin"))
        # Buttons, not line edits: they select the field, and typing goes
        # through the presenter so keypad and keyboard share one model.
        self.id_field = _no_focus(QPushButton())
        self.pin_field = _no_focus(QPushButton())
        for field in (self.id_field, self.pin_field):
            field.setCheckable(True)
            field.setMinimumHeight(56)
            field.setStyleSheet("font-size: 28px; text-align: left; padding: 6px;")
        self.id_field.clicked.connect(lambda: presenter.login.select_field(LoginField.EMPLOYEE_ID))
        self.pin_field.clicked.connect(lambda: presenter.login.select_field(LoginField.PIN))
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.keypad = KeypadWidget(presenter, translate)
        self.cancel = _no_focus(QPushButton(translate("ui.login.cancel")))
        self.cancel.clicked.connect(presenter.cancel_login)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.id_label)
        layout.addWidget(self.id_field)
        layout.addWidget(self.pin_label)
        layout.addWidget(self.pin_field)
        layout.addWidget(self.message)
        layout.addWidget(self.keypad, 1)
        layout.addWidget(self.cancel)

    def show_model(self, vm: LoginViewModel) -> None:
        """Show `vm`."""
        self.id_field.setText(vm.employee_id)
        self.pin_field.setText(vm.pin_mask)
        self.id_field.setChecked(vm.active_field is LoginField.EMPLOYEE_ID)
        self.pin_field.setChecked(vm.active_field is LoginField.PIN)
        self.message.setStyleSheet(f"font-size: 22px; color: {_MESSAGE_COLOURS[vm.message_kind]};")
        self.message.setText(vm.message_text)
        self.cancel.setEnabled(not vm.busy)
        self.keypad.show_model(vm)


_ID_ROLE: Final = Qt.ItemDataRole.UserRole


class RecordScreen(QWidget):
    """The worker's shifts, the selected shift's events and the dispute panel.

    Lists are rebuilt only when their rows change, with signals blocked, so a
    rebuild never feeds a selection back into the presenter."""

    def __init__(self, presenter: AppPresenter, translate: TranslateFn) -> None:
        super().__init__()
        self._presenter = presenter
        self.title = QLabel(translate("ui.record.title"))
        self.title.setStyleSheet("font-size: 32px; font-weight: bold;")
        self.worker = QLabel()
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.empty = QLabel()
        self.empty.setStyleSheet("font-size: 22px;")
        self.shifts_label = QLabel(translate("ui.record.shifts"))
        self.events_label = QLabel(translate("ui.record.events"))
        self.shifts = QListWidget()
        self.events = QListWidget()
        for widget in (self.shifts, self.events):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            widget.setStyleSheet("font-size: 20px;")
            widget.setWordWrap(True)
        self.shifts.itemClicked.connect(self._shift_clicked)
        self.events.itemClicked.connect(self._event_clicked)
        self.dispute_button = _no_focus(QPushButton(translate("ui.record.dispute_button")))
        self.refresh_button = _no_focus(QPushButton(translate("ui.record.refresh")))
        self.back = _no_focus(QPushButton(translate("ui.record.back")))
        self.dispute_button.clicked.connect(lambda: self._record_call("start_dispute"))
        self.refresh_button.clicked.connect(lambda: self._record_call("refresh"))
        self.back.clicked.connect(presenter.close_record)

        self.dispute_panel = QWidget()
        self.dispute_title = QLabel(translate("ui.record.dispute.title"))
        self.dispute_title.setStyleSheet("font-size: 24px; font-weight: bold;")
        self.reason_buttons: dict[DisputeReason, QPushButton] = {}
        panel = QVBoxLayout(self.dispute_panel)
        panel.addWidget(self.dispute_title)
        for reason in DisputeReason:
            button = _no_focus(QPushButton(translate(reason.key)))
            button.setCheckable(True)
            button.setMinimumHeight(56)
            button.clicked.connect(self._reason(reason))
            self.reason_buttons[reason] = button
            panel.addWidget(button)
        self.submit = _no_focus(QPushButton(translate("ui.record.dispute.submit")))
        self.cancel = _no_focus(QPushButton(translate("ui.record.dispute.cancel")))
        self.submit.clicked.connect(lambda: self._record_call("submit_dispute"))
        self.cancel.clicked.connect(lambda: self._record_call("cancel_dispute"))
        actions = QHBoxLayout()
        actions.addWidget(self.cancel)
        actions.addWidget(self.submit)
        panel.addLayout(actions)
        self.dispute_panel.setVisible(False)

        top = QHBoxLayout()
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.worker)
        lists = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self.shifts_label)
        left.addWidget(self.shifts, 1)
        right = QVBoxLayout()
        right.addWidget(self.events_label)
        right.addWidget(self.events, 1)
        right.addWidget(self.dispute_button)
        lists.addLayout(left, 1)
        lists.addLayout(right, 2)
        bottom = QHBoxLayout()
        bottom.addWidget(self.back)
        bottom.addStretch(1)
        bottom.addWidget(self.refresh_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.scope)
        layout.addWidget(self.message)
        layout.addWidget(self.empty)
        layout.addLayout(lists, 1)
        layout.addWidget(self.dispute_panel)
        layout.addLayout(bottom)
        self._shift_rows: tuple[tuple[str, str], ...] = ()
        self._event_rows: tuple[str, ...] = ()

    def _record_call(self, name: str) -> None:
        record = self._presenter.record
        if record is not None:
            getattr(record, name)()

    def _reason(self, reason: DisputeReason) -> Callable[[], None]:
        def choose() -> None:
            if self._presenter.record is not None:
                self._presenter.record.choose_reason(reason)

        return choose

    def _shift_clicked(self, item: QListWidgetItem) -> None:
        if self._presenter.record is not None:
            self._presenter.record.select_session(item.data(_ID_ROLE))

    def _event_clicked(self, item: QListWidgetItem) -> None:
        if self._presenter.record is not None:
            self._presenter.record.select_event(item.data(_ID_ROLE))

    def show_model(self, vm: RecordViewModel | None) -> None:
        """Show `vm` (None: nothing)."""
        if vm is None:
            return
        self.worker.setText(vm.worker_text)
        self.scope.setText(vm.scope_text)
        self.message.setStyleSheet(f"font-size: 22px; color: {_MESSAGE_COLOURS[vm.message_kind]};")
        _set_text(self.message, vm.message_text)
        _set_text(self.empty, vm.empty_text)
        shift_rows = tuple((row.title_text, row.counts_text) for row in vm.sessions)
        self.shifts.blockSignals(True)
        if shift_rows != self._shift_rows:
            self._shift_rows = shift_rows
            self.shifts.clear()
            for row in vm.sessions:
                item = QListWidgetItem(f"{row.title_text}\n{row.counts_text}")
                item.setData(_ID_ROLE, row.session_id)
                self.shifts.addItem(item)
        for index, row in enumerate(vm.sessions):
            self.shifts.item(index).setSelected(row.selected)
        self.shifts.blockSignals(False)
        event_rows = tuple(row.text for row in vm.events)
        self.events.blockSignals(True)
        if event_rows != self._event_rows:
            self._event_rows = event_rows
            self.events.clear()
            for event in vm.events:
                item = QListWidgetItem(event.text)
                item.setData(_ID_ROLE, event.event_id)
                self.events.addItem(item)
        for index, event in enumerate(vm.events):
            self.events.item(index).setSelected(event.selected)
        self.events.blockSignals(False)
        self.dispute_button.setEnabled(vm.can_dispute and not vm.dispute_open)
        self.dispute_panel.setVisible(vm.dispute_open)
        chosen = {row.reason for row in vm.reasons if row.selected}
        for reason, button in self.reason_buttons.items():
            button.setChecked(reason in chosen)
            button.setEnabled(not vm.busy)
        self.submit.setEnabled(vm.can_submit)
        self.cancel.setEnabled(not vm.busy)
        self.back.setEnabled(not vm.busy)


class MainWindow(QMainWindow):
    """The full-screen workstation window: scan, login and "My record" screens."""

    def __init__(
        self,
        presenter: AppPresenter,
        *,
        translate: TranslateFn = t,
        sound: SoundPlayer | None = None,
        tick_ms: int = TICK_MS,
    ) -> None:
        super().__init__()
        self.presenter = presenter
        self.sound = sound or SoundPlayer()
        self.setWindowTitle(translate("ui.window_title"))
        self.scan_screen = ScanScreen(presenter, translate)
        self.login_screen = LoginScreen(presenter, translate)
        self.record_screen = RecordScreen(presenter, translate)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.scan_screen)
        self.stack.addWidget(self.login_screen)
        self.stack.addWidget(self.record_screen)
        self.setCentralWidget(self.stack)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._last: AppViewModel | None = None
        self._sound_token = 0
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(tick_ms)
        self.refresh()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802  (Qt API)
        """Route key presses in this window to the presenter (scanner and login typing)."""
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(watched, QWidget)
            and watched.window() is self
            and isinstance(event, QKeyEvent)
        ):
            self.presenter.key(event.text())
            return True
        return super().eventFilter(watched, event)

    def refresh(self) -> None:
        """One presenter tick and render; errors are logged, never raised into Qt."""
        try:
            vm = self.presenter.tick()
            if vm != self._last:
                self.show_model(vm)
                self._last = vm
        except Exception as exc:
            logger.exception("scan screen refresh failed (%s)", type(exc).__name__)

    def show_model(self, vm: AppViewModel) -> None:
        """Show `vm`, and play the warning sound for each new warned scan."""
        self.scan_screen.show_model(vm.scan)
        self.scan_screen.show_record_action(vm.record_available)
        self.login_screen.show_model(vm.login)
        self.record_screen.show_model(vm.record)
        target: QWidget = {
            Screen.SCAN: self.scan_screen,
            Screen.LOGIN: self.login_screen,
            Screen.RECORD: self.record_screen,
        }[vm.screen]
        if self.stack.currentWidget() is not target:
            self.stack.setCurrentWidget(target)
        if vm.scan.sound_token != self._sound_token:
            self._sound_token = vm.scan.sound_token
            self.sound.play()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802  (Qt API)
        """Stop the timer and the event filter."""
        self.timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(event)
