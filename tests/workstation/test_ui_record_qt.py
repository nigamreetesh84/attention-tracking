"""The "My record" screen (PLAN 2.10): pytest-qt smoke test on the offscreen platform
with the dev assembly: open from the scan screen, see own events only, dispute
once, a scan on this screen is recorded and returns to the scan screen."""

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", reason="Qt is not available")

from ag_shared.api import AttentionEventKind, AttentionEventPayload, EventType
from ag_shared.i18n import Translator
from ag_shared.ids import new_uuid7
from ag_shared.states import AttentionState
from ag_workstation.clock import FakeClock
from ag_workstation.queue import EventQueue
from ag_workstation.record import PersonalRecordStore, RecordMirror
from ag_workstation.ui import Screen
from ag_workstation.ui.app import DEV_EMPLOYEE_ID, DEV_PIN, Assembly, assemble
from ag_workstation.ui.qt_views import MainWindow
from ag_workstation.ui.record_presenter import DisputeReason
from argon2 import PasswordHasher
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
TR = Translator("en", strict=True)
OTHER_WORKER = UUID("0193a0b0-0000-7000-8000-00000000beef")


@pytest.fixture
def assembly(tmp_path: Path) -> Iterator[Assembly]:
    built = assemble(data_dir=tmp_path, dev=True, clock=FakeClock(), hasher=FAST_HASHER)
    yield built
    built.close()


def _owned(assembly: Assembly, kind: type) -> object:
    """The object behind one of the assembly's close callbacks (test access only)."""
    for close in assembly.closers:
        owner = getattr(close, "__self__", None)
        if isinstance(owner, kind):
            return owner
    raise AssertionError(f"no {kind.__name__} in the assembly")


def _event(user: UUID, session: UUID, state: AttentionState) -> AttentionEventPayload:
    clock_now = FakeClock().now()
    return AttentionEventPayload(
        kind=AttentionEventKind.STATE_EPISODE,
        state=state,
        started_at=clock_now,
        ended_at=clock_now + timedelta(seconds=20),
        session_id=session,
        user_id=user,
    )


@pytest.fixture
def window(qtbot: QtBot, assembly: Assembly) -> MainWindow:
    win = MainWindow(assembly.presenter, translate=TR.t, tick_ms=10)
    qtbot.addWidget(win)
    win.show()
    return win


