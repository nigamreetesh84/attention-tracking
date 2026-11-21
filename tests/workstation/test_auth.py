"""Worker login and sessions (PLAN 2.4): PIN login, lockout, plug-ins, sessions, PPE override.

Workers in the test roster:

| ID     | user | PIN    | notes                                      |
|--------|------|--------|--------------------------------------------|
| EMP001 | W1   | 1234   | consent, PPE override MASK (carried), `te` |
| EMP002 | W2   | 567890 | no consent, no override                    |
| EMP003 | W3   | 1111   | inactive                                   |
| EMP004 | W4   | -      | no PIN set                                 |
| EMP005 | W5   | 2468   | override SUN, not carried by the bundle    |
"""

import logging
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from ag_shared.api import (
    AssignWorkerCommand,
    ConfigBundle,
    EndSessionCommand,
    EventType,
    LocalAlertPayload,
    SessionEndItem,
    SessionEndPayload,
    SessionEndReason,
    SessionMethod,
    SessionStartItem,
    SessionStartPayload,
    parse_event_item,
)
from ag_shared.config import EngineConfig
from ag_shared.ids import is_uuid7, new_uuid7
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_workstation.auth import (
    FAILURE_WINDOW,
    LOCK_DURATION,
    PIN_METHOD_NAME,
    AuthOutcome,
    AuthResult,
    AuthStateStore,
    CommandOutcome,
    EndCause,
    HealthFlag,
    LockoutRow,
    LoginMethodRegistry,
    LoginRequest,
    LoginThrottle,
    MessageKey,
    PinCredentials,
    PinLoginMethod,
    SessionManager,
    StaticRosterSource,
    lock_minutes,
    text,
)
from ag_workstation.clock import FakeClock
from ag_workstation.corrected_time import StampingClock
from ag_workstation.engine_api import ConfigApplyResult
from ag_workstation.fakes.engine import FakeStationEngine
from ag_workstation.queue import EventQueue, InMemoryKeyProvider
from argon2 import PasswordHasher
from pydantic import BaseModel

Json = dict[str, Any]  # Any: free-form JSON test fixtures

AUTH_T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
# Low-cost argon2id so the suite stays fast; production hashes come from the server.
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

SHIFT_A = UUID("0193a0b0-0000-7000-8000-00000000a001")
MASK = UUID("0193a0b0-0000-7000-8000-00000000b001")
SUN = UUID("0193a0b0-0000-7000-8000-00000000b002")
STATION = UUID("0193a0b0-0000-7000-8000-00000000d001")
W1 = UUID("0193a0b0-0000-7000-8000-00000000c001")
W2 = UUID("0193a0b0-0000-7000-8000-00000000c002")
W3 = UUID("0193a0b0-0000-7000-8000-00000000c003")
W4 = UUID("0193a0b0-0000-7000-8000-00000000c004")
W5 = UUID("0193a0b0-0000-7000-8000-00000000c005")

PINS = {"EMP001": "1234", "EMP002": "567890", "EMP003": "1111", "EMP005": "2468"}
_HASHES = {emp: FAST_HASHER.hash(pin) for emp, pin in PINS.items()}

STATION_ABSENT = 10.0
MASK_ABSENT = 20.0


def _profile(kind: PpeProfileKind) -> Json:
    return builtin_profile(kind).model_dump(mode="json")


def _worker(
    user_id: UUID,
    employee_id: str,
    *,
    consent: bool = True,
    override: UUID | None = None,
    language: str | None = None,
    active: bool = True,
) -> Json:
    return {
        "user_id": str(user_id),
        "employee_id": employee_id,
        "display_name": f"Worker {employee_id}",
        "pin_hash": _HASHES.get(employee_id),
        "has_consent": consent,
        "ppe_profile_override": None if override is None else str(override),
        "preferred_language": language,
        "is_active": active,
    }


def bundle_json(version: int = 7) -> Json:
    """A valid bundle with five workers (see the worker table in the tests)."""
    return {
        "version": version,
        "generated_at": "2026-11-20T07:00:00Z",
        "site": {"timezone": "Asia/Kolkata", "settings": {}},
        "workstation": {
            "id": str(STATION),
            "name": "Line 1 / WS 3",
            "line_id": str(STATION),
            "camera_count": 1,
        },
        "cameras": [{"slot": 1, "role": "primary", "kind": "usb", "uri": "0"}],
        "ppe_profile": _profile(PpeProfileKind.OPEN_FACE),
        "thresholds": {"absent_seconds": STATION_ABSENT},
        "profile_thresholds": [
            {
                "ppe_profile_id": str(MASK),
                "ppe_profile": _profile(PpeProfileKind.MASK),
                "thresholds": {"absent_seconds": MASK_ABSENT},
            }
        ],
        "roster": [
            _worker(W1, "EMP001", override=MASK, language="te"),
            _worker(W2, "EMP002", consent=False),
            _worker(W3, "EMP003", active=False),
            _worker(W4, "EMP004"),
            _worker(W5, "EMP005", override=SUN),
        ],
    }


def make_bundle(mutate: Callable[[Json], None] | None = None, version: int = 7) -> ConfigBundle:
    """`bundle_json` after `mutate`, validated."""
    data = bundle_json(version)
    if mutate is not None:
        mutate(data)
    return ConfigBundle.model_validate(data)


@dataclass(frozen=True)
class Recorded:
    """One enqueued record."""

    item_type: str
    payload: BaseModel
    occurred_at: datetime
    record_id: UUID | None


