"""The "My record" screen logic (PLAN 2.10; scope 7; D8; security-privacy 1).

The logged-in worker sees their own sessions (shifts at this station) of the
last days with scan counters, the attention events of the selected shift, and
can dispute an event with a reason. No images are shown (data minimization);
an event only says how many photos were saved.

Scoping: the presenter asks the record source for the user id of the session
open *now*, and drops any answer for another user id, any row whose user id
is not that worker's, and everything it holds as soon as the session changes
or ends. The record source itself only returns that worker's rows
(`PersonalRecordStore.snapshot`).

Loading and dispute submission run on a `Runner`, never on the UI thread;
results come back through an inbox drained by `tick()`. Scans are not
handled here: the app presenter feeds every key to the barcode parser first
and returns to the scan screen when a barcode completes.
"""

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from ag_shared.api import AttentionEventKind, DisputeReasonCode
from ag_shared.i18n import TranslateFn, Translator, t

from ag_workstation.auth import CurrentSession
from ag_workstation.clock import Clock
from ag_workstation.record import DisputeOutcome, DisputeResult, RecordEvent, RecordSnapshot
from ag_workstation.record import SessionSummary as RecordSession
from ag_workstation.ui.backends import SessionBackend
from ag_workstation.ui.login_presenter import MessageKind
from ag_workstation.ui.runner import Runner
from ag_workstation.ui.scan_presenter import STATE_KEYS

logger = logging.getLogger(__name__)


class RecordSource(Protocol):
    """`PersonalRecordStore.snapshot`: one worker's rows only."""

    def snapshot(self, user_id: UUID) -> RecordSnapshot:
        """The worker's recent sessions and events, newest first. May be slow."""
        ...


class DisputeSubmitter(Protocol):
    """`DisputeService.submit`: never raises, queues at most once per event."""

    def submit(
        self,
        user_id: UUID,
        attention_event_id: UUID,
        message: str,
        reason: DisputeReasonCode | None = None,
    ) -> DisputeResult:
        """Dispute one of the worker's events."""
        ...


class DisputeReason(StrEnum):
    """Reasons offered on the touchscreen (no free text: there is no keyboard)."""

    WAS_ATTENTIVE = "was_attentive"
    CAMERA_VIEW = "camera_view"
    ON_BREAK = "on_break"
    OTHER_TASK = "other_task"

    @property
    def key(self) -> str:
        """Catalog key of the reason's text."""
        return f"ui.record.reason.{self.value}"

    @property
    def code(self) -> DisputeReasonCode:
        """The contract's `dispute.reason` code (same value)."""
        return DisputeReasonCode(self.value)


_OUTCOME_MESSAGES: Final = {
    DisputeOutcome.QUEUED: ("ui.record.dispute.sent", MessageKind.INFO),
    DisputeOutcome.SAVED: ("ui.record.dispute.saved", MessageKind.INFO),
    DisputeOutcome.ALREADY_DISPUTED: ("ui.record.dispute.already", MessageKind.INFO),
    DisputeOutcome.NOT_FOUND: ("ui.record.dispute.not_found", MessageKind.ERROR),
    DisputeOutcome.FAILED: ("ui.record.dispute.failed", MessageKind.ERROR),
}
_SENT: Final = frozenset({DisputeOutcome.QUEUED, DisputeOutcome.SAVED})

_english = Translator("en")


def english(key: str, **params: object) -> str:
    """The dispute message goes to reviewers in one language (English)."""
    return _english.t(key, **params)


