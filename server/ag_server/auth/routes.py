"""Console sign-in routes and minimal pages (the full layout comes in 3.9).

Routes: `GET/POST /auth/login`, `GET/POST /auth/mfa/verify`,
`GET/POST /auth/mfa/enroll`, `POST /auth/logout`, and a placeholder console
home `GET /`. The login routes are public (`x-ag-public`); everything else
depends on `pending_session` or `current_session`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from ag_shared.i18n import Translator, install_jinja
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ag_server.auth.deps import (
    LOGIN_PATH,
    SAFE_METHODS,
    STEP_PATHS,
    CsrfRejectedError,
    DbSession,
    MfaPendingError,
    NotAuthenticatedError,
    Runtime,
    current_session,
    pending_session,
)
from ag_server.auth.service import ConsoleAuth, ConsoleSession, MfaOutcome, NextStep
from ag_server.auth.sessions import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    clear_host_cookie,
    new_token,
    set_host_cookie,
    tokens_equal,
)

PUBLIC: Final = {"x-ag-public": True}
_TEMPLATES: Final = Path(__file__).parent / "templates"
# The console has no inline scripts or styles and no third-party assets.
_CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_MAX_FIELD: Final = 1024

Pending = Annotated[ConsoleSession, Depends(pending_session)]
Current = Annotated[ConsoleSession, Depends(current_session)]


class Pages:
    """Renders the auth templates with the externalized strings."""

    def __init__(self, translator: Translator) -> None:
        self.env = Environment(
            loader=FileSystemLoader(_TEMPLATES),
            autoescape=select_autoescape(default=True, default_for_string=True),
        )
        install_jinja(self.env, translator)
        self.t = translator.t

    def render(self, template: str, status: int = 200, **context: object) -> HTMLResponse:
        html = self.env.get_template(template).render(**context)
        response = HTMLResponse(html, status_code=status)
        secure_headers(response)
        return response


def secure_headers(response: Response) -> None:
    """Auth pages and redirects are never cached and never framed."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"


def _redirect(path: str) -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    secure_headers(response)
    return response


