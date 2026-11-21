"""Console authentication (task 3.4, security-privacy 3).

- `AuthProvider` / `LocalAuthProvider`: credential checks (argon2id, dummy
  hash for unknown users).
- `ConsoleAuth`: sign-in, lockout, TOTP MFA and recovery codes, server-side
  sessions with idle and absolute timeouts, rotation at sign-in and MFA.
- `current_session` / `pending_session`: FastAPI dependencies (CSRF-checked).
  Task 3.5 wraps `current_session` with permissions.
"""

from ag_server.auth.deps import current_session, pending_session
from ag_server.auth.provider import AuthProvider, CredentialCheck, LocalAuthProvider
from ag_server.auth.service import (
    AuthPolicy,
    AuthRuntime,
    ConsoleAuth,
    ConsoleSession,
    NextStep,
)

__all__ = [
    "AuthPolicy",
    "AuthProvider",
    "AuthRuntime",
    "ConsoleAuth",
    "ConsoleSession",
    "CredentialCheck",
    "LocalAuthProvider",
    "NextStep",
    "current_session",
    "pending_session",
]
