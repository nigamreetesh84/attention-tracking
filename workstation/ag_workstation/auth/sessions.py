"""Worker sessions on this station (PLAN 2.4, `api-contract.md` 4.1-4.3).

One session at most is open per station. A new login or supervisor
assignment replaces the open one (ended with reason `replaced`). Every start
and end is queued as a `session_start` / `session_end` item whose ids are
UUIDv7 and whose times come from the `utc` clock (server-corrected UTC,
contract 6); the `session_start` item id is the session id.

Time bases (2.11 fix): `CurrentSession.started_at` and the persisted open
session are **device time** (the raw `clock`), because the session start is
pushed to the engine child's capture gate, which compares it with the
burst's frames in device time; a change of the server offset then can never
open the gate for frames from before the session. Records and comparisons
with server-issued times (`valid_until`) use corrected UTC.

PPE override: when a session starts, the worker's roster
`ppe_profile_override` selects the engine profile and thresholds
(`ConfigBundle.engine_config`); when it ends, the station profile returns.
Applying is fail-open: an engine that refuses or fails never blocks a login;
the failure is logged and raised as a health flag.

Commands (`assign_worker`, `end_session`) are idempotent by command id, and
an `assign_worker` also by assignment id, persisted so a restart does not
start an assignment twice. Every outcome is final: the caller acknowledges
the command whatever this returns.
"""

import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from ag_shared.api import (
    AssignWorkerCommand,
    ConfigBundle,
    EndSessionCommand,
    EventType,
    LocalAlertKind,
    LocalAlertPayload,
    RosterEntry,
    SessionEndPayload,
    SessionEndReason,
    SessionMethod,
    SessionStartPayload,
)
from ag_shared.ids import Uuid7Generator
from pydantic import BaseModel, JsonValue

from ag_workstation.auth.messages import MessageKey
from ag_workstation.auth.methods import AuthOutcome, LoginMethodRegistry, LoginRequest
from ag_workstation.auth.roster import RosterSource, roster_entry_by_user
from ag_workstation.auth.state import AuthStateStore, OpenSessionRow
from ag_workstation.clock import Clock
from ag_workstation.engine_api import EngineConfigTarget

logger = logging.getLogger(__name__)

HANDLED_COMMAND_RETENTION: Final = timedelta(days=7)
"""Commands repeat only until acknowledged, so a week of ids is plenty."""


class RecordSink(Protocol):
    """Where session records go (`EventQueue` satisfies this)."""

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel,
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        """Store one batch item durably; may raise."""
        ...


class EndCause(StrEnum):
    """Why this station ended a session (finer than the wire `end_reason`)."""

    LOGOUT = "logout"
    REPLACED = "replaced"
    ASSIGNMENT_EXPIRED = "assignment_expired"
    END_SESSION_COMMAND = "end_session_command"
    SHUTDOWN = "shutdown"
    RECOVERED = "recovered"
    """Found open at start-up after a crash or power loss."""
    SHIFT_END = "shift_end"
    TIMEOUT = "timeout"

    @property
    def wire_reason(self) -> SessionEndReason:
        """The `worker_session.end_reason` value (schema: logout, timeout, shift_end, replaced)."""
        return _WIRE_REASON[self]


_WIRE_REASON: Final = {
    EndCause.LOGOUT: SessionEndReason.LOGOUT,
    EndCause.REPLACED: SessionEndReason.REPLACED,
    EndCause.ASSIGNMENT_EXPIRED: SessionEndReason.TIMEOUT,
    # A supervisor ending the session from the console is a logout on their behalf.
    EndCause.END_SESSION_COMMAND: SessionEndReason.LOGOUT,
    EndCause.SHUTDOWN: SessionEndReason.LOGOUT,
    EndCause.RECOVERED: SessionEndReason.TIMEOUT,
    EndCause.SHIFT_END: SessionEndReason.SHIFT_END,
    EndCause.TIMEOUT: SessionEndReason.TIMEOUT,
}


class CommandOutcome(StrEnum):
    """What a heartbeat command did. Every outcome is final (acknowledge it)."""

    STARTED = "started"
    ENDED = "ended"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"
    REFUSED = "refused"
    """No roster, or the user is unknown or inactive in it."""
    NOT_CURRENT = "not_current"
    """`end_session` for a session that is not open here."""


class HealthFlag(StrEnum):
    """Problems this module reports to station health."""

    PPE_APPLY_FAILED = "ppe_apply_failed"
    RECORD_WRITE_FAILED = "session_record_write_failed"
    STATE_STORE_FAILED = "login_state_store_failed"


