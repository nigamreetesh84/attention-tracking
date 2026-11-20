"""PySide6 views (PLAN 2.8): pytest-qt smoke tests on the offscreen platform,
and the app assembly with the fake engine."""

import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", reason="Qt is not available")

from ag_shared.i18n import Translator
from ag_shared.ids import new_uuid7
from ag_shared.states import AttentionState
from ag_workstation.clock import FakeClock
from ag_workstation.engine_api import EngineHealth, StationState
from ag_workstation.ui import Screen, StatusLevel
from ag_workstation.ui import app as app_module
from ag_workstation.ui.app import (
    DEV_EMPLOYEE_ID,
    DEV_PIN,
    Assembly,
    UnreachableEngine,
    _parse,
    assemble,
)
from ag_workstation.ui.qt_views import MainWindow, SoundPlayer
from argon2 import PasswordHasher
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
TR = Translator("en", strict=True)


@pytest.fixture
def assembly(tmp_path: Path) -> Iterator[Assembly]:
    # The parser runs on a clock that never moves, so qtbot's keystrokes always
    # count as a scanner burst however slow the CI machine is.
    built = assemble(data_dir=tmp_path, dev=True, clock=FakeClock(), hasher=FAST_HASHER)
    yield built
    built.close()


@pytest.fixture
def window(qtbot: QtBot, assembly: Assembly) -> MainWindow:
    win = MainWindow(assembly.presenter, translate=TR.t, tick_ms=10)
    qtbot.addWidget(win)
    win.show()
    return win


def test_barcode_keystrokes_update_last_item(qtbot: QtBot, window: MainWindow) -> None:
    assert window.stack.currentWidget() is window.scan_screen
    assert window.windowTitle() == "AttentionGuard"
    QTest.keyClicks(window, "ITEM-123")
    QTest.keyClick(window, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: "ITEM-123" in window.scan_screen.last_item.text(), timeout=3000)
    # Nobody is logged in: the scan is recorded unattributed and login is asked for (A1).
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.login_screen, timeout=3000)
    assert window.login_screen.message.text() == TR.t("scan.login_required")


def test_login_with_keypad_then_logout(qtbot: QtBot, window: MainWindow) -> None:
    QTest.mouseClick(window.scan_screen.login_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.login_screen, timeout=2000)
    keypad = window.login_screen.keypad
    QTest.mouseClick(keypad.toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: keypad.button_for("D") is not None, timeout=2000)
    for ch in "DEV":
        button = keypad.button_for(ch)
        assert button is not None
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(keypad.toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: keypad.button_for("0") is not None, timeout=2000)
    for ch in "001":
        button = keypad.button_for(ch)
        assert button is not None
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(window.login_screen.pin_field, Qt.MouseButton.LeftButton)
    for ch in DEV_PIN:
        button = keypad.button_for(ch)
        assert button is not None
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.login_screen.pin_field.text() == "•" * 4, timeout=2000)
    assert window.login_screen.id_field.text() == DEV_EMPLOYEE_ID
    QTest.mouseClick(keypad.enter, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.scan_screen, timeout=5000)
    qtbot.waitUntil(lambda: window.scan_screen.logout_button.isVisible(), timeout=2000)
    assert window.scan_screen.worker.text() == "Logged in: Dev Worker"
    qtbot.waitUntil(lambda: window.scan_screen.monitoring.isVisible(), timeout=3000)
    QTest.mouseClick(window.scan_screen.logout_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.scan_screen.login_button.isVisible(), timeout=3000)
    assert window.scan_screen.message.text() == "You are logged out."


def test_warned_scan_shows_banner_and_plays_sound(
    qtbot: QtBot, window: MainWindow, assembly: Assembly
) -> None:
    presenter = assembly.presenter
    presenter.show_login()
    presenter.login.key(f"{DEV_EMPLOYEE_ID}\t{DEV_PIN}\r")
    qtbot.waitUntil(lambda: presenter.scan.logged_in, timeout=5000)
    assert assembly.fake_engine is not None
    assembly.fake_engine.set_state(AttentionState.DISTRACTED)
    QTest.keyClicks(window, "BOX-9")
    QTest.keyClick(window, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.scan_screen.banner.isVisible(), timeout=3000)
    assert window.scan_screen.banner.text() == "Please stay attentive."
    assert window.sound.played == 1


def test_login_screen_keyboard_and_back(qtbot: QtBot, window: MainWindow) -> None:
    window.presenter.show_login()
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.login_screen, timeout=2000)
    window.presenter.login.key("ab")
    qtbot.waitUntil(lambda: window.login_screen.id_field.text() == "AB", timeout=2000)
    QTest.mouseClick(window.login_screen.cancel, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.scan_screen, timeout=2000)


def test_live_view_indicator_and_close(qtbot: QtBot, window: MainWindow) -> None:
    window.presenter.scan.set_live_view_active(True)
    qtbot.waitUntil(lambda: window.scan_screen.live_view.isVisible(), timeout=2000)
    assert window.scan_screen.live_view.text() == "Live view active"
    window.close()
    assert not window.timer.isActive()


