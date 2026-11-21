"""FastAPI dependencies for console sessions.

- `current_session`: a complete session (MFA done where owed). Task 3.5 wraps
  it with permission checks. Use it on every console route.
- `pending_session`: any live session, also one that still owes MFA. Only
  the MFA routes and logout use it.

Both check the CSRF token on every state-changing request, so a route that
depends on either cannot forget it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Final

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ag_server.auth.service import AuthRuntime, ConsoleAuth, ConsoleSession, NextStep
from ag_server.auth.sessions import CSRF_FIELD, CSRF_HEADER, SESSION_COOKIE, tokens_equal

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
STEP_PATHS: Final = {
    NextStep.HOME: "/",
    NextStep.MFA_VERIFY: "/auth/mfa/verify",
    NextStep.MFA_ENROLL: "/auth/mfa/enroll",
}
LOGIN_PATH: Final = "/auth/login"


class NotAuthenticatedError(Exception):
    """No live session: GET goes to the login page, anything else gets 401."""


class MfaPendingError(Exception):
    """The session still owes MFA: GET goes to the MFA step, anything else gets 403."""

    def __init__(self, step: NextStep) -> None:
        super().__init__(step.value)
        self.step = step


class CsrfRejectedError(Exception):
    """A state-changing request without the session's CSRF token: 403."""


def auth_runtime(request: Request) -> AuthRuntime:
    runtime: AuthRuntime = request.app.state.auth
    return runtime


def db_session(request: Request) -> Iterator[Session]:
    """One SQLAlchemy session per request; callers manage their own transactions."""
    with Session(request.app.state.engine, expire_on_commit=False) as db:
        yield db


DbSession = Annotated[Session, Depends(db_session)]
Runtime = Annotated[AuthRuntime, Depends(auth_runtime)]


async def check_csrf(request: Request, expected: str) -> None:
    """Raise `CsrfRejectedError` unless an unsafe request carries `expected`."""
    if request.method in SAFE_METHODS:
        return
    given = request.headers.get(CSRF_HEADER)
    if given is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            value = (await request.form()).get(CSRF_FIELD)
            given = value if isinstance(value, str) else None
    if not tokens_equal(expected, given):
        raise CsrfRejectedError


async def pending_session(request: Request, db: DbSession, runtime: Runtime) -> ConsoleSession:
    """Any live session (complete or still owing MFA), CSRF-checked."""
    session = ConsoleAuth(runtime, db).load(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise NotAuthenticatedError
    await check_csrf(request, session.csrf_token)
    return session


async def current_session(
    session: Annotated[ConsoleSession, Depends(pending_session)],
) -> ConsoleSession:
    """A complete console session. Partial sessions are sent to their MFA step."""
    if not session.complete:
        raise MfaPendingError(session.next_step)
    return session