def _peer(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _pages(request: Request) -> Pages:
    pages: Pages = request.app.state.auth_pages
    return pages


PagesDep = Annotated[Pages, Depends(_pages)]


def _login_page(pages: Pages, *, status: int = 200, error: str | None = None) -> HTMLResponse:
    token = new_token()
    response = pages.render("login.html", status, csrf_token=token, error=error)
    set_host_cookie(response, LOGIN_CSRF_COOKIE, token)
    return response


router = APIRouter()


@router.get("/auth/login", openapi_extra=PUBLIC, response_class=HTMLResponse)
def login_form(pages: PagesDep) -> HTMLResponse:
    """The sign-in form with a fresh double-submit CSRF token."""
    return _login_page(pages)


@router.post("/auth/login", openapi_extra=PUBLIC, response_model=None)
def login_submit(
    request: Request,
    db: DbSession,
    runtime: Runtime,
    pages: PagesDep,
    username: Annotated[str, Form(max_length=_MAX_FIELD)] = "",
    password: Annotated[str, Form(max_length=_MAX_FIELD)] = "",
    csrf_token: Annotated[str, Form(max_length=_MAX_FIELD)] = "",
) -> Response:
    """Check the password. Every failure gets the same page, status and message."""
    if not tokens_equal(request.cookies.get(LOGIN_CSRF_COOKIE) or "", csrf_token):
        raise CsrfRejectedError
    result = ConsoleAuth(runtime, db).login(
        username,
        password,
        ip=_peer(request),
        user_agent=_ua(request),
        replaces=request.cookies.get(SESSION_COOKIE),
    )
    if not result.ok or result.token is None:
        return _login_page(pages, status=401, error=pages.t("console.auth.failed"))
    response = _redirect(STEP_PATHS[result.next_step])
    set_host_cookie(response, SESSION_COOKIE, result.token)
    clear_host_cookie(response, LOGIN_CSRF_COOKIE)
    return response


@router.get("/auth/mfa/verify", response_model=None)
def mfa_verify_form(session: Pending, pages: PagesDep) -> Response:
    if session.next_step is not NextStep.MFA_VERIFY:
        return _redirect(STEP_PATHS[session.next_step])
    return pages.render("mfa_verify.html", csrf_token=session.csrf_token, error=None)


@router.post("/auth/mfa/verify", response_model=None)
def mfa_verify_submit(
    request: Request,
    session: Pending,
    db: DbSession,
    runtime: Runtime,
    pages: PagesDep,
    code: Annotated[str, Form(max_length=64)] = "",
) -> Response:
    """A TOTP or recovery code. Success rotates the session id."""
    if session.next_step is not NextStep.MFA_VERIFY:
        return _redirect(STEP_PATHS[session.next_step])
    result = ConsoleAuth(runtime, db).verify_mfa(
        session, code, ip=_peer(request), user_agent=_ua(request)
    )
    if result.outcome is MfaOutcome.OK and result.token is not None:
        response = _redirect(STEP_PATHS[NextStep.HOME])
        set_host_cookie(response, SESSION_COOKIE, result.token)
        return response
    if result.outcome is MfaOutcome.LOCKED:
        response = _redirect(LOGIN_PATH)
        clear_host_cookie(response, SESSION_COOKIE)
        return response
    return pages.render(
        "mfa_verify.html",
        400,
        csrf_token=session.csrf_token,
        error=pages.t("console.auth.mfa.failed"),
    )


@router.get("/auth/mfa/enroll", response_model=None)
def mfa_enroll_form(
    request: Request, session: Pending, db: DbSession, runtime: Runtime, pages: PagesDep
) -> Response:
    """A new TOTP secret on every visit, so a secret seen by someone else is replaced."""
    if session.next_step is not NextStep.MFA_ENROLL:
        return _redirect(STEP_PATHS[session.next_step])
    enrollment = ConsoleAuth(runtime, db).begin_enrollment(session, ip=_peer(request))
    if enrollment is None:
        return _redirect(STEP_PATHS[NextStep.MFA_VERIFY])
    return pages.render(
        "mfa_enroll.html",
        csrf_token=session.csrf_token,
        secret=enrollment.secret,
        uri=enrollment.uri,
        error=None,
    )


@router.post("/auth/mfa/enroll", response_model=None)
def mfa_enroll_submit(
    request: Request,
    session: Pending,
    db: DbSession,
    runtime: Runtime,
    pages: PagesDep,
    code: Annotated[str, Form(max_length=64)] = "",
) -> Response:
    """Confirm the secret; show the recovery codes once, with the rotated session cookie."""
    if session.next_step is not NextStep.MFA_ENROLL:
        return _redirect(STEP_PATHS[session.next_step])
    auth = ConsoleAuth(runtime, db)
    done = auth.complete_enrollment(session, code, ip=_peer(request), user_agent=_ua(request))
    if done is None:
        pending = auth.pending_enrollment(session)
        if pending is None:
            return _redirect(STEP_PATHS[NextStep.MFA_ENROLL])
        return pages.render(
            "mfa_enroll.html",
            400,
            csrf_token=session.csrf_token,
            secret=pending.secret,
            uri=pending.uri,
            error=pages.t("console.auth.mfa.failed"),
        )
    response = pages.render("mfa_recovery.html", codes=done.recovery_codes)
    set_host_cookie(response, SESSION_COOKIE, done.token)
    return response


@router.post("/auth/logout", response_model=None)
def logout(request: Request, session: Pending, db: DbSession, runtime: Runtime) -> Response:
    ConsoleAuth(runtime, db).logout(session, ip=_peer(request))
    response = _redirect(LOGIN_PATH)
    clear_host_cookie(response, SESSION_COOKIE)
    return response


@router.get("/", response_class=HTMLResponse)
def console_home(session: Current, pages: PagesDep) -> HTMLResponse:
    """Placeholder console home until the layout of task 3.9."""
    return pages.render("home.html", csrf_token=session.csrf_token, name=session.display_name)


def _wants_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _deny(request: Request, status: int, location: str) -> Response:
    """Redirect page loads; refuse anything else (HTMX gets `HX-Redirect`)."""
    if request.method in SAFE_METHODS and not _wants_htmx(request):
        return _redirect(location)
    response = PlainTextResponse(str(status), status_code=status)
    secure_headers(response)
    if _wants_htmx(request):
        response.headers["HX-Redirect"] = location
    return response


def install_auth(app: FastAPI, translator: Translator) -> None:
    """Add the auth routes, pages and the exception handlers that turn auth errors
    into redirects or 401/403 responses."""
    app.state.auth_pages = Pages(translator)
    app.include_router(router)

    async def not_authenticated(request: Request, _exc: Exception) -> Response:
        response = _deny(request, 401, LOGIN_PATH)
        if request.cookies.get(SESSION_COOKIE) is not None:
            clear_host_cookie(response, SESSION_COOKIE)
        return response

    async def mfa_pending(request: Request, exc: Exception) -> Response:
        step = exc.step if isinstance(exc, MfaPendingError) else NextStep.MFA_VERIFY
        return _deny(request, 403, STEP_PATHS[step])

    async def csrf_rejected(request: Request, _exc: Exception) -> Response:
        response = PlainTextResponse(
            app.state.auth_pages.t("console.auth.csrf_failed"), status_code=403
        )
        secure_headers(response)
        return response

    app.add_exception_handler(NotAuthenticatedError, not_authenticated)
    app.add_exception_handler(MfaPendingError, mfa_pending)
    app.add_exception_handler(CsrfRejectedError, csrf_rejected)