def test_refresh_survives_a_presenter_error(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(window.presenter, "tick", broken)
    window.refresh()  # logged, not raised


class FakeEffect:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.plays = 0

    def play(self) -> None:
        self.plays += 1
        if self.fail:
            raise RuntimeError("no audio device")


def test_sound_player_beeps_without_an_asset(tmp_path: Path, qtbot: QtBot) -> None:
    player = SoundPlayer(tmp_path / "missing.wav")
    player.play()
    assert player.played == 1


def test_sound_player_uses_the_effect_and_never_raises(tmp_path: Path, qtbot: QtBot) -> None:
    wav = tmp_path / "alert.wav"
    wav.write_bytes(b"RIFF")
    effect = FakeEffect()
    player = SoundPlayer(wav, effect_factory=lambda _: effect)
    player.play()
    assert effect.plays == 1
    failing = SoundPlayer(wav, effect_factory=lambda _: FakeEffect(fail=True))
    failing.play()  # logged
    assert failing.played == 1

    def no_backend(_: Path) -> FakeEffect:
        raise ImportError("QtMultimedia")

    fallback = SoundPlayer(wav, effect_factory=no_backend)
    fallback.play()  # beeps
    assert fallback.played == 1


# -- assembly ---------------------------------------------------------------------------------


def test_assemble_without_service_is_fail_open(tmp_path: Path) -> None:
    built = assemble(data_dir=tmp_path, dev=False, clock=FakeClock(), hasher=FAST_HASHER)
    try:
        assert built.engine_mode == "unreachable"
        assert built.fake_engine is None
        presenter = built.presenter
        presenter.key("SCAN1\r")
        deadline = time.monotonic() + 3
        vm = presenter.tick()
        while vm.scan.scans_pending and time.monotonic() < deadline:
            time.sleep(0.01)
            vm = presenter.tick()
        assert vm.scan.last_item_text == "Last item: SCAN1"
        assert vm.screen is Screen.LOGIN
        # No roster without --dev: login is unavailable, not an error.
        presenter.login.key("EMP1\t1234\r")
        deadline = time.monotonic() + 3
        while presenter.tick().login.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert presenter.tick().login.message_text == TR.t("login.unavailable")
        assert presenter.tick().scan.status_level in (StatusLevel.NEUTRAL, StatusLevel.UNAVAILABLE)
    finally:
        built.close()
        built.close()  # idempotent


def test_unreachable_engine() -> None:
    engine = UnreachableEngine()
    with pytest.raises(Exception, match="no service"):
        engine.scan_state()
    with pytest.raises(Exception, match="no service"):
        engine.current_state()
    assert isinstance(engine.health(), EngineHealth)
    assert not engine.health().monitoring_available


def test_parse_args() -> None:
    args = _parse(["--dev", "--windowed", "--dev-state", "drowsy"])
    assert args.dev and args.windowed
    assert AttentionState(args.dev_state) is AttentionState.DROWSY


def test_main_launches_and_closes(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance()
    assert app is not None
    monkeypatch.setattr(app_module, "PasswordHasher", lambda: FAST_HASHER)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert app_module.main(["--dev", "--windowed", "--data-dir", str(tmp_path / "w")]) == 0
    assert app_module.main(["--dev", "--data-dir", str(tmp_path / "f")]) == 0
    for widget in QApplication.topLevelWidgets():
        widget.close()


def test_ipc_client_only_with_a_reachable_service(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.delenv(app_module.AUTHKEY_ENV, raising=False)
    assert app_module._ipc_client(clock) is None
    monkeypatch.setenv(app_module.AUTHKEY_ENV, "not-hex")
    assert app_module._ipc_client(clock) is None
    monkeypatch.setenv(app_module.AUTHKEY_ENV, "00" * 32)
    monkeypatch.setattr(
        app_module, "default_address", lambda: str(Path(tempfile.mkdtemp()) / "none.sock")
    )
    assert app_module._ipc_client(clock) is None  # nobody listening


def test_assemble_with_ipc_client(tmp_path: Path) -> None:
    class StubIpc:
        closed = False

        def scan_state(self) -> StationState:
            raise RuntimeError("down")

        def current_state(self) -> StationState:
            raise RuntimeError("down")

        def health(self) -> EngineHealth:
            return UnreachableEngine().health()

        def close(self) -> None:
            StubIpc.closed = True

    built = assemble(
        data_dir=tmp_path,
        dev=False,
        clock=FakeClock(),
        ipc=StubIpc(),  # type: ignore[arg-type]  # duck-typed IpcClient
        hasher=FAST_HASHER,
    )
    assert built.engine_mode == "ipc"
    built.close()
    assert StubIpc.closed


def test_dropped_evidence_and_accepting_config() -> None:
    from ag_shared.config import EngineConfig
    from ag_workstation.engine_api import BurstKind
    from ag_workstation.ui.app import AcceptingConfigTarget, DroppedEvidence

    DroppedEvidence().request_burst(new_uuid7(), BurstKind.SCAN_WARNING, 0.0)
    config = EngineConfig.model_construct(config_version=3)
    assert AcceptingConfigTarget().apply_config(config).accepted