@dataclass(frozen=True, slots=True)
class RecordPresenterConfig:
    """Screen settings."""

    idle_s: float = 60.0
    """Back to the scan screen after this long without a touch."""
    days_shown: int = 7
    """Window the scope note mentions (the store's retention)."""

    def __post_init__(self) -> None:
        if self.idle_s <= 0:
            raise ValueError("idle_s must be positive")


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One shift in the list."""

    session_id: UUID
    title_text: str
    counts_text: str
    selected: bool


@dataclass(frozen=True, slots=True)
class EventRow:
    """One event of the selected shift."""

    event_id: UUID
    text: str
    can_dispute: bool
    selected: bool


@dataclass(frozen=True, slots=True)
class ReasonRow:
    """One dispute reason button."""

    reason: DisputeReason
    text: str
    selected: bool


@dataclass(frozen=True, slots=True)
class RecordViewModel:
    """Everything the screen shows. Empty text means hidden."""

    worker_text: str
    scope_text: str
    message_text: str
    message_kind: MessageKind
    sessions: tuple[SessionRow, ...]
    events: tuple[EventRow, ...]
    empty_text: str
    """Shown instead of the lists (loading, nothing recorded, no events)."""
    can_dispute: bool
    """The selected event can be disputed (enables the Dispute button)."""
    dispute_open: bool
    reasons: tuple[ReasonRow, ...]
    can_submit: bool
    busy: bool


_Result = tuple[UUID, RecordSnapshot | None] | tuple[UUID, UUID, DisputeResult]


class RecordPresenter:
    """ "My record" presenter. UI thread only (except the runner jobs)."""

    def __init__(
        self,
        *,
        source: RecordSource,
        disputes: DisputeSubmitter,
        sessions: SessionBackend,
        runner: Runner,
        clock: Clock,
        translate: TranslateFn = t,
        message_translate: TranslateFn = english,
        timezone: Callable[[], tzinfo] = lambda: UTC,
        config: RecordPresenterConfig | None = None,
    ) -> None:
        """`timezone` gives the site timezone for display (times are UTC inside)."""
        self._source = source
        self._disputes = disputes
        self._sessions = sessions
        self._runner = runner
        self._clock = clock
        self._t = translate
        self._message_t = message_translate
        self._timezone = timezone
        self._config = config or RecordPresenterConfig()
        self._inbox: deque[_Result] = deque()
        self._lock = threading.Lock()
        self._clear()
        self._last_touch = clock.monotonic()

    def _clear(self) -> None:
        self._session: CurrentSession | None = None
        self._snapshot: RecordSnapshot | None = None
        self._loading = False
        self._busy = False
        self._selected_session: UUID | None = None
        self._selected_event: UUID | None = None
        self._dispute_open = False
        self._reason: DisputeReason | None = None
        self._message = ""
        self._message_kind = MessageKind.NONE

    # -- lifecycle ---------------------------------------------------------------

    @property
    def session_id(self) -> UUID | None:
        """The session the screen was opened for."""
        return self._session.session_id if self._session is not None else None

    def open(self, session: CurrentSession) -> None:
        """Show `session`'s worker their record (loads in the background)."""
        self._clear()
        self._inbox.clear()
        self._session = session
        self._selected_session = session.session_id
        self._touch()
        self._load()

    def close(self) -> None:
        """Forget everything shown (leaving the screen)."""
        self._clear()
        self._inbox.clear()

    def idle(self) -> bool:
        """No touch for `idle_s` and nothing in progress."""
        if self._busy:
            return False
        return self._clock.monotonic() - self._last_touch >= self._config.idle_s

    def _touch(self) -> None:
        self._last_touch = self._clock.monotonic()

    def _load(self) -> None:
        session = self._session
        if session is None:
            return
        user_id = session.user_id
        self._loading = True

        def job() -> None:
            try:
                snapshot: RecordSnapshot | None = self._source.snapshot(user_id)
            except Exception as exc:
                logger.error("my record not loaded (%s)", type(exc).__name__)
                snapshot = None
            self._inbox.append((user_id, snapshot))

        try:
            self._runner.submit(job)
        except Exception as exc:
            logger.error("my record load not started (%s)", type(exc).__name__)
            self._loading = False
            self._set_message("ui.record.load_failed", MessageKind.ERROR)

    # -- inputs ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload."""
        self._touch()
        if not self._loading:
            self._load()

    def select_session(self, session_id: UUID) -> None:
        """Show that shift's events."""
        self._touch()
        if self._snapshot is not None and any(
            s.session_id == session_id for s in self._snapshot.sessions
        ):
            self._selected_session = session_id
            self._selected_event = None
            self._dispute_open = False

    def select_event(self, event_id: UUID) -> None:
        """Select an event of the shown shift."""
        self._touch()
        if any(e.id == event_id for e in self._shown_events()):
            self._selected_event = event_id
            self._dispute_open = False
            self._reason = None

    def start_dispute(self) -> None:
        """Open the reason panel for the selected event."""
        self._touch()
        event = self._selected()
        if event is not None and event.dispute is None and not self._busy:
            self._dispute_open = True
            self._reason = None
            self._message, self._message_kind = "", MessageKind.NONE

    def choose_reason(self, reason: DisputeReason) -> None:
        """Pick a reason."""
        self._touch()
        if self._dispute_open:
            self._reason = reason

    def cancel_dispute(self) -> None:
        """Close the reason panel."""
        self._touch()
        if not self._busy:
            self._dispute_open = False
            self._reason = None

    def submit_dispute(self) -> None:
        """Send the dispute (once: further taps are ignored while busy)."""
        self._touch()
        event = self._selected()
        session = self._session
        reason = self._reason
        if (
            self._busy
            or not self._dispute_open
            or event is None
            or session is None
            or reason is None
        ):
            return
        user_id, event_id = session.user_id, event.id
        message = self._message_t(reason.key)
        code = reason.code
        self._busy = True

        def job() -> None:
            result = self._disputes.submit(user_id, event_id, message, code)
            self._inbox.append((user_id, event_id, result))

        try:
            self._runner.submit(job)
        except Exception as exc:
            logger.error("dispute not started (%s)", type(exc).__name__)
            self._busy = False
            self._set_message("ui.record.dispute.failed", MessageKind.ERROR)

    def _set_message(self, key: str, kind: MessageKind) -> None:
        self._message, self._message_kind = self._t(key), kind

    # -- tick ------------------------------------------------------------------------

    def tick(self) -> RecordViewModel:
        """Apply finished work; never blocks."""
        self._check_session()
        while self._inbox:
            self._apply(self._inbox.popleft())
        return self._view()

    def _check_session(self) -> None:
        if self._session is None:
            return
        try:
            current = self._sessions.current()
        except Exception as exc:
            logger.error("session lookup failed (%s)", type(exc).__name__)
            current = None
        if current is None or current.session_id != self._session.session_id:
            # Logged out, timed out or replaced: show nothing of the old worker.
            self.close()

    def _apply(self, item: _Result) -> None:
        session = self._session
        if session is None or item[0] != session.user_id:
            return  # for a worker no longer on the screen
        if len(item) == 2:
            snapshot = item[1]
            self._loading = False
            if snapshot is None:
                self._set_message("ui.record.load_failed", MessageKind.ERROR)
                return
            self._snapshot = _only(snapshot, session.user_id)
            ids = {s.session_id for s in self._snapshot.sessions}
            if self._selected_session not in ids:
                first = self._snapshot.sessions[0].session_id if self._snapshot.sessions else None
                self._selected_session = first
            if self._selected_event not in {e.id for e in self._shown_events()}:
                self._selected_event = None
            return
        _, event_id, result = item
        self._busy = False
        key, kind = _OUTCOME_MESSAGES[result.outcome]
        self._set_message(key, kind)
        if result.outcome in _SENT or result.outcome is DisputeOutcome.ALREADY_DISPUTED:
            self._dispute_open = False
            self._reason = None
            self._selected_event = event_id
            self._load()

    def _shown_events(self) -> tuple[RecordEvent, ...]:
        if self._snapshot is None:
            return ()
        return tuple(e for e in self._snapshot.events if e.session_id == self._selected_session)

    def _selected(self) -> RecordEvent | None:
        return next((e for e in self._shown_events() if e.id == self._selected_event), None)

    def _view(self) -> RecordViewModel:
        session = self._session
        snapshot = self._snapshot
        sessions = snapshot.sessions if snapshot is not None else ()
        events = self._shown_events()
        if session is None:
            empty = ""
        elif snapshot is None:
            empty = self._t("ui.record.loading") if self._loading else ""
        elif not sessions:
            empty = self._t("ui.record.empty", days=self._config.days_shown)
        elif not events:
            empty = self._t("ui.record.no_events")
        else:
            empty = ""
        selected = self._selected()
        return RecordViewModel(
            worker_text=(
                self._t("ui.worker", name=session.display_name) if session is not None else ""
            ),
            scope_text=self._t("ui.record.scope", days=self._config.days_shown),
            message_text=(self._t("ui.record.dispute.sending") if self._busy else self._message),
            message_kind=MessageKind.INFO if self._busy else self._message_kind,
            sessions=tuple(self._session_row(s, session) for s in sessions),
            events=tuple(
                EventRow(
                    event_id=e.id,
                    text=self._event_text(e),
                    can_dispute=e.dispute is None,
                    selected=e.id == self._selected_event,
                )
                for e in events
            ),
            empty_text=empty,
            can_dispute=selected is not None and selected.dispute is None and not self._busy,
            dispute_open=self._dispute_open,
            reasons=(
                tuple(ReasonRow(r, self._t(r.key), r is self._reason) for r in DisputeReason)
                if self._dispute_open
                else ()
            ),
            can_submit=self._dispute_open and self._reason is not None and not self._busy,
            busy=self._busy,
        )

    # -- formatting ---------------------------------------------------------------------

    def _local(self, value: datetime) -> datetime:
        try:
            zone = self._timezone()
        except Exception as exc:
            logger.warning("site timezone unavailable (%s); showing UTC", type(exc).__name__)
            zone = UTC
        return value.astimezone(zone)

    def _session_row(self, s: RecordSession, current: CurrentSession | None) -> SessionRow:
        start = self._local(s.started_at)
        date, start_time = start.strftime("%Y-%m-%d"), start.strftime("%H:%M")
        if current is not None and s.session_id == current.session_id:
            title = self._t("ui.record.session_current", time=start_time)
        elif s.ended_at is not None:
            end = self._local(s.ended_at).strftime("%H:%M")
            title = self._t("ui.record.session_past", date=date, start=start_time, end=end)
        else:
            title = self._t("ui.record.session_past_open", date=date, start=start_time)
        counts = self._t(
            "ui.record.counts",
            scans=s.scans,
            warned=s.warned,
            unmonitored=s.unmonitored,
            events=s.events,
        )
        return SessionRow(s.session_id, title, counts, s.session_id == self._selected_session)

    def _event_text(self, e: RecordEvent) -> str:
        when = self._local(e.started_at).strftime("%H:%M:%S")
        state = self._t(STATE_KEYS[e.state])
        if e.kind is AttentionEventKind.SCAN_WARNING:
            what = self._t("ui.record.event.scan_warning", time=when, state=state)
        elif e.ended_at is not None:
            seconds = max(0, round((e.ended_at - e.started_at).total_seconds()))
            duration = (
                self._t("ui.record.duration.minutes", minutes=seconds // 60, seconds=seconds % 60)
                if seconds >= 60
                else self._t("ui.record.duration.seconds", seconds=seconds)
            )
            what = self._t("ui.record.event.episode", time=when, state=state, duration=duration)
        else:
            what = self._t("ui.record.event.episode_open", time=when, state=state)
        notes = []
        if e.low_confidence:
            notes.append(self._t("ui.record.low_confidence"))
        if e.images_saved:
            notes.append(self._t("ui.record.photos", count=e.images_saved))
        if e.dispute is not None:
            notes.append(self._t("ui.record.disputed"))
        return self._t("ui.record.event_line", what=what, notes=", ".join(notes)) if notes else what


def _only(snapshot: RecordSnapshot, user_id: UUID) -> RecordSnapshot:
    """Defence in depth: keep only `user_id`'s rows whatever the source returned."""
    if snapshot.user_id != user_id:
        return RecordSnapshot(user_id, (), ())
    sessions = tuple(s for s in snapshot.sessions if s.user_id == user_id)
    ids = {s.session_id for s in sessions}
    events = tuple(e for e in snapshot.events if e.user_id == user_id and e.session_id in ids)
    return RecordSnapshot(user_id, sessions, events, snapshot.unreadable)