@dataclass(frozen=True, slots=True)
class CurrentSession:
    """The open session, for the consent gate, scan flow and UI (2.5, 2.6, 2.8)."""

    session_id: UUID
    user_id: UUID
    employee_id: str
    display_name: str
    has_consent: bool
    """From the currently applied roster; False if the worker is no longer listed."""
    preferred_language: str | None
    ppe_profile_id: UUID | None
    """The PPE override the engine is running; None: the station profile."""
    method: SessionMethod
    station_assignment_id: UUID | None
    started_at: datetime
    """Device time (the capture gate's time base); the record carries corrected UTC."""
    valid_until: datetime | None
    """End of a supervisor assignment (server time); None for a login."""


@dataclass(frozen=True, slots=True)
class LoginResult:
    """What the login screen shows."""

    outcome: AuthOutcome
    session: CurrentSession | None = None
    retry_after: timedelta | None = None
    attempts_remaining: int | None = None

    @property
    def message_key(self) -> MessageKey:
        """The message to show; generic for every kind of failure."""
        return self.outcome.message_key


class SessionManager:
    """Owns the station's worker session. Thread-safe.

    Call `recover()` once at start-up, `tick()` periodically (expiry of
    supervisor assignments), and `on_bundle_applied()` after the config
    applier installed a new bundle.
    """

    def __init__(
        self,
        *,
        registry: LoginMethodRegistry,
        roster: RosterSource,
        records: RecordSink,
        engine: EngineConfigTarget,
        state: AuthStateStore,
        clock: Clock,
        ids: Uuid7Generator | None = None,
        utc: Clock | None = None,
    ) -> None:
        """`clock` is the device clock; `utc` the server-corrected clock that
        stamps records (default: `clock`)."""
        self._registry = registry
        self._roster = roster
        self._records = records
        self._engine = engine
        self._state = state
        self._clock = clock
        self._utc = utc or clock
        self._ids = ids or Uuid7Generator(now=self._utc.now)
        self._lock = threading.RLock()
        self._active: CurrentSession | None = None
        self._engine_profile: UUID | None = None
        """What the engine runs; the config applier installs the station profile."""
        self._engine_known = True
        self._flags: set[HealthFlag] = set()
        self._handled_fallback: set[str] = set()

    # -- public -------------------------------------------------------------

    def recover(self) -> None:
        """Close a session left open by a crash and purge old command ids."""
        with self._lock:
            now = self._clock.now()
            try:
                row = self._state.get_open_session()
                self._state.purge_handled(now - HANDLED_COMMAND_RETENTION)
            except Exception as exc:
                self._store_failed(exc)
                return
            if row is not None:
                self._write(
                    EventType.SESSION_END,
                    SessionEndPayload(
                        session_id=row.session_id, end_reason=EndCause.RECOVERED.wire_reason
                    ),
                    self._utc.now(),
                )
                self._set_open(None)

    def login(self, method_name: str, request: LoginRequest) -> LoginResult:
        """Authenticate with the named method; on success start a session.

        Never raises. The open session, if any, is replaced only on success.
        """
        method = self._registry.get(method_name)
        if method is None:
            logger.warning("login with unknown method %r refused", method_name[:40])
            return LoginResult(AuthOutcome.UNAVAILABLE)
        try:
            result = method.authenticate(request)
        except Exception as exc:
            # Type name only: the message could echo the request.
            logger.error("login method %s failed: %s", method.name, type(exc).__name__)
            self._raise(HealthFlag.STATE_STORE_FAILED)
            return LoginResult(AuthOutcome.UNAVAILABLE)

        if result.outcome in (AuthOutcome.FAILED, AuthOutcome.LOCKED):
            self._audit_failure(
                method.name, result.subject_user_id, result.newly_locked, result.outcome
            )
        if result.outcome is not AuthOutcome.SUCCESS or result.user is None:
            return LoginResult(
                result.outcome,
                retry_after=result.retry_after,
                attempts_remaining=result.attempts_remaining,
            )
        with self._lock:
            session = self._start(result.user, method.session_method, None, None)
        return LoginResult(AuthOutcome.SUCCESS, session=session)

    def logout(self) -> bool:
        """End the open session (worker logout); False if none was open."""
        return self.end(EndCause.LOGOUT)

    def shutdown(self) -> None:
        """End the open session because the app stops (the engine is not touched)."""
        with self._lock:
            if self._active is not None:
                self._end(EndCause.SHUTDOWN, revert=False)

    def end(self, cause: EndCause) -> bool:
        """End the open session for `cause`; False if none was open."""
        with self._lock:
            if self._active is None:
                return False
            self._end(cause)
            return True

    def handle_assign_worker(self, command: AssignWorkerCommand) -> CommandOutcome:
        """Start a `supervisor_assigned` session from a heartbeat command."""
        with self._lock:
            keys = (f"cmd:{command.id}", f"asg:{command.assignment_id}")
            if any(self._is_handled(key) for key in keys):
                return CommandOutcome.DUPLICATE
            for key in keys:
                self._mark_handled(key)
            if command.valid_until <= self._utc.now():
                logger.info("assignment %s arrived expired", command.assignment_id)
                return CommandOutcome.EXPIRED
            bundle = self._roster.current_bundle()
            entry = None if bundle is None else roster_entry_by_user(bundle, command.user_id)
            if entry is None or not entry.is_active:
                logger.warning(
                    "assignment %s refused: worker not in the active roster",
                    command.assignment_id,
                )
                return CommandOutcome.REFUSED
            self._start(
                entry,
                SessionMethod.SUPERVISOR_ASSIGNED,
                command.assignment_id,
                command.valid_until,
            )
            return CommandOutcome.STARTED

    def handle_end_session(self, command: EndSessionCommand) -> CommandOutcome:
        """End the open session if it is the one the command names."""
        with self._lock:
            key = f"cmd:{command.id}"
            if self._is_handled(key):
                return CommandOutcome.DUPLICATE
            self._mark_handled(key)
            self._expire_if_due()
            if self._active is None or self._active.session_id != command.session_id:
                return CommandOutcome.NOT_CURRENT
            self._end(EndCause.END_SESSION_COMMAND)
            return CommandOutcome.ENDED

    def tick(self) -> None:
        """End a supervisor assignment whose `valid_until` has passed."""
        with self._lock:
            self._expire_if_due()

    def current(self) -> CurrentSession | None:
        """The open session with consent, name and language from the current roster."""
        with self._lock:
            self._expire_if_due()
            active = self._active
            if active is None:
                return None
            bundle = self._roster.current_bundle()
            entry = None if bundle is None else roster_entry_by_user(bundle, active.user_id)
            if entry is None:
                # Not listed any more: withdraw consent so no image is taken.
                return replace(active, has_consent=False)
            return replace(
                active,
                display_name=entry.display_name,
                has_consent=entry.has_consent and entry.is_active,
                preferred_language=entry.preferred_language,
            )

    def on_bundle_applied(self) -> None:
        """The config applier just installed a new bundle's station profile.

        Re-applies the open session's PPE override from the new roster.
        """
        with self._lock:
            self._engine_profile, self._engine_known = None, True
            active = self._active
            if active is None:
                return
            bundle = self._roster.current_bundle()
            entry = None if bundle is None else roster_entry_by_user(bundle, active.user_id)
            override = None if entry is None else entry.ppe_profile_override
            effective = self._apply_profile(override, bundle)
            self._active = replace(active, ppe_profile_id=effective)

    def health_flags(self) -> frozenset[HealthFlag]:
        """Current problems (cleared when the operation next succeeds)."""
        with self._lock:
            return frozenset(self._flags)

    # -- internals (callers hold the lock) ----------------------------------

    def _start(
        self,
        entry: RosterEntry,
        method: SessionMethod,
        assignment_id: UUID | None,
        valid_until: datetime | None,
    ) -> CurrentSession:
        if self._active is not None:
            self._end(EndCause.REPLACED, revert=False)
        bundle = self._roster.current_bundle()
        effective = self._apply_profile(entry.ppe_profile_override, bundle)
        session_id = self._ids()
        now = self._clock.now()  # device time: pushed to the capture gate
        self._write(
            EventType.SESSION_START,
            SessionStartPayload(
                user_id=entry.user_id,
                method=method,
                station_assignment_id=assignment_id,
                ppe_profile_id=effective,
            ),
            self._utc.now(),
            record_id=session_id,
        )
        self._set_open(OpenSessionRow(session_id, entry.user_id, now))
        self._active = CurrentSession(
            session_id=session_id,
            user_id=entry.user_id,
            employee_id=entry.employee_id,
            display_name=entry.display_name,
            has_consent=entry.has_consent,
            preferred_language=entry.preferred_language,
            ppe_profile_id=effective,
            method=method,
            station_assignment_id=assignment_id,
            started_at=now,
            valid_until=valid_until,
        )
        logger.info("session %s started (%s)", session_id, method.value)
        return self._active

    def _end(self, cause: EndCause, *, revert: bool = True) -> None:
        active = self._active
        if active is None:
            return
        self._active = None
        self._write(
            EventType.SESSION_END,
            SessionEndPayload(session_id=active.session_id, end_reason=cause.wire_reason),
            self._utc.now(),
        )
        self._set_open(None)
        logger.info("session %s ended (%s)", active.session_id, cause.value)
        if revert:
            self._apply_profile(None, self._roster.current_bundle())

    def _expire_if_due(self) -> None:
        active = self._active
        if (
            active is not None
            and active.valid_until is not None
            and self._utc.now() >= active.valid_until
        ):
            self._end(EndCause.ASSIGNMENT_EXPIRED)

    def _apply_profile(self, override: UUID | None, bundle: ConfigBundle | None) -> UUID | None:
        """Make the engine run `override` (None: station); returns what it runs."""
        if bundle is None:
            return self._engine_profile if self._engine_known else None
        carried = {p.ppe_profile_id for p in bundle.profile_thresholds}
        if override is not None and override not in carried:
            logger.warning("PPE override %s is not in the config bundle; station profile", override)
            override = None
        if self._engine_known and self._engine_profile == override:
            return override
        try:
            result = self._engine.apply_config(bundle.engine_config(override))
        except Exception as exc:
            logger.error("engine config apply failed: %s", type(exc).__name__)
            self._raise(HealthFlag.PPE_APPLY_FAILED)
            self._engine_known = False
            return None
        if not result.accepted:
            logger.error("engine refused the PPE profile config: %s", result.reason)
            self._raise(HealthFlag.PPE_APPLY_FAILED)
            # The previous config stays active.
            return self._engine_profile if self._engine_known else None
        self._flags.discard(HealthFlag.PPE_APPLY_FAILED)
        self._engine_profile, self._engine_known = override, True
        return override

    def _write(
        self,
        item_type: EventType,
        payload: BaseModel,
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> None:
        try:
            self._records.enqueue(item_type, payload, occurred_at, record_id=record_id)
        except Exception as exc:
            logger.error("could not queue %s record: %s", item_type.value, type(exc).__name__)
            self._raise(HealthFlag.RECORD_WRITE_FAILED)
            return
        self._flags.discard(HealthFlag.RECORD_WRITE_FAILED)

    def _audit_failure(
        self, method: str, user_id: UUID | None, newly_locked: bool, outcome: AuthOutcome
    ) -> None:
        """Queue failed logins and lockouts for the server audit (security-privacy 3).

        Contract 4.3 has no login event type, so this is a `local_alert` of
        kind `other` (2.R decision; proposal: a `login_failure` type). It
        names the roster user the typed ID matched, or none: the typed text
        itself never leaves the station, because an unknown ID is often a
        PIN typed into the ID field (never log PINs)."""
        if outcome is AuthOutcome.LOCKED and not newly_locked:
            return
        event = "worker_login_locked" if newly_locked else "worker_login_failed"
        detail: dict[str, JsonValue] = {
            "event": event,
            "method": method,
            "user_id": None if user_id is None else str(user_id),
            "known_worker": user_id is not None,
        }
        with self._lock:
            self._write(
                EventType.LOCAL_ALERT,
                LocalAlertPayload(kind=LocalAlertKind.OTHER, detail=detail),
                self._utc.now(),
            )

    def _set_open(self, row: OpenSessionRow | None) -> None:
        try:
            self._state.set_open_session(row)
        except Exception as exc:
            self._store_failed(exc)

    def _is_handled(self, key: str) -> bool:
        if key in self._handled_fallback:
            return True
        try:
            return self._state.is_handled(key)
        except Exception as exc:
            self._store_failed(exc)
            return False

    def _mark_handled(self, key: str) -> None:
        try:
            self._state.mark_handled(key, self._clock.now())
        except Exception as exc:
            # Remember it in memory so a repeat in this run is still a duplicate.
            self._handled_fallback.add(key)
            self._store_failed(exc)

    def _store_failed(self, exc: Exception) -> None:
        logger.error("login state store failed: %s", type(exc).__name__)
        self._raise(HealthFlag.STATE_STORE_FAILED)

    def _raise(self, flag: HealthFlag) -> None:
        with self._lock:
            self._flags.add(flag)
