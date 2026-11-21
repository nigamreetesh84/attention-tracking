"""Console authentication end to end on a real PostgreSQL (task 3.4).

Covers login paths, lockout, mandatory MFA, enrollment, recovery codes,
session expiry and rotation, CSRF, cookie flags, no user enumeration and
secret-free audit entries. The app connects as the limited app role.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import pyotp
import pytest
from ag_server.app import create_app
from ag_server.auth import ConsoleAuth, ConsoleSession, current_session
from ag_server.auth.mfa import generate_recovery_codes, new_pending_state, open_mfa, seal_mfa
from ag_server.auth.passwords import PolicyViolation
from ag_server.auth.sessions import LOGIN_CSRF_COOKIE, SESSION_COOKIE
from ag_server.crypto import KeyRing
from ag_server.migrations.runner import upgrade
from ag_server.models import AuditLog, Role, User, WebSession
from ag_server.settings import Settings, load_settings
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.orm import Session

PASSWORD = "violet-tractor-87-lantern"  # noqa: S105 - test account password
BASE = "https://testserver"
PEER = ("203.0.113.7", 50000)
START = datetime(2026, 11, 22, 9, 0, 3, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class CountingHasher(PasswordHasher):
    """Cheap argon2id parameters for tests; counts verifications."""

    def __init__(self) -> None:
        super().__init__(time_cost=1, memory_cost=64, parallelism=1)
        self.verifications = 0

    def verify(self, hash: str | bytes, password: str | bytes) -> Literal[True]:
        self.verifications += 1
        return super().verify(hash, password)


@pytest.fixture(scope="module")
def auth_dsn(pg_database_factory: Callable[[], str], pg_app_role: str) -> str:
    dsn = pg_database_factory()
    upgrade(dsn, "head", app_role=pg_app_role)
    return dsn


@pytest.fixture(scope="module")
def owner_engine(auth_dsn: str) -> Iterator[Engine]:
    engine = create_engine(auth_dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def settings(auth_dsn: str, as_app_role: Callable[[str], str]) -> Settings:
    return load_settings({"AG_ENV": "test", "AG_DATABASE_URL": as_app_role(auth_dsn)})


@dataclass
class Env:
    app: FastAPI
    client: TestClient
    clock: FakeClock
    hasher: CountingHasher
    ring: KeyRing
    engine: Engine

    def new_client(self) -> TestClient:
        return TestClient(self.app, base_url=BASE, follow_redirects=False, client=PEER)


@pytest.fixture
def env(settings: Settings, owner_engine: Engine) -> Iterator[Env]:
    clock = FakeClock()
    hasher = CountingHasher()
    app = create_app(settings, clock=clock, password_hasher=hasher)

    @app.get("/test/whoami")
    def whoami(s: Annotated[ConsoleSession, Depends(current_session)]) -> dict[str, object]:
        return {
            "user_id": str(s.user_id),
            "role": s.role,
            "permissions": sorted(s.permissions),
            "site_id": None if s.site_id is None else str(s.site_id),
            "mfa_passed": s.mfa_passed,
        }

    @app.post("/test/change")
    def change(s: Annotated[ConsoleSession, Depends(current_session)]) -> dict[str, str]:
        return {"ok": str(s.user_id)}

    with TestClient(app, base_url=BASE, follow_redirects=False, client=PEER) as client:
        yield Env(app, client, clock, hasher, KeyRing.from_settings(settings), owner_engine)


@dataclass
class Account:
    user_id: uuid.UUID
    username: str
    totp_secret: str | None = None
    recovery_codes: tuple[str, ...] = ()


def make_user(
    env: Env,
    role: str = "admin",
    *,
    mfa: bool = False,
    active: bool = True,
    password: str | None = PASSWORD,
) -> Account:
    username = f"u{secrets.token_hex(5)}"
    with Session(env.engine) as db, db.begin():
        role_id = db.execute(select(Role.id).where(Role.name == role)).scalar_one()
        user = User(
            role_id=role_id,
            display_name=f"User {username}",
            username=username,
            password_hash=None if password is None else env.hasher.hash(password),
            is_active=active,
        )
        db.add(user)
        db.flush()
        account = Account(user.id, username)
        if mfa:
            codes = generate_recovery_codes()
            state = new_pending_state().confirm().with_recovery_codes(codes)
            user.totp_secret_encrypted = seal_mfa(env.ring, user.id, state)
            user.mfa_enabled = True
            account.totp_secret = state.secret
            account.recovery_codes = tuple(codes)
    return account


CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
ERROR_BLOCK = re.compile(r'<p class="error"[^>]*>(.*?)</p>', re.S)


def form_token(response: Response) -> str:
    match = CSRF_FIELD.search(response.text)
    assert match, "page has no CSRF field"
    return match.group(1)


def post_login(client: TestClient, username: str, password: str) -> Response:
    page = client.get("/auth/login")
    assert page.status_code == 200
    r: Response = client.post(
        "/auth/login",
        data={"username": username, "password": password, "csrf_token": form_token(page)},
    )
    return r


def totp_now(env: Env, account: Account) -> str:
    assert account.totp_secret
    return pyotp.TOTP(account.totp_secret).at(env.clock())


def post_mfa(client: TestClient, code: str) -> Response:
    page = client.get("/auth/mfa/verify")
    assert page.status_code == 200
    r: Response = client.post(
        "/auth/mfa/verify", data={"code": code, "csrf_token": form_token(page)}
    )
    return r


def full_login(env: Env, account: Account, client: TestClient | None = None) -> TestClient:
    c = client or env.client
    r = post_login(c, account.username, PASSWORD)
    assert r.status_code == 303
    if account.totp_secret:
        assert r.headers["location"] == "/auth/mfa/verify"
        r = post_mfa(c, totp_now(env, account))
        assert r.status_code == 303, r.text
    assert r.headers["location"] == "/"
    return c


def session_cookie(client: TestClient) -> str | None:
    value: str | None = client.cookies.get(SESSION_COOKIE)
    return value


def audit_rows(env: Env, user_id: uuid.UUID | None = None) -> list[AuditLog]:
    with Session(env.engine) as db:
        q = select(AuditLog).order_by(AuditLog.id)
        if user_id is not None:
            q = q.where(AuditLog.object_id == str(user_id))
        return list(db.execute(q).scalars())


def actions(env: Env, user_id: uuid.UUID) -> list[str]:
    return [r.action for r in audit_rows(env, user_id)]


def set_cookie_headers(r: Response) -> list[str]:
    return r.headers.get_list("set-cookie")


# --- login paths -----------------------------------------------------------


def test_login_page_is_public_and_sets_a_login_csrf_cookie(env: Env) -> None:
    r = env.client.get("/auth/login")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]
    cookie = next(h for h in set_cookie_headers(r) if h.startswith(LOGIN_CSRF_COOKIE))
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=strict" in low
    assert "path=/" in low and "domain" not in low


def test_supervisor_without_mfa_signs_in_and_reaches_pages(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    r = env.client.get("/test/whoami")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == str(sup.user_id)
    assert body["role"] == "supervisor"
    assert "events.review" in body["permissions"]
    assert env.client.get("/").status_code == 200
    assert "auth.login" in actions(env, sup.user_id)


def test_session_cookie_flags(env: Env) -> None:
    sup = make_user(env, "supervisor")
    r = post_login(env.client, sup.username, PASSWORD)
    cookie = next(h for h in set_cookie_headers(r) if h.startswith(SESSION_COOKIE + "="))
    assert SESSION_COOKIE.startswith("__Host-")
    parts = [p.strip().lower() for p in cookie.split(";")]
    assert "httponly" in parts
    assert "secure" in parts
    assert "samesite=strict" in parts
    assert "path=/" in parts
    assert not any(p.startswith("domain") for p in parts)
    # A session cookie: no Max-Age/Expires, the server enforces the lifetime.
    assert not any(p.startswith(("max-age", "expires")) for p in parts)


def test_web_session_stores_only_the_hash_of_the_cookie(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    token = session_cookie(env.client)
    assert token
    with Session(env.engine) as db:
        rows = db.execute(select(WebSession).where(WebSession.user_id == sup.user_id)).scalars()
        ids = [r.id for r in rows]
    assert hashlib.sha256(token.encode()).digest() in ids
    assert all(token.encode() not in i for i in ids)


def test_unauthenticated_requests_are_sent_to_login(env: Env) -> None:
    r = env.client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"
    assert env.client.get("/test/whoami").status_code == 303
    assert env.client.post("/test/change").status_code == 401


def test_bogus_session_cookie_is_ignored(env: Env) -> None:
    env.client.cookies.set(SESSION_COOKIE, "forged")
    assert env.client.get("/").headers["location"] == "/auth/login"


# --- MFA is mandatory for Admins ----------------------------------------------


def _all_console_get_paths(app: FastAPI) -> list[str]:
    # The OpenAPI document lists every route, including those of included
    # routers (FastAPI keeps those out of `app.routes`).
    paths = []
    for path, ops in app.openapi()["paths"].items():
        op = ops.get("get")
        public = op is not None and op.get("x-ag-access", {}).get("kind") == "public"
        if op is None or public or path.startswith("/auth/") or "{" in path:
            continue
        paths.append(path)
    return paths


def test_admin_without_mfa_cannot_reach_any_page_before_enrolling(env: Env) -> None:
    admin = make_user(env, "admin")
    r = post_login(env.client, admin.username, PASSWORD)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/mfa/enroll"
    paths = _all_console_get_paths(env.app)
    assert "/" in paths and "/test/whoami" in paths
    for path in paths:
        r = env.client.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/auth/mfa/enroll", path
    # Unsafe methods are refused outright, even with a valid CSRF token.
    page = env.client.get("/auth/mfa/enroll")
    r = env.client.post("/test/change", headers={"X-CSRF-Token": form_token(page)})
    assert r.status_code == 403
    # The verify page is not for a user who has no MFA yet.
    assert env.client.get("/auth/mfa/verify").headers["location"] == "/auth/mfa/enroll"


def test_admin_enrollment_flow(env: Env) -> None:
    admin = make_user(env, "admin")
    post_login(env.client, admin.username, PASSWORD)
    partial = session_cookie(env.client)
    page = env.client.get("/auth/mfa/enroll")
    assert page.status_code == 200
    assert "no-store" in page.headers["cache-control"]
    secret = re.search(r'data-secret="([A-Z2-7]+)"', page.text)
    assert secret
    totp = pyotp.TOTP(secret.group(1))

    bad = env.client.post(
        "/auth/mfa/enroll", data={"code": "000000", "csrf_token": form_token(page)}
    )
    assert bad.status_code == 400
    assert env.client.get("/").status_code == 303

    page = env.client.get("/auth/mfa/enroll")
    secret2 = re.search(r'data-secret="([A-Z2-7]+)"', page.text)
    assert secret2
    totp = pyotp.TOTP(secret2.group(1))
    done = env.client.post(
        "/auth/mfa/enroll", data={"code": totp.at(env.clock()), "csrf_token": form_token(page)}
    )
    assert done.status_code == 200, done.text
    assert "no-store" in done.headers["cache-control"]
    codes = re.findall(r"<code class=\"recovery\">([a-z2-9-]+)</code>", done.text)
    assert len(codes) == 10

    # Session rotated at MFA; the old (partial) token is dead.
    full = session_cookie(env.client)
    assert full and full != partial
    assert env.client.get("/test/whoami").json()["mfa_passed"] is True
    stale = env.new_client()
    stale.cookies.set(SESSION_COOKIE, partial or "")
    assert stale.get("/").headers["location"] == "/auth/login"

    with Session(env.engine) as db:
        user = db.get(User, admin.user_id)
        assert user and user.mfa_enabled and user.totp_secret_encrypted
        assert secret2.group(1).encode() not in user.totp_secret_encrypted
        state = open_mfa(env.ring, user.id, user.totp_secret_encrypted)
        assert state.confirmed and state.secret == secret2.group(1)
        assert len(state.recovery_hashes) == 10
    assert actions(env, admin.user_id).count("auth.mfa_enrolled") == 1


def test_enrollment_is_refused_once_mfa_is_enabled(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    post_login(env.client, admin.username, PASSWORD)
    r = env.client.get("/auth/mfa/enroll")
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/mfa/verify"


def test_admin_with_mfa_must_verify_and_session_rotates(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    r = post_login(env.client, admin.username, PASSWORD)
    assert r.headers["location"] == "/auth/mfa/verify"
    partial = session_cookie(env.client)
    assert env.client.get("/").headers["location"] == "/auth/mfa/verify"
    assert env.client.get("/test/whoami").status_code == 303

    r = post_mfa(env.client, totp_now(env, admin))
    assert r.status_code == 303 and r.headers["location"] == "/"
    full = session_cookie(env.client)
    assert full and full != partial
    assert env.client.get("/").status_code == 200
    assert {"auth.login", "auth.mfa_verified"} <= set(actions(env, admin.user_id))


def test_supervisor_who_enabled_mfa_must_verify(env: Env) -> None:
    sup = make_user(env, "supervisor", mfa=True)
    r = post_login(env.client, sup.username, PASSWORD)
    assert r.headers["location"] == "/auth/mfa/verify"
    assert env.client.get("/").headers["location"] == "/auth/mfa/verify"


def test_wrong_totp_is_rejected_generically(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    post_login(env.client, admin.username, PASSWORD)
    r = post_mfa(env.client, "000000")
    assert r.status_code == 400
    assert env.client.get("/").headers["location"] == "/auth/mfa/verify"
    assert "auth.mfa_failure" in actions(env, admin.user_id)


def test_totp_code_cannot_be_replayed(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    code = totp_now(env, admin)
    post_login(env.client, admin.username, PASSWORD)
    assert post_mfa(env.client, code).status_code == 303
    other = env.new_client()
    post_login(other, admin.username, PASSWORD)
    assert post_mfa(other, code).status_code == 400


def test_recovery_code_signs_in_once(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    code = admin.recovery_codes[0]
    post_login(env.client, admin.username, PASSWORD)
    assert post_mfa(env.client, code.upper()).status_code == 303
    assert env.client.get("/").status_code == 200

    again = env.new_client()
    post_login(again, admin.username, PASSWORD)
    assert post_mfa(again, code).status_code == 400
    assert post_mfa(again, admin.recovery_codes[1]).status_code == 303

    rows = audit_rows(env, admin.user_id)
    used = [r for r in rows if r.action == "auth.mfa_verified"]
    assert [r.detail["method"] for r in used] == ["recovery_code", "recovery_code"]
    assert [r.detail["remaining"] for r in used] == [9, 8]


def test_role_promotion_forces_mfa_on_the_next_request(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    assert env.client.get("/").status_code == 200
    with Session(env.engine) as db, db.begin():
        admin_role = db.execute(select(Role.id).where(Role.name == "admin")).scalar_one()
        db.execute(update(User).where(User.id == sup.user_id).values(role_id=admin_role))
    assert env.client.get("/").headers["location"] == "/auth/mfa/enroll"


# --- no user enumeration ---------------------------------------------------------


def _error_text(r: Response) -> str:
    match = ERROR_BLOCK.search(r.text)
    assert match, r.text
    return match.group(1).strip()


def test_failures_look_the_same_for_every_cause(env: Env) -> None:
    good = make_user(env, "supervisor")
    inactive = make_user(env, "supervisor", active=False)
    worker = make_user(env, "workstation_user")
    no_password = make_user(env, "supervisor", password=None)
    locked = make_user(env, "supervisor")
    with Session(env.engine) as db, db.begin():
        db.execute(
            update(User)
            .where(User.id == locked.user_id)
            .values(locked_until=START + timedelta(minutes=10))
        )
    cases = {
        "unknown user": post_login(env.new_client(), "nobody-" + secrets.token_hex(4), PASSWORD),
        "wrong password": post_login(env.new_client(), good.username, PASSWORD + "x"),
        "inactive": post_login(env.new_client(), inactive.username, PASSWORD),
        "workstation user": post_login(env.new_client(), worker.username, PASSWORD),
        "no password": post_login(env.new_client(), no_password.username, PASSWORD),
        "locked": post_login(env.new_client(), locked.username, PASSWORD),
    }
    statuses = {name: r.status_code for name, r in cases.items()}
    texts = {name: _error_text(r) for name, r in cases.items()}
    assert set(statuses.values()) == {401}, statuses
    assert len(set(texts.values())) == 1, texts
    for r in cases.values():
        assert not any(h.startswith(SESSION_COOKIE + "=") for h in set_cookie_headers(r))


def test_every_failure_path_runs_one_password_verification(env: Env) -> None:
    good = make_user(env, "supervisor")
    inactive = make_user(env, "supervisor", active=False)
    worker = make_user(env, "workstation_user")
    no_password = make_user(env, "supervisor", password=None)
    for username, pw in [
        ("nobody-" + secrets.token_hex(4), PASSWORD),
        (good.username, "wrong-password-123"),
        (inactive.username, PASSWORD),
        (worker.username, PASSWORD),
        (no_password.username, PASSWORD),
        (good.username, PASSWORD),
    ]:
        before = env.hasher.verifications
        post_login(env.new_client(), username, pw)
        assert env.hasher.verifications - before == 1, username


def test_unknown_user_failure_is_audited_without_the_typed_name(env: Env) -> None:
    name = "ghost-" + secrets.token_hex(6)
    post_login(env.new_client(), name, "Typed-Secret-Password-1")
    rows = [r for r in audit_rows(env) if r.action == "auth.login_failure"]
    last = rows[-1]
    assert last.object_id is None and last.actor_user_id is None
    assert last.detail == {"reason": "unknown_user"}
    assert name not in str(last.detail)


# --- lockout -------------------------------------------------------------------


def test_five_failures_lock_the_account_for_fifteen_minutes(env: Env) -> None:
    sup = make_user(env, "supervisor")
    for _ in range(5):
        assert post_login(env.new_client(), sup.username, "wrong-password-123").status_code == 401
    assert post_login(env.new_client(), sup.username, PASSWORD).status_code == 401
    acts = actions(env, sup.user_id)
    assert acts.count("auth.lockout") == 1
    with Session(env.engine) as db:
        user = db.get(User, sup.user_id)
        assert user and user.locked_until == START + timedelta(minutes=15)

    env.clock.advance(minutes=14, seconds=59)
    assert post_login(env.new_client(), sup.username, PASSWORD).status_code == 401
    env.clock.advance(seconds=1)
    assert post_login(env.new_client(), sup.username, PASSWORD).status_code == 303
    with Session(env.engine) as db:
        user = db.get(User, sup.user_id)
        assert user and user.failed_attempts == 0 and user.locked_until is None


def test_success_resets_the_failure_counter(env: Env) -> None:
    sup = make_user(env, "supervisor")
    for _ in range(4):
        post_login(env.new_client(), sup.username, "wrong-password-123")
    assert post_login(env.new_client(), sup.username, PASSWORD).status_code == 303
    for _ in range(4):
        post_login(env.new_client(), sup.username, "wrong-password-123")
    assert post_login(env.new_client(), sup.username, PASSWORD).status_code == 303


def test_mfa_failures_count_towards_lockout_and_end_the_partial_session(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    post_login(env.client, admin.username, PASSWORD)
    for _ in range(4):
        assert post_mfa(env.client, "000000").status_code == 400
    page = env.client.get("/auth/mfa/verify")
    r = env.client.post("/auth/mfa/verify", data={"code": "000000", "csrf_token": form_token(page)})
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    assert env.client.get("/").headers["location"] == "/auth/login"
    assert "auth.lockout" in actions(env, admin.user_id)
    # Locked: even the right password and code do not get in.
    assert post_login(env.new_client(), admin.username, PASSWORD).status_code == 401


# --- session expiry and rotation -------------------------------------------------


def test_idle_timeout(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    for _ in range(3):
        env.clock.advance(minutes=14)
        assert env.client.get("/").status_code == 200
    env.clock.advance(minutes=15, seconds=1)
    assert env.client.get("/").headers["location"] == "/auth/login"
    with Session(env.engine) as db:
        rows = list(
            db.execute(select(WebSession).where(WebSession.user_id == sup.user_id)).scalars()
        )
    assert rows and all(r.revoked_at is not None for r in rows)


def test_absolute_timeout(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    for _ in range(47):
        env.clock.advance(minutes=10)
        assert env.client.get("/").status_code == 200
    env.clock.advance(minutes=10)
    assert env.client.get("/").headers["location"] == "/auth/login"


def test_absolute_timeout_counts_from_password_login_not_mfa(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    post_login(env.client, admin.username, PASSWORD)
    env.clock.advance(minutes=4)
    assert post_mfa(env.client, totp_now(env, admin)).status_code == 303
    with Session(env.engine) as db:
        token = session_cookie(env.client) or ""
        row = db.get(WebSession, hashlib.sha256(token.encode()).digest())
        assert row and row.expires_at == START + timedelta(hours=8)


def test_partial_session_expires_quickly(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    post_login(env.client, admin.username, PASSWORD)
    env.clock.advance(minutes=5, seconds=1)
    assert env.client.get("/auth/mfa/verify").headers["location"] == "/auth/login"


def test_login_rotates_any_existing_session(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    first = session_cookie(env.client)
    full_login(env, sup)
    second = session_cookie(env.client)
    assert first and second and first != second
    stale = env.new_client()
    stale.cookies.set(SESSION_COOKIE, first)
    assert stale.get("/").headers["location"] == "/auth/login"


def test_deactivated_user_loses_the_session(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    with Session(env.engine) as db, db.begin():
        db.execute(update(User).where(User.id == sup.user_id).values(is_active=False))
    assert env.client.get("/").headers["location"] == "/auth/login"


def test_logout_revokes_the_session_and_clears_the_cookie(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    token = session_cookie(env.client)
    home = env.client.get("/")
    r = env.client.post("/auth/logout", data={"csrf_token": form_token(home)})
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    cleared = next(h for h in set_cookie_headers(r) if h.startswith(SESSION_COOKIE + "="))
    assert "max-age=0" in cleared.lower()
    stale = env.new_client()
    stale.cookies.set(SESSION_COOKIE, token or "")
    assert stale.get("/").headers["location"] == "/auth/login"
    assert "auth.logout" in actions(env, sup.user_id)


# --- CSRF ----------------------------------------------------------------------


def test_login_post_needs_the_double_submit_token(env: Env) -> None:
    sup = make_user(env, "supervisor")
    c = env.new_client()
    c.get("/auth/login")
    r = c.post("/auth/login", data={"username": sup.username, "password": PASSWORD})
    assert r.status_code == 403
    r = c.post(
        "/auth/login",
        data={"username": sup.username, "password": PASSWORD, "csrf_token": "x" * 43},
    )
    assert r.status_code == 403
    fresh = env.new_client()
    r = fresh.post(
        "/auth/login",
        data={"username": sup.username, "password": PASSWORD, "csrf_token": "x" * 43},
    )
    assert r.status_code == 403
    assert sup.user_id not in {r.object_id for r in audit_rows(env) if r.action == "auth.login"}


def test_state_changing_requests_need_the_session_csrf_token(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    token = form_token(env.client.get("/"))
    assert env.client.post("/test/change").status_code == 403
    assert env.client.post("/test/change", headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert env.client.post("/test/change", headers={"X-CSRF-Token": token}).status_code == 200
    assert env.client.post("/test/change", data={"csrf_token": token}).status_code == 200
    assert env.client.post("/auth/logout").status_code == 403


def test_csrf_token_of_another_session_is_rejected(env: Env) -> None:
    a = make_user(env, "supervisor")
    b = make_user(env, "supervisor")
    full_login(env, a)
    other = full_login(env, b, env.new_client())
    foreign = form_token(other.get("/"))
    assert env.client.post("/test/change", headers={"X-CSRF-Token": foreign}).status_code == 403


def test_csrf_token_changes_when_the_session_rotates(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    post_login(env.client, admin.username, PASSWORD)
    before = form_token(env.client.get("/auth/mfa/verify"))
    post_mfa(env.client, totp_now(env, admin))
    after = form_token(env.client.get("/"))
    assert before != after
    assert env.client.post("/test/change", headers={"X-CSRF-Token": before}).status_code == 403


# --- audit hygiene ----------------------------------------------------------------


def test_audit_entries_never_contain_secrets(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    first = len(audit_rows(env))
    post_login(env.new_client(), admin.username, "Wrong-Password-Typed-1")
    c = env.new_client()
    post_login(c, admin.username, PASSWORD)
    post_mfa(c, "123456")
    post_mfa(c, admin.recovery_codes[2])
    token = session_cookie(c) or ""
    csrf = form_token(c.get("/"))
    c.post("/auth/logout", data={"csrf_token": csrf})
    rows = audit_rows(env)[first:]
    assert rows
    blob = " ".join(f"{r.action} {r.object_type} {r.object_id} {r.detail}" for r in rows)
    for secret in [
        PASSWORD,
        "Wrong-Password-Typed-1",
        admin.totp_secret or "",
        *admin.recovery_codes,
        token,
        csrf,
        "123456",
    ]:
        assert secret not in blob
    assert all(str(r.ip) == PEER[0] for r in rows)


# --- services used by later tasks -------------------------------------------------


def _session_for(env: Env, client: TestClient) -> ConsoleSession:
    token = session_cookie(client)
    with Session(env.engine) as db:
        loaded = ConsoleAuth(env.app.state.auth, db).load(token)
    assert loaded is not None
    return loaded


def test_set_password_applies_the_policy_and_ends_sessions(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    actor = _session_for(env, env.client)
    new_password = "orchid-barge-51-meadow"  # noqa: S105 - test data
    with Session(env.engine) as db:
        auth = ConsoleAuth(env.app.state.auth, db)
        assert auth.set_password(sup.user_id, "short", actor=actor, ip=PEER[0]) == [
            PolicyViolation.TOO_SHORT
        ]
        assert auth.set_password(sup.user_id, PASSWORD, actor=actor, ip=PEER[0]) == [
            PolicyViolation.REUSED
        ]
        assert auth.set_password(sup.user_id, new_password, actor=actor, ip=PEER[0]) == []
    assert env.client.get("/").headers["location"] == "/auth/login"
    assert post_login(env.new_client(), sup.username, PASSWORD).status_code == 401
    c = env.new_client()
    r = post_login(c, sup.username, new_password)
    assert r.status_code == 303
    row = next(r for r in audit_rows(env, sup.user_id) if r.action == "auth.password_changed")
    assert row.detail == {"by_self": True}
    assert new_password not in str(row.detail)


def test_rotate_issues_a_new_id_and_keeps_the_absolute_limit(env: Env) -> None:
    sup = make_user(env, "supervisor")
    full_login(env, sup)
    before = _session_for(env, env.client)
    with Session(env.engine) as db:
        token = ConsoleAuth(env.app.state.auth, db).rotate(before, ip=PEER[0], user_agent=None)
    assert token != before.token
    rotated = env.new_client()
    rotated.cookies.set(SESSION_COOKIE, token)
    after = _session_for(env, rotated)
    assert after.expires_at == before.expires_at
    # Control: a cookie set this way is sent (3.5 found that `domain="testserver"`
    # made httpx drop it, so the stale checks passed without sending anything).
    assert rotated.get("/").status_code == 200
    stale = env.new_client()
    stale.cookies.set(SESSION_COOKIE, before.token)
    assert stale.get("/").headers["location"] == "/auth/login"


def test_htmx_requests_get_a_status_and_hx_redirect(env: Env) -> None:
    r = env.client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/auth/login"
    admin = make_user(env, "admin")
    post_login(env.client, admin.username, PASSWORD)
    r = env.client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 403
    assert r.headers["HX-Redirect"] == "/auth/mfa/enroll"


def test_mfa_state_sealed_under_another_kek_never_passes(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    other = KeyRing.from_secrets("another-kek-value-that-is-long-enough-123")
    with Session(env.engine) as db, db.begin():
        user = db.get(User, admin.user_id)
        assert user and user.totp_secret_encrypted
        state = open_mfa(env.ring, user.id, user.totp_secret_encrypted)
        user.totp_secret_encrypted = seal_mfa(other, user.id, state)
    post_login(env.client, admin.username, PASSWORD)
    assert post_mfa(env.client, totp_now(env, admin)).status_code == 400
    assert env.client.get("/").headers["location"] == "/auth/mfa/verify"


def test_account_locked_elsewhere_ends_a_partial_session_at_mfa(env: Env) -> None:
    admin = make_user(env, "admin", mfa=True)
    post_login(env.client, admin.username, PASSWORD)
    for _ in range(5):
        post_login(env.new_client(), admin.username, "wrong-password-123")
    page = env.client.get("/auth/mfa/verify")
    r = env.client.post(
        "/auth/mfa/verify", data={"code": totp_now(env, admin), "csrf_token": form_token(page)}
    )
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    assert env.client.get("/").headers["location"] == "/auth/login"


def test_security_headers_on_auth_pages(env: Env) -> None:
    r = env.client.get("/auth/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "'unsafe-inline'" not in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "<script" not in r.text
