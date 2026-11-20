"""Worker login and sessions (PLAN 2.4; D7; security-privacy section 3).

- `LoginMethod` / `LoginMethodRegistry`: the plug-in interface. The only
  built-in method is `PinLoginMethod` (employee ID + PIN against the cached
  roster, argon2id, lockout per employee ID on this station).
- `SessionManager`: one session per station, supervisor assignments, the
  `end_session` command, session records and the PPE override.
"""

from ag_workstation.auth.lockout import (
    FAILURE_WINDOW,
    LOCK_DURATION,
    MAX_FAILURES,
    FailureResult,
    LockStatus,
    LoginThrottle,
)
from ag_workstation.auth.messages import MessageKey, text
from ag_workstation.auth.methods import (
    AuthOutcome,
    AuthResult,
    LoginMethod,
    LoginMethodRegistry,
    LoginRequest,
)
from ag_workstation.auth.pin import (
    PIN_METHOD_NAME,
    PinCredentials,
    PinLoginMethod,
    lock_minutes,
    normalize_employee_id,
    valid_pin,
)
from ag_workstation.auth.roster import RosterSource, StaticRosterSource, roster_entry_by_user
from ag_workstation.auth.sessions import (
    CommandOutcome,
    CurrentSession,
    EndCause,
    HealthFlag,
    LoginResult,
    RecordSink,
    SessionManager,
)
from ag_workstation.auth.state import AuthStateStore, LockoutRow, OpenSessionRow

__all__ = [
    "FAILURE_WINDOW",
    "LOCK_DURATION",
    "MAX_FAILURES",
    "PIN_METHOD_NAME",
    "AuthOutcome",
    "AuthResult",
    "AuthStateStore",
    "CommandOutcome",
    "CurrentSession",
    "EndCause",
    "FailureResult",
    "HealthFlag",
    "LockStatus",
    "LockoutRow",
    "LoginMethod",
    "LoginMethodRegistry",
    "LoginRequest",
    "LoginResult",
    "LoginThrottle",
    "MessageKey",
    "OpenSessionRow",
    "PinCredentials",
    "PinLoginMethod",
    "RecordSink",
    "RosterSource",
    "SessionManager",
    "StaticRosterSource",
    "lock_minutes",
    "normalize_employee_id",
    "roster_entry_by_user",
    "text",
    "valid_pin",
]