def test_my_record_open_dispute_scan_and_back(
    qtbot: QtBot, window: MainWindow, assembly: Assembly
) -> None:
    presenter = assembly.presenter
    assert not window.scan_screen.record_button.isVisible()
    presenter.show_login()
    presenter.login.key(f"{DEV_EMPLOYEE_ID}\t{DEV_PIN}\r")
    qtbot.waitUntil(lambda: presenter.scan.logged_in, timeout=5000)
    qtbot.waitUntil(lambda: window.scan_screen.record_button.isVisible(), timeout=2000)
    assert window.scan_screen.record_button.text() == "My record"

    session = presenter.scan.session
    assert session is not None
    mirror = _owned(assembly, RecordMirror)
    assert isinstance(mirror, RecordMirror) and mirror.flush()
    store = _owned(assembly, PersonalRecordStore)
    assert isinstance(store, PersonalRecordStore)
    # The dev login was mirrored into the record by the assembly's RecordMirror;
    # add one event of mine and a whole shift of another worker.
    mine = new_uuid7()
    store.note_event(mine, _event(session.user_id, session.session_id, AttentionState.DROWSY))
    theirs_session = new_uuid7()
    store.note_session_start(theirs_session, OTHER_WORKER, FakeClock().now())
    store.note_event(new_uuid7(), _event(OTHER_WORKER, theirs_session, AttentionState.ABSENT))

    QTest.mouseClick(window.scan_screen.record_button, Qt.MouseButton.LeftButton)
    screen = window.record_screen
    qtbot.waitUntil(lambda: window.stack.currentWidget() is screen, timeout=2000)
    qtbot.waitUntil(lambda: screen.events.count() == 1, timeout=3000)
    assert screen.shifts.count() == 1
    assert screen.shifts.item(0).text().startswith("This shift")
    assert "Drowsy for 20 s" in screen.events.item(0).text()
    assert "Absent" not in screen.events.item(0).text()
    assert screen.title.text() == "My record"

    item = screen.events.item(0)
    screen.events.itemClicked.emit(item)
    qtbot.waitUntil(lambda: screen.dispute_button.isEnabled(), timeout=2000)
    QTest.mouseClick(screen.dispute_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: screen.dispute_panel.isVisible(), timeout=2000)
    assert not screen.submit.isEnabled()
    QTest.mouseClick(screen.reason_buttons[DisputeReason.WAS_ATTENTIVE], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: screen.submit.isEnabled(), timeout=2000)
    QTest.mouseClick(screen.submit, Qt.MouseButton.LeftButton)
    QTest.mouseClick(screen.submit, Qt.MouseButton.LeftButton)  # double tap
    qtbot.waitUntil(lambda: screen.message.text() == TR.t("ui.record.dispute.sent"), timeout=3000)
    qtbot.waitUntil(lambda: "disputed" in screen.events.item(0).text(), timeout=3000)
    assert not screen.dispute_button.isEnabled()

    queue = _owned(assembly, EventQueue)
    assert isinstance(queue, EventQueue)
    disputes = [r for r in queue.claim_batch() if r.type == EventType.DISPUTE]
    assert len(disputes) == 1
    assert disputes[0].payload["attention_event_id"] == str(mine)
    assert disputes[0].payload["message"] == "I was paying attention."
    queue.recover_sending()

    # A scan while on this screen is recorded and brings the scan screen back.
    QTest.keyClicks(window, "BOX-42")
    QTest.keyClick(window, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.scan_screen, timeout=3000)
    qtbot.waitUntil(lambda: "BOX-42" in window.scan_screen.last_item.text(), timeout=3000)

    # Back from the screen, and logout hides the action.
    QTest.mouseClick(window.scan_screen.record_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.stack.currentWidget() is screen, timeout=2000)
    QTest.mouseClick(screen.refresh_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(screen.back, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.scan_screen, timeout=2000)
    presenter.logout()
    qtbot.waitUntil(lambda: not window.scan_screen.record_button.isVisible(), timeout=3000)
    assert presenter.screen is Screen.SCAN


def test_dispute_panel_cancel_and_shift_click(
    qtbot: QtBot, window: MainWindow, assembly: Assembly
) -> None:
    presenter = assembly.presenter
    presenter.show_login()
    presenter.login.key(f"{DEV_EMPLOYEE_ID}\t{DEV_PIN}\r")
    qtbot.waitUntil(lambda: presenter.scan.logged_in, timeout=5000)
    session = presenter.scan.session
    assert session is not None
    mirror = _owned(assembly, RecordMirror)
    assert isinstance(mirror, RecordMirror) and mirror.flush()
    store = _owned(assembly, PersonalRecordStore)
    assert isinstance(store, PersonalRecordStore)
    store.note_event(
        new_uuid7(), _event(session.user_id, session.session_id, AttentionState.SLEEPING)
    )
    presenter.show_record()
    screen = window.record_screen
    qtbot.waitUntil(lambda: screen.events.count() == 1, timeout=3000)
    screen.shifts.itemClicked.emit(screen.shifts.item(0))
    screen.events.itemClicked.emit(screen.events.item(0))
    qtbot.waitUntil(lambda: screen.dispute_button.isEnabled(), timeout=2000)
    QTest.mouseClick(screen.dispute_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: screen.dispute_panel.isVisible(), timeout=2000)
    QTest.mouseClick(screen.cancel, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not screen.dispute_panel.isVisible(), timeout=2000)
    presenter.logout()  # session ends while on the screen: back to scan, nothing kept
    qtbot.waitUntil(lambda: window.stack.currentWidget() is window.scan_screen, timeout=3000)
    assert presenter.record is not None and presenter.record.session_id is None