class RecordingSink:
    """A `RecordSink` that keeps records in memory; can be told to fail."""

    def __init__(self) -> None:
        self.records: list[Recorded] = []
        self.fail = False

    def enqueue(
        self,
        item_type: str,
        payload: BaseModel,
        occurred_at: datetime,
        *,
        record_id: UUID | None = None,
    ) -> object:
        if self.fail:
            raise OSError("disk I/O error")
        self.records.append(Recorded(str(item_type), payload, occurred_at, record_id))
        return None

    def of_type(self, item_type: str) -> list[Recorded]:
        return [r for r in self.records if r.item_type == item_type]


@dataclass
class AuthRig:
    """Everything a session test needs, wired together."""

    clock: FakeClock
    roster: StaticRosterSource
    state: AuthStateStore
    throttle: LoginThrottle
    pin: PinLoginMethod
    registry: LoginMethodRegistry
    records: RecordingSink
    engine: FakeStationEngine
    manager: SessionManager


def build_rig(
    clock: FakeClock, state: AuthStateStore, roster: StaticRosterSource | None = None
) -> AuthRig:
    """A rig over `state` (so a test can 'restart' with the same store)."""
    roster = roster or StaticRosterSource(make_bundle())
    throttle = LoginThrottle(state, clock)
    pin = PinLoginMethod(roster, throttle, clock, hasher=FAST_HASHER)
    registry = LoginMethodRegistry()
    registry.register(pin)
    records = RecordingSink()
    engine = FakeStationEngine(clock)
    manager = SessionManager(
        registry=registry,
        roster=roster,
        records=records,
        engine=engine,
        state=state,
        clock=clock,
    )
    return AuthRig(clock, roster, state, throttle, pin, registry, records, engine, manager)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_utc=AUTH_T0)


@pytest.fixture
def state() -> Iterator[AuthStateStore]:
    store = AuthStateStore.in_memory()
    yield store
    store.close()


@pytest.fixture
def rig(clock: FakeClock, state: AuthStateStore) -> AuthRig:
    return build_rig(clock, state)


def _login(rig: AuthRig, employee_id: str, pin: str) -> Any:  # Any: LoginResult, kept short
    return rig.manager.login(PIN_METHOD_NAME, PinCredentials(employee_id, pin))


def _alerts(rig: AuthRig) -> list[dict[str, Any]]:  # Any: free-form alert detail
    out = []
    for rec in rig.records.of_type(EventType.LOCAL_ALERT):
        assert isinstance(rec.payload, LocalAlertPayload)
        out.append(dict(rec.payload.detail))
    return out


def _starts(rig: AuthRig) -> list[SessionStartPayload]:
    out = []
    for rec in rig.records.of_type(EventType.SESSION_START):
        assert isinstance(rec.payload, SessionStartPayload)
        out.append(rec.payload)
    return out


def _ends(rig: AuthRig) -> list[SessionEndPayload]:
    out = []
    for rec in rig.records.of_type(EventType.SESSION_END):
        assert isinstance(rec.payload, SessionEndPayload)
        out.append(rec.payload)
    return out


# -- PIN login --------------------------------------------------------------


def test_success_starts_a_session_with_roster_details(rig: AuthRig) -> None:
    result = _login(rig, "EMP001", "1234")
    assert result.outcome is AuthOutcome.SUCCESS
    assert result.message_key is MessageKey.LOGIN_OK
    session = result.session
    assert session is not None
    assert session.user_id == W1
    assert session.employee_id == "EMP001"
    assert session.display_name == "Worker EMP001"
    assert session.has_consent is True
    assert session.preferred_language == "te"
    assert session.ppe_profile_id == MASK
    assert session.method is SessionMethod.EMPLOYEE_ID_PIN
    assert session.station_assignment_id is None
    assert session.valid_until is None
    assert session.started_at == AUTH_T0
    assert rig.manager.current() == session
    assert _alerts(rig) == []


def test_wrong_pin_is_a_generic_failure(rig: AuthRig) -> None:
    result = _login(rig, "EMP001", "9999")
    assert result.outcome is AuthOutcome.FAILED
    assert result.message_key is MessageKey.LOGIN_FAILED
    assert result.session is None
    assert result.attempts_remaining == 4
    assert rig.manager.current() is None
    assert rig.records.of_type(EventType.SESSION_START) == []
    assert _alerts(rig) == [
        {"event": "worker_login_failed", "method": PIN_METHOD_NAME, "employee_id": "emp001"}
    ]


@pytest.mark.parametrize(
    ("employee_id", "pin"),
    [
        ("NOBODY1", "1234"),  # unknown ID
        ("EMP003", "1111"),  # inactive worker, correct PIN
        ("EMP004", "1234"),  # no PIN set
    ],
)
def test_unknown_inactive_and_pinless_look_like_a_wrong_pin(
    rig: AuthRig, employee_id: str, pin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    verified: list[str] = []
    original = PasswordHasher.verify

    def spy(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        verified.append(str(hash))
        return original(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", spy)
    wrong = _login(rig, "EMP001", "9999")
    other = _login(rig, employee_id, pin)
    assert other.outcome is wrong.outcome is AuthOutcome.FAILED
    assert other.message_key is wrong.message_key
    assert other.attempts_remaining == wrong.attempts_remaining == 4
    # Exactly one argon2 verification each, with the roster's cost parameters.
    assert len(verified) == 2
    params = verified[0].split("$")[3]
    assert verified[1].split("$")[3] == params
    assert verified[1].startswith("$argon2id$")


def test_inactive_worker_is_refused(rig: AuthRig) -> None:
    assert _login(rig, "EMP003", "1111").outcome is AuthOutcome.FAILED
    assert rig.manager.current() is None


@pytest.mark.parametrize(
    ("employee_id", "pin"),
    [
        ("", "1234"),
        ("   ", "1234"),
        ("EMP-01", "1234"),
        ("A" * 17, "1234"),
        ("EMPØ", "1234"),
        ("EMP001", ""),
        ("EMP001", "123"),
        ("EMP001", "1234567"),
        ("EMP001", "12a4"),
        ("EMP001", "".join(chr(0xFF11 + i) for i in range(4))),  # full-width digits
        ("EMP001", "1234\n"),
        ("EMP001", " 1234"),
    ],
)
def test_invalid_input_is_not_counted(rig: AuthRig, employee_id: str, pin: str) -> None:
    for _ in range(6):
        result = _login(rig, employee_id, pin)
        assert result.outcome is AuthOutcome.INVALID_INPUT
        assert result.message_key is MessageKey.LOGIN_INVALID_INPUT
    assert rig.records.records == []
    assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.SUCCESS


def test_employee_id_is_case_insensitive_and_trimmed(rig: AuthRig) -> None:
    result = _login(rig, "  emp001\t", "1234")
    assert result.outcome is AuthOutcome.SUCCESS
    assert result.session.user_id == W1
    assert _login(rig, "A" * 16, "1234").outcome is AuthOutcome.FAILED  # 16 is valid format
    assert _login(rig, "EMP002", "567890").outcome is AuthOutcome.SUCCESS  # 6-digit PIN


def test_no_roster_yet_is_unavailable(clock: FakeClock, state: AuthStateStore) -> None:
    rig = build_rig(clock, state, StaticRosterSource(None))
    result = _login(rig, "EMP001", "1234")
    assert result.outcome is AuthOutcome.UNAVAILABLE
    assert result.message_key is MessageKey.LOGIN_UNAVAILABLE
    assert rig.throttle.status("emp001").locked is False


def test_wrong_request_type_is_invalid_input(rig: AuthRig) -> None:
    assert rig.pin.authenticate(LoginRequest()).outcome is AuthOutcome.INVALID_INPUT


def test_malformed_roster_hash_fails_without_logging_it(
    clock: FakeClock, state: AuthStateStore, caplog: pytest.LogCaptureFixture
) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["roster"][1]["pin_hash"] = "$argon2id$garbage-HASHSENTINEL"

    rig = build_rig(clock, state, StaticRosterSource(make_bundle(mutate)))
    with caplog.at_level(logging.DEBUG):
        assert _login(rig, "EMP002", "567890").outcome is AuthOutcome.FAILED
        assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.SUCCESS
    assert "malformed" in caplog.text
    assert "HASHSENTINEL" not in caplog.text


def test_dummy_hash_when_roster_has_no_hashes(clock: FakeClock, state: AuthStateStore) -> None:
    def mutate(data: dict[str, Any]) -> None:
        for worker in data["roster"]:
            worker["pin_hash"] = None

    rig = build_rig(clock, state, StaticRosterSource(make_bundle(mutate)))
    assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.FAILED
    assert _login(rig, "EMP002", "1234").outcome is AuthOutcome.FAILED


def test_pins_and_hashes_never_reach_logs_or_repr(
    rig: AuthRig, caplog: pytest.LogCaptureFixture
) -> None:
    creds = PinCredentials("EMP001", "8642")
    assert "8642" not in repr(creds)
    with caplog.at_level(logging.DEBUG):
        for _ in range(6):
            _login(rig, "EMP002", "864297")
        _login(rig, "EMP001", "1234")
        rig.manager.logout()
    for secret in ("864297", "1234", "567890", "$argon2"):
        assert secret not in caplog.text
    for rec in rig.records.records:
        dumped = rec.payload.model_dump_json()
        assert "864297" not in dumped
        assert "argon2" not in dumped


# -- lockout ----------------------------------------------------------------


def test_lockout_after_five_failures_and_expiry(rig: AuthRig) -> None:
    for remaining in (4, 3, 2, 1):
        result = _login(rig, "EMP001", "0000")
        assert result.outcome is AuthOutcome.FAILED
        assert result.attempts_remaining == remaining
    fifth = _login(rig, "EMP001", "0000")
    assert fifth.outcome is AuthOutcome.LOCKED
    assert fifth.message_key is MessageKey.LOGIN_LOCKED
    assert fifth.retry_after == LOCK_DURATION
    # The correct PIN is refused while locked; nothing new is counted or audited.
    alerts_before = len(_alerts(rig))
    rig.clock.advance(299)
    locked = _login(rig, "emp001", "1234")
    assert locked.outcome is AuthOutcome.LOCKED
    assert locked.retry_after == timedelta(seconds=1)
    assert len(_alerts(rig)) == alerts_before
    rig.clock.advance(1)
    assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.SUCCESS
    events = [a["event"] for a in _alerts(rig)]
    assert events == ["worker_login_failed"] * 4 + ["worker_login_locked"]


def test_lockout_is_per_employee_id(rig: AuthRig) -> None:
    for _ in range(5):
        _login(rig, "EMP001", "0000")
    assert _login(rig, "EMP002", "567890").outcome is AuthOutcome.SUCCESS


def test_unknown_ids_lock_like_known_ones(rig: AuthRig) -> None:
    results = [_login(rig, "GHOST9", "0000").outcome for _ in range(5)]
    assert results == [AuthOutcome.FAILED] * 4 + [AuthOutcome.LOCKED]
    assert _login(rig, "GHOST9", "0000").outcome is AuthOutcome.LOCKED


def test_success_resets_the_failure_count(rig: AuthRig) -> None:
    for _ in range(4):
        _login(rig, "EMP001", "0000")
    assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.SUCCESS
    assert _login(rig, "EMP001", "0000").attempts_remaining == 4


def test_failures_outside_the_window_start_a_new_count(rig: AuthRig) -> None:
    for _ in range(4):
        _login(rig, "EMP001", "0000")
    rig.clock.advance(FAILURE_WINDOW.total_seconds())
    assert _login(rig, "EMP001", "0000").attempts_remaining == 4


def test_lockout_survives_a_restart(tmp_path: Path, clock: FakeClock) -> None:
    db = tmp_path / "auth.db"
    with AuthStateStore.open(db) as store:
        rig = build_rig(clock, store)
        for _ in range(5):
            _login(rig, "EMP001", "0000")
    with AuthStateStore.open(db) as store:
        rig = build_rig(clock, store)
        assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.LOCKED
        clock.advance(LOCK_DURATION.total_seconds())
        assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.SUCCESS
    # No PIN material is stored in the clear.
    wal = tmp_path / "auth.db-wal"
    raw = db.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    assert b"1234" not in raw.replace(b"EMP001", b"")
    assert b"argon2" not in raw


def test_a_clock_set_back_never_lengthens_a_lock(clock: FakeClock, state: AuthStateStore) -> None:
    throttle = LoginThrottle(state, clock)
    state.put_lockout("emp001", LockoutRow(0, AUTH_T0, AUTH_T0 + timedelta(hours=3)))
    status = throttle.status("emp001")
    assert status.locked_until == AUTH_T0 + LOCK_DURATION
    clock.advance(LOCK_DURATION.total_seconds())
    assert throttle.status("emp001").locked is False


def test_throttle_purge_and_validation(clock: FakeClock, state: AuthStateStore) -> None:
    with pytest.raises(ValueError, match="max_failures"):
        LoginThrottle(state, clock, max_failures=0)
    throttle = LoginThrottle(state, clock)
    throttle.record_failure("a")
    for _ in range(5):
        throttle.record_failure("b")
    assert throttle.purge() == 0
    clock.advance(FAILURE_WINDOW.total_seconds() + 1)
    assert throttle.purge() == 2
    assert state.get_lockout("a") is None


def test_lock_minutes_and_messages() -> None:
    assert lock_minutes(None) == 1
    assert lock_minutes(timedelta(seconds=0)) == 1
    assert lock_minutes(timedelta(seconds=61)) == 2
    assert lock_minutes(timedelta(minutes=5)) == 5
    assert text(MessageKey.LOGIN_LOCKED, minutes=5) == "Too many attempts. Try again in 5 min."
    assert "Worker" in text(MessageKey.LOGIN_OK, name="Worker")
    for outcome in AuthOutcome:
        assert text(outcome.message_key, minutes=1, name="x")


# -- plug-in interface ------------------------------------------------------


@dataclass(frozen=True)
class TokenRequest(LoginRequest):
    token: str


class TokenLoginMethod:
    """A stand-in for a future method (face match would look like this)."""

    def __init__(self, roster: StaticRosterSource) -> None:
        self._roster = roster
        self.fail_with: Exception | None = None

    @property
    def name(self) -> str:
        return "test_token"

    @property
    def session_method(self) -> SessionMethod:
        return SessionMethod.EMPLOYEE_ID_PIN

    def authenticate(self, request: LoginRequest) -> AuthResult:
        if self.fail_with is not None:
            raise self.fail_with
        bundle = self._roster.current_bundle()
        assert bundle is not None
        if isinstance(request, TokenRequest) and request.token == "ok":  # noqa: S105 - test token, not a credential
            entry = bundle.roster_entry("EMP002")
            return AuthResult(AuthOutcome.SUCCESS, user=entry, subject="emp002")
        return AuthResult(AuthOutcome.FAILED, subject="?")


def test_plugin_registration_and_login(rig: AuthRig) -> None:
    token = TokenLoginMethod(rig.roster)
    rig.registry.register(token)
    assert rig.registry.names() == (PIN_METHOD_NAME, "test_token")
    assert rig.registry.get("test_token") is token
    assert rig.registry.get("nope") is None
    with pytest.raises(ValueError, match="already registered"):
        rig.registry.register(TokenLoginMethod(rig.roster))

    result = rig.manager.login("test_token", TokenRequest("ok"))
    assert result.outcome is AuthOutcome.SUCCESS
    assert result.session is not None
    assert result.session.user_id == W2
    assert rig.manager.login("test_token", TokenRequest("bad")).outcome is AuthOutcome.FAILED
    assert rig.manager.login("face", TokenRequest("ok")).outcome is AuthOutcome.UNAVAILABLE


def test_a_failing_method_is_contained(rig: AuthRig, caplog: pytest.LogCaptureFixture) -> None:
    token = TokenLoginMethod(rig.roster)
    token.fail_with = RuntimeError("secret-detail")
    rig.registry.register(token)
    with caplog.at_level(logging.DEBUG):
        result = rig.manager.login("test_token", TokenRequest("ok"))
    assert result.outcome is AuthOutcome.UNAVAILABLE
    assert "secret-detail" not in caplog.text
    assert HealthFlag.STATE_STORE_FAILED in rig.manager.health_flags()


def test_auth_result_invariant() -> None:
    with pytest.raises(ValueError, match="exactly on success"):
        AuthResult(AuthOutcome.SUCCESS)


# -- sessions ---------------------------------------------------------------


def test_session_records_have_uuid7_ids_and_clock_times(rig: AuthRig) -> None:
    session = _login(rig, "EMP001", "1234").session
    (start,) = rig.records.of_type(EventType.SESSION_START)
    assert start.record_id == session.session_id
    assert is_uuid7(session.session_id)
    assert start.occurred_at == AUTH_T0
    assert start.payload == SessionStartPayload(
        user_id=W1, method=SessionMethod.EMPLOYEE_ID_PIN, ppe_profile_id=MASK
    )
    rig.clock.advance(60)
    assert rig.manager.logout() is True
    (end,) = rig.records.of_type(EventType.SESSION_END)
    assert end.occurred_at == AUTH_T0 + timedelta(seconds=60)
    assert end.payload == SessionEndPayload(
        session_id=session.session_id, end_reason=SessionEndReason.LOGOUT
    )
    assert rig.manager.current() is None
    assert rig.manager.logout() is False


def test_records_go_through_the_real_queue(tmp_path: Path, clock: FakeClock) -> None:
    with (
        AuthStateStore.open(tmp_path / "auth.db") as state,
        EventQueue.open(tmp_path / "q.db", InMemoryKeyProvider(bytes(32)), clock) as queue,
    ):
        rig = build_rig(clock, state)
        manager = SessionManager(
            registry=rig.registry,
            roster=rig.roster,
            records=queue,
            engine=rig.engine,
            state=state,
            clock=clock,
        )
        session = manager.login(PIN_METHOD_NAME, PinCredentials("EMP002", "567890")).session
        assert session is not None
        clock.advance(5)
        manager.logout()
        items = [parse_event_item(r.to_item()) for r in queue.claim_batch()]
    start, end = items
    assert isinstance(start, SessionStartItem)
    assert start.id == session.session_id
    assert start.occurred_at == AUTH_T0
    assert start.payload.user_id == W2
    assert start.payload.ppe_profile_id is None
    assert isinstance(end, SessionEndItem)
    assert is_uuid7(end.id)
    assert end.payload.session_id == session.session_id
    assert end.occurred_at == AUTH_T0 + timedelta(seconds=5)


def test_a_new_login_replaces_the_session(rig: AuthRig) -> None:
    first = _login(rig, "EMP001", "1234").session
    rig.clock.advance(1)
    second = _login(rig, "EMP002", "567890").session
    assert [r.item_type for r in rig.records.records] == [
        EventType.SESSION_START,
        EventType.SESSION_END,
        EventType.SESSION_START,
    ]
    assert _ends(rig) == [
        SessionEndPayload(session_id=first.session_id, end_reason=SessionEndReason.REPLACED)
    ]
    assert rig.manager.current() == second
    assert second.session_id != first.session_id
    # Mask for W1, then straight to the station profile for W2 (no extra swap).
    kinds = [c.profile.kind for c in rig.engine.applied_configs]
    assert kinds == [PpeProfileKind.MASK, PpeProfileKind.OPEN_FACE]


def test_same_worker_logging_in_again_gets_a_new_session(rig: AuthRig) -> None:
    first = _login(rig, "EMP001", "1234").session
    second = _login(rig, "EMP001", "1234").session
    assert second.session_id != first.session_id
    assert _ends(rig)[0].end_reason is SessionEndReason.REPLACED
    assert len(rig.engine.applied_configs) == 1  # profile unchanged: no re-apply


def test_a_failed_login_keeps_the_open_session(rig: AuthRig) -> None:
    session = _login(rig, "EMP001", "1234").session
    _login(rig, "EMP002", "000000")
    assert rig.manager.current() == session
    assert _ends(rig) == []


# -- PPE override -----------------------------------------------------------


def _absent(config: EngineConfig | None) -> float:
    assert config is not None
    return config.thresholds.absent_seconds


def test_ppe_override_applies_profile_thresholds_and_reverts(rig: AuthRig) -> None:
    _login(rig, "EMP001", "1234")
    applied = rig.engine.config
    assert applied is not None
    assert applied.profile.kind is PpeProfileKind.MASK
    assert _absent(applied) == MASK_ABSENT
    assert applied.config_version == 7
    rig.manager.logout()
    reverted = rig.engine.config
    assert reverted is not None
    assert reverted.profile.kind is PpeProfileKind.OPEN_FACE
    assert _absent(reverted) == STATION_ABSENT
    assert rig.manager.health_flags() == frozenset()


def test_no_override_leaves_the_engine_alone(rig: AuthRig) -> None:
    _login(rig, "EMP002", "567890")
    rig.manager.logout()
    assert rig.engine.applied_configs == []


def test_override_missing_from_bundle_uses_station_profile(rig: AuthRig) -> None:
    session = _login(rig, "EMP005", "2468").session
    assert session.ppe_profile_id is None
    assert _starts(rig)[0].ppe_profile_id is None
    assert rig.engine.applied_configs == []


def test_engine_refusal_does_not_block_login(rig: AuthRig) -> None:
    rig.engine.reject_next_config("no")
    result = _login(rig, "EMP001", "1234")
    assert result.outcome is AuthOutcome.SUCCESS
    assert result.session.ppe_profile_id is None
    assert _starts(rig)[0].ppe_profile_id is None
    assert HealthFlag.PPE_APPLY_FAILED in rig.manager.health_flags()
    # The engine still runs the station profile, so logout has nothing to revert.
    rig.manager.logout()
    assert rig.engine.applied_configs == []
    _login(rig, "EMP001", "1234")
    assert _absent(rig.engine.config) == MASK_ABSENT
    assert rig.manager.health_flags() == frozenset()


class ExplodingEngine:
    """An engine whose config apply raises until told otherwise."""

    def __init__(self) -> None:
        self.broken = True
        self.applied: list[EngineConfig] = []

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        if self.broken:
            raise ConnectionError("pipe closed")
        self.applied.append(config)
        return ConfigApplyResult(True, config.config_version)


def test_engine_exception_does_not_block_login(clock: FakeClock, state: AuthStateStore) -> None:
    rig = build_rig(clock, state)
    engine = ExplodingEngine()
    manager = SessionManager(
        registry=rig.registry,
        roster=rig.roster,
        records=rig.records,
        engine=engine,
        state=state,
        clock=clock,
    )
    result = manager.login(PIN_METHOD_NAME, PinCredentials("EMP001", "1234"))
    assert result.outcome is AuthOutcome.SUCCESS
    assert result.session is not None
    assert result.session.ppe_profile_id is None
    assert manager.health_flags() == {HealthFlag.PPE_APPLY_FAILED}
    # The engine state is unknown now, so logout re-applies the station profile.
    engine.broken = False
    manager.logout()
    assert [c.profile.kind for c in engine.applied] == [PpeProfileKind.OPEN_FACE]
    assert manager.health_flags() == frozenset()


def test_on_bundle_applied_reapplies_the_override(rig: AuthRig) -> None:
    _login(rig, "EMP001", "1234")
    rig.manager.on_bundle_applied()
    assert len(rig.engine.applied_configs) == 2
    assert _absent(rig.engine.config) == MASK_ABSENT

    def drop_override(data: dict[str, Any]) -> None:
        data["roster"][0]["ppe_profile_override"] = None

    rig.roster.set_bundle(make_bundle(drop_override, version=8))
    rig.manager.on_bundle_applied()
    current = rig.manager.current()
    assert current is not None
    assert current.ppe_profile_id is None
    assert len(rig.engine.applied_configs) == 2
    rig.manager.logout()
    rig.manager.on_bundle_applied()  # no session: nothing to do
    assert len(rig.engine.applied_configs) == 2


def test_no_bundle_while_session_open(rig: AuthRig) -> None:
    _login(rig, "EMP001", "1234")
    rig.roster.set_bundle(None)
    current = rig.manager.current()
    assert current is not None
    assert current.has_consent is False
    rig.manager.logout()  # cannot revert without a bundle; does not raise
    assert rig.engine.config is not None


# -- current session view ---------------------------------------------------


def test_current_follows_the_applied_roster(rig: AuthRig) -> None:
    _login(rig, "EMP001", "1234")

    def update(data: dict[str, Any]) -> None:
        data["roster"][0].update(has_consent=False, preferred_language="hi", display_name="Renamed")

    rig.roster.set_bundle(make_bundle(update, version=8))
    current = rig.manager.current()
    assert current is not None
    assert (current.has_consent, current.preferred_language, current.display_name) == (
        False,
        "hi",
        "Renamed",
    )

    def remove(data: dict[str, Any]) -> None:
        data["roster"] = data["roster"][1:]

    rig.roster.set_bundle(make_bundle(remove, version=9))
    current = rig.manager.current()
    assert current is not None
    assert current.has_consent is False


def test_no_consent_worker(rig: AuthRig) -> None:
    session = _login(rig, "EMP002", "567890").session
    assert session.has_consent is False


# -- supervisor assignment --------------------------------------------------


def _assign(
    user: UUID = W2, minutes: int = 60, *, command_id: UUID | None = None, assignment: UUID | None
) -> AssignWorkerCommand:
    return AssignWorkerCommand(
        id=command_id or new_uuid7(),
        assignment_id=assignment or new_uuid7(),
        user_id=user,
        valid_until=AUTH_T0 + timedelta(minutes=minutes),
    )


def test_assign_worker_starts_a_supervisor_session(rig: AuthRig) -> None:
    command = _assign(W1, assignment=None)
    assert rig.manager.handle_assign_worker(command) is CommandOutcome.STARTED
    current = rig.manager.current()
    assert current is not None
    assert current.method is SessionMethod.SUPERVISOR_ASSIGNED
    assert current.station_assignment_id == command.assignment_id
    assert current.valid_until == command.valid_until
    assert current.ppe_profile_id == MASK
    assert _starts(rig) == [
        SessionStartPayload(
            user_id=W1,
            method=SessionMethod.SUPERVISOR_ASSIGNED,
            station_assignment_id=command.assignment_id,
            ppe_profile_id=MASK,
        )
    ]
    assert _absent(rig.engine.config) == MASK_ABSENT


def test_assign_worker_is_idempotent(rig: AuthRig) -> None:
    command = _assign(assignment=None)
    assert rig.manager.handle_assign_worker(command) is CommandOutcome.STARTED
    assert rig.manager.handle_assign_worker(command) is CommandOutcome.DUPLICATE
    same_assignment = _assign(assignment=command.assignment_id)
    assert rig.manager.handle_assign_worker(same_assignment) is CommandOutcome.DUPLICATE
    # Even after the worker logged out, a repeat does not start it again.
    rig.manager.logout()
    assert rig.manager.handle_assign_worker(command) is CommandOutcome.DUPLICATE
    assert len(_starts(rig)) == 1


def test_assignment_idempotency_survives_a_restart(tmp_path: Path, clock: FakeClock) -> None:
    command = _assign(assignment=None)
    with AuthStateStore.open(tmp_path / "auth.db") as store:
        assert build_rig(clock, store).manager.handle_assign_worker(command) is (
            CommandOutcome.STARTED
        )
    with AuthStateStore.open(tmp_path / "auth.db") as store:
        rig = build_rig(clock, store)
        assert rig.manager.handle_assign_worker(command) is CommandOutcome.DUPLICATE


def test_assignment_expires(rig: AuthRig) -> None:
    command = _assign(minutes=30, assignment=None)
    rig.manager.handle_assign_worker(command)
    session = rig.manager.current()
    assert session is not None
    rig.clock.advance(30 * 60 - 1)
    rig.manager.tick()
    assert rig.manager.current() is not None
    rig.clock.advance(1)
    rig.manager.tick()
    assert rig.manager.current() is None
    (end,) = rig.records.of_type(EventType.SESSION_END)
    assert end.occurred_at == command.valid_until
    assert end.payload == SessionEndPayload(
        session_id=session.session_id, end_reason=SessionEndReason.TIMEOUT
    )


def test_expired_assignment_is_lazily_ended_by_current(rig: AuthRig) -> None:
    _login(rig, "EMP001", "1234")
    rig.manager.handle_assign_worker(_assign(W1, minutes=1, assignment=None))
    rig.clock.advance(120)
    assert rig.manager.current() is None
    assert _absent(rig.engine.config) == STATION_ABSENT


def test_assignment_replaces_a_pin_session(rig: AuthRig) -> None:
    first = _login(rig, "EMP001", "1234").session
    assert rig.manager.handle_assign_worker(_assign(W2, assignment=None)) is (
        CommandOutcome.STARTED
    )
    assert _ends(rig)[0] == SessionEndPayload(
        session_id=first.session_id, end_reason=SessionEndReason.REPLACED
    )
    current = rig.manager.current()
    assert current is not None
    assert current.user_id == W2


def test_assignment_refusals(clock: FakeClock, state: AuthStateStore) -> None:
    rig = build_rig(clock, state)
    expired = _assign(minutes=0, assignment=None)
    assert rig.manager.handle_assign_worker(expired) is CommandOutcome.EXPIRED
    assert rig.manager.handle_assign_worker(expired) is CommandOutcome.DUPLICATE
    unknown = _assign(UUID(int=12345), assignment=None)
    assert rig.manager.handle_assign_worker(unknown) is CommandOutcome.REFUSED
    inactive = _assign(W3, assignment=None)
    assert rig.manager.handle_assign_worker(inactive) is CommandOutcome.REFUSED
    rig.roster.set_bundle(None)
    assert rig.manager.handle_assign_worker(_assign(assignment=None)) is CommandOutcome.REFUSED
    assert rig.records.records == []


# -- end_session command ----------------------------------------------------


def test_end_session_command(rig: AuthRig) -> None:
    session = _login(rig, "EMP001", "1234").session
    other = EndSessionCommand(id=new_uuid7(), session_id=new_uuid7())
    assert rig.manager.handle_end_session(other) is CommandOutcome.NOT_CURRENT
    assert rig.manager.current() is not None
    command = EndSessionCommand(id=new_uuid7(), session_id=session.session_id)
    assert rig.manager.handle_end_session(command) is CommandOutcome.ENDED
    assert rig.manager.current() is None
    assert _ends(rig) == [
        SessionEndPayload(session_id=session.session_id, end_reason=SessionEndReason.LOGOUT)
    ]
    assert _absent(rig.engine.config) == STATION_ABSENT
    assert rig.manager.handle_end_session(command) is CommandOutcome.DUPLICATE
    late = EndSessionCommand(id=new_uuid7(), session_id=session.session_id)
    assert rig.manager.handle_end_session(late) is CommandOutcome.NOT_CURRENT


# -- shutdown, recovery, failures -------------------------------------------


def test_shutdown_ends_without_touching_the_engine(rig: AuthRig) -> None:
    session = _login(rig, "EMP001", "1234").session
    rig.manager.shutdown()
    assert rig.manager.current() is None
    assert _ends(rig)[0].session_id == session.session_id
    assert len(rig.engine.applied_configs) == 1
    rig.manager.shutdown()
    assert len(_ends(rig)) == 1


def test_recover_closes_a_session_left_open_by_a_crash(tmp_path: Path, clock: FakeClock) -> None:
    db = tmp_path / "auth.db"
    with AuthStateStore.open(db) as store:
        session = _login(build_rig(clock, store), "EMP001", "1234").session
    clock.advance(600)
    with AuthStateStore.open(db) as store:
        rig = build_rig(clock, store)
        rig.manager.recover()
        (end,) = rig.records.of_type(EventType.SESSION_END)
        assert end.payload == SessionEndPayload(
            session_id=session.session_id, end_reason=SessionEndReason.TIMEOUT
        )
        assert end.occurred_at == clock.now()
        assert rig.manager.current() is None
        rig.manager.recover()
        assert len(rig.records.records) == 1


def test_recover_purges_old_command_ids(rig: AuthRig, state: AuthStateStore) -> None:
    command = _assign(assignment=None)
    rig.manager.handle_assign_worker(command)
    rig.clock.advance(timedelta(days=8).total_seconds())
    rig.manager.recover()
    assert not state.is_handled(f"cmd:{command.id}")


def test_queue_failure_does_not_block_login(rig: AuthRig) -> None:
    rig.records.fail = True
    result = _login(rig, "EMP001", "1234")
    assert result.outcome is AuthOutcome.SUCCESS
    assert HealthFlag.RECORD_WRITE_FAILED in rig.manager.health_flags()
    rig.records.fail = False
    rig.manager.logout()
    assert HealthFlag.RECORD_WRITE_FAILED not in rig.manager.health_flags()


class BrokenStore(AuthStateStore):
    """Session and command persistence fail (disk error); lockouts still work."""

    def _fail(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def set_open_session(self, row: Any) -> None:  # Any: matches any argument
        self._fail()

    def get_open_session(self) -> None:
        self._fail()

    def is_handled(self, key: str) -> bool:
        self._fail()
        return False

    def mark_handled(self, key: str, at: datetime) -> bool:
        self._fail()
        return False


def test_state_store_failure_is_contained(clock: FakeClock) -> None:
    store = BrokenStore(sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None))
    rig = build_rig(clock, store)
    rig.manager.recover()
    assert rig.manager.health_flags() == {HealthFlag.STATE_STORE_FAILED}
    assert _login(rig, "EMP001", "1234").outcome is AuthOutcome.SUCCESS
    command = _assign(assignment=None)
    assert rig.manager.handle_assign_worker(command) is CommandOutcome.STARTED
    assert rig.manager.handle_assign_worker(command) is CommandOutcome.DUPLICATE
    store.close()


def test_store_rejects_a_newer_database(tmp_path: Path) -> None:
    db = tmp_path / "auth.db"
    AuthStateStore.open(db).close()
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        AuthStateStore.open(db)


def test_end_cause_maps_every_member_to_the_schema() -> None:
    allowed = {"logout", "timeout", "shift_end", "replaced"}
    assert {cause.wire_reason.value for cause in EndCause} <= allowed
    assert EndCause.ASSIGNMENT_EXPIRED.wire_reason is SessionEndReason.TIMEOUT
    assert EndCause.REPLACED.wire_reason is SessionEndReason.REPLACED


def test_explicit_end_cause(rig: AuthRig) -> None:
    assert rig.manager.end(EndCause.SHIFT_END) is False
    _login(rig, "EMP001", "1234")
    assert rig.manager.end(EndCause.SHIFT_END) is True
    assert _ends(rig)[0].end_reason is SessionEndReason.SHIFT_END


# -- time bases (contract 6 "Clock"; 2.11 fix) ---------------------------------------


class _Offset:
    """A settable server offset (what `SharedClockOffset` reads from the file)."""

    def __init__(self, ms: int = 0) -> None:
        self.ms = ms

    def offset_ms(self) -> int:
        return self.ms


def _corrected_rig(clock: FakeClock, state: AuthStateStore, offset: _Offset) -> AuthRig:
    rig = build_rig(clock, state)
    rig.manager = SessionManager(
        registry=rig.registry,
        roster=rig.roster,
        records=rig.records,
        engine=rig.engine,
        state=state,
        clock=clock,
        utc=StampingClock(clock, offset),
    )
    return rig


def test_session_records_carry_corrected_utc_and_the_session_keeps_device_time(
    clock: FakeClock, state: AuthStateStore
) -> None:
    """Records are stamped with server-corrected UTC; `started_at` (pushed to
    the engine child's capture gate) stays in device time, the gate's base."""
    offset = _Offset(5000)
    rig = _corrected_rig(clock, state, offset)
    session = _login(rig, "EMP001", "1234").session
    assert session is not None
    assert session.started_at == clock.now()  # device time
    (start,) = rig.records.of_type(EventType.SESSION_START)
    assert start.occurred_at == clock.now() + timedelta(seconds=5)
    row = state.get_open_session()
    assert row is not None and row.started_at == clock.now()
    offset.ms = -2000
    clock.advance(10)
    rig.manager.logout()
    (end,) = rig.records.of_type(EventType.SESSION_END)
    assert end.occurred_at == clock.now() - timedelta(seconds=2)


def test_assignment_expiry_is_judged_on_server_time(
    clock: FakeClock, state: AuthStateStore
) -> None:
    """`valid_until` is a server time: the PC clock 5 s behind must not keep
    an assignment open 5 s too long."""
    rig = _corrected_rig(clock, state, _Offset(5000))
    command = _assign(minutes=30, assignment=None)
    assert rig.manager.handle_assign_worker(command) is CommandOutcome.STARTED
    rig.clock.advance(30 * 60 - 5)  # device time is 5 s short of valid_until
    rig.manager.tick()
    assert rig.manager.current() is None
    (end,) = rig.records.of_type(EventType.SESSION_END)
    assert end.occurred_at == command.valid_until
    # Already expired on server time: refused at arrival.
    late = _assign(minutes=30, assignment=None)
    assert rig.manager.handle_assign_worker(late) is CommandOutcome.EXPIRED
