"""Device enrollment, access tokens, rotation and revocation (task 3.6).

Real PostgreSQL, connecting as the limited app role, with a fake clock.
Contract: `api-contract.md` sections 2, 3 and 7; audit: security-privacy 5.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from ag_server.app import create_app
from ag_server.auth.sessions import SESSION_COOKIE, csrf_token_for, new_token, session_key
from ag_server.devices import (
    CurrentDevice,
    DeviceIdentity,
    DevicePolicy,
    Limit,
    RateLimits,
)
from ag_server.migrations.runner import upgrade
from ag_server.models import AuditLog, Line, PpeProfile, Role, Site, User, WebSession, Workstation
from ag_server.rbac import Access, AccessKind, Permission, route_access, undeclared_routes
from ag_server.settings import Settings, load_settings
from ag_shared.api import API_PREFIX, PROBLEM_CONTENT_TYPE, SCHEMA_REVISION
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

BASE = "https://testserver"
START = datetime(2026, 11, 22, 8, 0, 0, tzinfo=UTC)
HEADERS = {"X-AG-Schema": str(SCHEMA_REVISION), "X-AG-App-Version": "1.2.3"}
GENEROUS = RateLimits(
    token_per_ip=Limit(10_000, 60.0),
    token_per_workstation=Limit(10_000, 60.0),
    enroll_per_ip=Limit(10_000, 60.0),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@dataclass
class World:
    sites: dict[str, uuid.UUID] = field(default_factory=dict)
    lines: dict[str, uuid.UUID] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)
    ppe_id: uuid.UUID = field(default_factory=uuid.uuid4)


@pytest.fixture(scope="module")
def dsn(pg_database_factory: Callable[[], str], pg_app_role: str) -> str:
    d = pg_database_factory()
    upgrade(d, "head", app_role=pg_app_role)
    return d


@pytest.fixture(scope="module")
def owner_engine(dsn: str) -> Iterator[Engine]:
    engine = create_engine(dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def world(owner_engine: Engine) -> World:
    w = World()
    with Session(owner_engine) as db, db.begin():
        roles = {r.name: r.id for r in db.execute(select(Role)).scalars()}
        w.ppe_id = db.execute(
            select(PpeProfile.id).where(PpeProfile.is_builtin, PpeProfile.kind == "open_face")
        ).scalar_one()
        for s in ("A", "B"):
            site = Site(name=f"site {s}", timezone="Asia/Kolkata")
            db.add(site)
            db.flush()
            w.sites[s] = site.id
            line = Line(site_id=site.id, name=f"line {s}1")
            db.add(line)
            db.flush()
            w.lines[s] = line.id
        for name, role, site_key in [
            ("admin_A", "admin", "A"),
            ("admin_all", "admin", None),
            ("sup_A", "supervisor", "A"),
        ]:
            u = User(
                site_id=w.sites[site_key] if site_key else None,
                role_id=roles[role],
                display_name=name,
                username=f"{name}-{uuid.uuid4().hex[:6]}",
            )
            db.add(u)
            db.flush()
            w.users[name] = u.id
    return w


@pytest.fixture(scope="module")
def settings(dsn: str, as_app_role: Callable[[str], str]) -> Settings:
    return load_settings({"AG_ENV": "test", "AG_DATABASE_URL": as_app_role(dsn)})


@pytest.fixture(scope="module")
def clock() -> FakeClock:
    return FakeClock()


def build_app(settings: Settings, clock: FakeClock, limits: RateLimits = GENEROUS) -> FastAPI:
    app = create_app(settings, clock=clock, device_policy=DevicePolicy(limits=limits))

    # A test-only device route: what 3.7/3.8 get from `current_device`.
    @app.get(API_PREFIX + "/_test/whoami")
    def whoami(device: CurrentDevice) -> dict[str, str]:
        assert isinstance(device, DeviceIdentity)
        return {
            "workstation_id": str(device.workstation_id),
            "site_id": str(device.site_id),
            "line_id": str(device.line_id),
        }

    return app


@pytest.fixture(scope="module")
def client(settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    with TestClient(build_app(settings, clock), base_url=BASE, follow_redirects=False) as c:
        yield c


# --- helpers -----------------------------------------------------------------------------


def new_workstation(owner_engine: Engine, world: World, site: str = "A") -> uuid.UUID:
    with Session(owner_engine) as db, db.begin():
        ws = Workstation(
            line_id=world.lines[site],
            name=f"ws {uuid.uuid4().hex[:6]}",
            ppe_profile_id=world.ppe_id,
        )
        db.add(ws)
        db.flush()
        return ws.id


def workstation_row(owner_engine: Engine, ws_id: uuid.UUID) -> Workstation:
    with Session(owner_engine, expire_on_commit=False) as db:
        row = db.get(Workstation, ws_id)
        assert row is not None
        return row


def login_as(
    client: TestClient, owner_engine: Engine, clock: FakeClock, user_id: uuid.UUID
) -> dict[str, str]:
    """A complete console session written to the database; returns CSRF headers."""
    token = new_token()
    with Session(owner_engine) as db, db.begin():
        db.add(
            WebSession(
                id=session_key(token),
                user_id=user_id,
                created_at=clock.now,
                last_seen_at=clock.now,
                expires_at=clock.now + timedelta(hours=8),
                mfa_passed=True,
            )
        )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)
    key = client.app.state.auth.csrf_key  # type: ignore[attr-defined]  # FastAPI app at runtime
    return {"X-CSRF-Token": csrf_token_for(key, token)}


def issue_token(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World, ws_id: uuid.UUID
) -> str:
    headers = login_as(client, owner_engine, clock, world.users["admin_A"])
    r = client.post(f"/admin/workstations/{ws_id}/enrollment-token", headers=headers)
    client.cookies.clear()
    assert r.status_code == 201, r.text
    token: str = r.json()["enrollment_token"]
    return token


def enroll_body(token: str) -> dict[str, str]:
    return {"enrollment_token": token, "hostname": "PACK-07", "hardware_fingerprint": "fp-123"}


def enroll(client: TestClient, token: str) -> tuple[uuid.UUID, str]:
    r = client.post(API_PREFIX + "/enroll", json=enroll_body(token), headers=HEADERS)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["workstation_id"]), r.json()["device_secret"]


def enrolled(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World, site: str = "A"
) -> tuple[uuid.UUID, str]:
    ws = new_workstation(owner_engine, world, site)
    if site == "A":
        token = issue_token(client, owner_engine, clock, world, ws)
    else:
        headers = login_as(client, owner_engine, clock, world.users["admin_all"])
        r = client.post(f"/admin/workstations/{ws}/enrollment-token", headers=headers)
        client.cookies.clear()
        token = r.json()["enrollment_token"]
    got, secret = enroll(client, token)
    assert got == ws
    return ws, secret


def access_token(client: TestClient, ws: uuid.UUID, secret: str) -> str:
    r = client.post(
        API_PREFIX + "/token",
        json={"workstation_id": str(ws), "device_secret": secret},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    tok: str = r.json()["access_token"]
    return tok


def token_status(client: TestClient, ws: uuid.UUID, secret: str) -> tuple[int, str | None]:
    r = client.post(
        API_PREFIX + "/token",
        json={"workstation_id": str(ws), "device_secret": secret},
        headers=HEADERS,
    )
    return r.status_code, (r.json().get("code") if r.status_code != 200 else None)


def bearer(tok: str) -> dict[str, str]:
    return HEADERS | {"Authorization": f"Bearer {tok}"}


def audit_rows(owner_engine: Engine, action: str, object_id: uuid.UUID) -> list[AuditLog]:
    with Session(owner_engine) as db:
        return list(
            db.execute(
                select(AuditLog)
                .where(AuditLog.action == action, AuditLog.object_id == str(object_id))
                .order_by(AuditLog.id)
            ).scalars()
        )


def assert_problem(r: object, status: int, code: str | None) -> None:
    from httpx import Response

    assert isinstance(r, Response)
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = r.json()
    assert body["status"] == status
    assert body.get("code") == code


# --- route declarations -------------------------------------------------------------------


def test_device_and_admin_routes_are_declared(settings: Settings, clock: FakeClock) -> None:
    app = build_app(settings, clock)
    assert undeclared_routes(app) == []
    found = {(m, r.path): r.declarations[0] for r in route_access(app) if r.api for m in r.methods}
    assert found[("POST", API_PREFIX + "/enroll")] == Access(AccessKind.PUBLIC)
    assert found[("POST", API_PREFIX + "/token")] == Access(AccessKind.PUBLIC)
    assert found[("POST", API_PREFIX + "/secret/rotate")] == Access(AccessKind.DEVICE)
    manage = Access(AccessKind.PERMISSION, Permission.CONFIG_MANAGE)
    assert found[("POST", "/admin/workstations/{workstation_id}/enrollment-token")] == manage
    assert found[("POST", "/admin/workstations/{workstation_id}/revoke")] == manage


# --- enrollment token issue ---------------------------------------------------------------


def test_issue_enrollment_token_is_hashed_shown_once_and_audited(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws = new_workstation(owner_engine, world)
    headers = login_as(client, owner_engine, clock, world.users["admin_A"])
    r = client.post(f"/admin/workstations/{ws}/enrollment-token", headers=headers)
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    token = body["enrollment_token"]
    assert len(token) >= 43
    assert body["workstation_id"] == str(ws)
    assert datetime.fromisoformat(body["expires_at"]) == clock.now + timedelta(hours=24)
    row = workstation_row(owner_engine, ws)
    assert row.enrollment_token_hash and token not in row.enrollment_token_hash
    assert row.enrollment_expires_at == clock.now + timedelta(hours=24)
    (entry,) = audit_rows(owner_engine, "device.enrollment_token_issued", ws)
    assert entry.actor_user_id == world.users["admin_A"]
    assert entry.site_id == world.sites["A"]
    assert token not in json.dumps(entry.detail)


def test_issue_needs_config_manage_scope_and_csrf(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws_a = new_workstation(owner_engine, world, "A")
    ws_b = new_workstation(owner_engine, world, "B")
    client.cookies.clear()
    assert client.post(f"/admin/workstations/{ws_a}/enrollment-token").status_code == 401
    headers = login_as(client, owner_engine, clock, world.users["sup_A"])
    assert (
        client.post(f"/admin/workstations/{ws_a}/enrollment-token", headers=headers).status_code
        == 403
    )
    headers = login_as(client, owner_engine, clock, world.users["admin_A"])
    assert client.post(f"/admin/workstations/{ws_a}/enrollment-token").status_code == 403, "csrf"
    other = client.post(f"/admin/workstations/{ws_b}/enrollment-token", headers=headers)
    missing = client.post(f"/admin/workstations/{uuid.uuid4()}/enrollment-token", headers=headers)
    assert other.status_code == 404 and missing.status_code == 404
    assert other.json() == missing.json()
    assert workstation_row(owner_engine, ws_b).enrollment_token_hash is None
    client.cookies.clear()


# --- enrollment ---------------------------------------------------------------------------


def test_enrollment_works_once_only(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws = new_workstation(owner_engine, world)
    token = issue_token(client, owner_engine, clock, world, ws)
    r = client.post(API_PREFIX + "/enroll", json=enroll_body(token), headers=HEADERS)
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["workstation_id"] == str(ws)
    secret = body["device_secret"]
    assert len(secret) >= 43
    assert datetime.fromisoformat(body["server_time"].replace("Z", "+00:00")) == clock.now
    row = workstation_row(owner_engine, ws)
    assert row.status == "active" and row.enrolled_at == clock.now
    assert row.enrollment_token_hash is None and row.enrollment_expires_at is None
    assert row.device_secret_hash and secret not in row.device_secret_hash
    assert row.app_version == "1.2.3"
    again = client.post(API_PREFIX + "/enroll", json=enroll_body(token), headers=HEADERS)
    assert_problem(again, 401, "enrollment_invalid")
    (entry,) = audit_rows(owner_engine, "device.enrolled", ws)
    assert entry.actor_workstation_id == ws and entry.site_id == world.sites["A"]
    assert entry.detail["hostname"] == "PACK-07"
    dumped = json.dumps(entry.detail)
    assert secret not in dumped and token not in dumped


def test_enrollment_token_expires_after_24_hours(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws = new_workstation(owner_engine, world)
    token = issue_token(client, owner_engine, clock, world, ws)
    clock.advance(hours=24)
    expired = client.post(API_PREFIX + "/enroll", json=enroll_body(token), headers=HEADERS)
    unknown = client.post(API_PREFIX + "/enroll", json=enroll_body("x" * 43), headers=HEADERS)
    assert_problem(expired, 401, "enrollment_invalid")
    assert expired.json() == unknown.json(), "expired looks exactly like unknown"
    assert workstation_row(owner_engine, ws).status == "pending"


def test_reissuing_replaces_the_previous_enrollment_token(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws = new_workstation(owner_engine, world)
    first = issue_token(client, owner_engine, clock, world, ws)
    second = issue_token(client, owner_engine, clock, world, ws)
    old = client.post(API_PREFIX + "/enroll", json=enroll_body(first), headers=HEADERS)
    assert_problem(old, 401, "enrollment_invalid")
    assert enroll(client, second)[0] == ws


def test_enroll_validation_error_never_echoes_the_token(client: TestClient) -> None:
    token = "secret-enrollment-token-value-" + "q" * 20
    body = {"enrollment_token": token, "hostname": "h" * 300, "hardware_fingerprint": "fp"}
    r = client.post(API_PREFIX + "/enroll", json=body, headers=HEADERS)
    assert_problem(r, 422, "invalid_request")
    assert token not in r.text


# --- access tokens ------------------------------------------------------------------------


def test_token_is_a_short_lived_device_jwt(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World, settings: Settings
) -> None:
    ws, secret = enrolled(client, owner_engine, clock, world)
    r = client.post(
        API_PREFIX + "/token",
        json={"workstation_id": str(ws), "device_secret": secret},
        headers=HEADERS,
    )
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["expires_in"] == 900
    claims = jwt.decode(
        body["access_token"],
        settings.jwt_signing_key.get_secret_value(),
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_aud": False, "verify_iat": False},
    )
    assert claims["sub"] == str(ws) and claims["site_id"] == str(world.sites["A"])
    assert claims["scope"] == "device"
    who = client.get(API_PREFIX + "/_test/whoami", headers=bearer(body["access_token"]))
    assert who.json() == {
        "workstation_id": str(ws),
        "site_id": str(world.sites["A"]),
        "line_id": str(world.lines["A"]),
    }


def test_token_expires_after_15_minutes(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, secret = enrolled(client, owner_engine, clock, world)
    tok = access_token(client, ws, secret)
    clock.advance(seconds=899)
    assert client.get(API_PREFIX + "/_test/whoami", headers=bearer(tok)).status_code == 200
    clock.advance(seconds=1)
    r = client.get(API_PREFIX + "/_test/whoami", headers=bearer(tok))
    assert_problem(r, 401, "token_invalid")
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_wrong_secret_unknown_and_pending_workstations_fail_the_same_way(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, _secret = enrolled(client, owner_engine, clock, world)
    pending = new_workstation(owner_engine, world)
    bodies = []
    for wid, sec in [(ws, "wrong-" + "x" * 40), (uuid.uuid4(), "x" * 43), (pending, "x" * 43)]:
        r = client.post(
            API_PREFIX + "/token",
            json={"workstation_id": str(wid), "device_secret": sec},
            headers=HEADERS,
        )
        assert_problem(r, 401, "invalid_credentials")
        bodies.append(r.json())
    assert bodies[0] == bodies[1] == bodies[2]


def test_missing_or_malformed_bearer_is_refused(client: TestClient) -> None:
    assert_problem(client.get(API_PREFIX + "/_test/whoami", headers=HEADERS), 401, "token_invalid")
    for value in ["Bearer", "Basic abc", "Bearer not.a.jwt", "bearer x.y.z"]:
        r = client.get(API_PREFIX + "/_test/whoami", headers=HEADERS | {"Authorization": value})
        assert_problem(r, 401, "token_invalid")


# --- rotation -----------------------------------------------------------------------------


def test_rotation_keeps_the_old_secret_for_ten_minutes(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, old = enrolled(client, owner_engine, clock, world)
    tok = access_token(client, ws, old)
    r = client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    new = r.json()["device_secret"]
    until = datetime.fromisoformat(r.json()["previous_valid_until"].replace("Z", "+00:00"))
    assert new != old and until == clock.now + timedelta(minutes=10)
    row = workstation_row(owner_engine, ws)
    assert row.device_secret_hash and new not in row.device_secret_hash
    assert old not in row.device_secret_hash
    clock.advance(seconds=599)
    assert token_status(client, ws, old) == (200, None)
    assert token_status(client, ws, new) == (200, None)
    clock.advance(seconds=1)
    assert token_status(client, ws, old) == (401, "invalid_credentials")
    assert token_status(client, ws, new) == (200, None)
    (entry,) = audit_rows(owner_engine, "device.secret_rotated", ws)
    assert entry.actor_workstation_id == ws and entry.site_id == world.sites["A"]
    dumped = json.dumps(entry.detail)
    assert old not in dumped and new not in dumped


def test_a_lost_rotation_answer_does_not_lock_the_device_out(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, s0 = enrolled(client, owner_engine, clock, world)
    tok = access_token(client, ws, s0)
    lost = client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok)).json()
    clock.advance(seconds=30)
    second = client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok)).json()
    s2 = second["device_secret"]
    clock.advance(seconds=9 * 60)
    assert token_status(client, ws, s0) == (200, None), "s0 is what the device holds"
    assert token_status(client, ws, s2) == (200, None)
    assert token_status(client, ws, lost["device_secret"]) == (401, "invalid_credentials")


def test_rotation_with_a_token_of_an_expired_secret_is_refused(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, s0 = enrolled(client, owner_engine, clock, world)
    tok0 = access_token(client, ws, s0)
    s1 = client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok0)).json()[
        "device_secret"
    ]
    tok1 = access_token(client, ws, s1)
    client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok1))
    # tok0 is still a valid token, but s0 was dropped by the second rotation.
    r = client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok0))
    assert_problem(r, 401, "token_invalid")


def test_rotation_needs_a_device_token(client: TestClient) -> None:
    assert_problem(
        client.post(API_PREFIX + "/secret/rotate", json={}, headers=HEADERS), 401, "token_invalid"
    )


# --- revocation ---------------------------------------------------------------------------


def test_revocation_stops_tokens_and_is_audited(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, secret = enrolled(client, owner_engine, clock, world)
    tok = access_token(client, ws, secret)
    headers = login_as(client, owner_engine, clock, world.users["admin_A"])
    r = client.post(f"/admin/workstations/{ws}/revoke", headers=headers)
    client.cookies.clear()
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert_problem(
        client.get(API_PREFIX + "/_test/whoami", headers=bearer(tok)), 401, "device_revoked"
    )
    assert_problem(
        client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok)),
        401,
        "device_revoked",
    )
    assert token_status(client, ws, secret) == (401, "device_revoked")
    # Without the secret nobody learns that the device is revoked.
    assert token_status(client, ws, "y" * 43) == (401, "invalid_credentials")
    (entry,) = audit_rows(owner_engine, "device.revoked", ws)
    assert entry.actor_user_id == world.users["admin_A"] and entry.site_id == world.sites["A"]
    assert workstation_row(owner_engine, ws).status == "revoked"


def test_revocation_cancels_a_pending_enrollment_token_and_reenrollment_works(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, old = enrolled(client, owner_engine, clock, world)
    pending = issue_token(client, owner_engine, clock, world, ws)
    headers = login_as(client, owner_engine, clock, world.users["admin_A"])
    assert client.post(f"/admin/workstations/{ws}/revoke", headers=headers).status_code == 200
    client.cookies.clear()
    assert_problem(
        client.post(API_PREFIX + "/enroll", json=enroll_body(pending), headers=HEADERS),
        401,
        "enrollment_invalid",
    )
    fresh = issue_token(client, owner_engine, clock, world, ws)
    got, new = enroll(client, fresh)
    assert got == ws and workstation_row(owner_engine, ws).status == "active"
    assert token_status(client, ws, new) == (200, None)
    assert token_status(client, ws, old) == (401, "invalid_credentials")


def test_revoke_is_scoped_and_needs_the_permission(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws_b, secret = enrolled(client, owner_engine, clock, world, "B")
    headers = login_as(client, owner_engine, clock, world.users["admin_A"])
    assert client.post(f"/admin/workstations/{ws_b}/revoke", headers=headers).status_code == 404
    headers = login_as(client, owner_engine, clock, world.users["sup_A"])
    ws_a = new_workstation(owner_engine, world)
    assert client.post(f"/admin/workstations/{ws_a}/revoke", headers=headers).status_code == 403
    client.cookies.clear()
    assert token_status(client, ws_b, secret) == (200, None)


# --- scope isolation ----------------------------------------------------------------------


def test_device_token_cannot_reach_console_routes(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, secret = enrolled(client, owner_engine, clock, world)
    tok = access_token(client, ws, secret)
    client.cookies.clear()
    other = new_workstation(owner_engine, world)
    r = client.post(f"/admin/workstations/{other}/enrollment-token", headers=bearer(tok))
    assert r.status_code == 401
    assert client.get("/", headers=bearer(tok)).status_code == 303
    # Not even as a cookie value.
    client.cookies.set(SESSION_COOKIE, tok)
    assert client.get("/").status_code == 303
    client.cookies.clear()


def test_console_session_cannot_reach_device_routes(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    headers = login_as(client, owner_engine, clock, world.users["admin_all"])
    assert client.get("/", headers=headers).status_code == 200, "positive control"
    r = client.post(API_PREFIX + "/secret/rotate", json={}, headers=HEADERS | headers)
    assert_problem(r, 401, "token_invalid")
    assert_problem(
        client.get(API_PREFIX + "/_test/whoami", headers=HEADERS | headers), 401, "token_invalid"
    )
    client.cookies.clear()


def test_a_device_acts_only_on_itself(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws_a, sec_a = enrolled(client, owner_engine, clock, world, "A")
    ws_b, sec_b = enrolled(client, owner_engine, clock, world, "B")
    before_b = workstation_row(owner_engine, ws_b).device_secret_hash
    tok_a = access_token(client, ws_a, sec_a)
    # A body naming B is ignored: the workstation comes from the token.
    r = client.post(
        API_PREFIX + "/secret/rotate", json={"workstation_id": str(ws_b)}, headers=bearer(tok_a)
    )
    assert r.status_code == 200
    assert workstation_row(owner_engine, ws_b).device_secret_hash == before_b
    assert workstation_row(owner_engine, ws_a).device_secret_hash != before_b
    assert token_status(client, ws_b, sec_b) == (200, None)
    # A's secret never gets a token for B.
    assert token_status(client, ws_b, sec_a) == (401, "invalid_credentials")


def test_forged_tokens_are_refused(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World, settings: Settings
) -> None:
    ws_a, sec_a = enrolled(client, owner_engine, clock, world, "A")
    ws_b, _ = enrolled(client, owner_engine, clock, world, "B")
    claims = jwt.decode(access_token(client, ws_a, sec_a), options={"verify_signature": False})
    key = settings.jwt_signing_key.get_secret_value()
    as_b = jwt.encode(claims | {"sub": str(ws_b)}, "attacker-key-0123456789-0123456789", "HS256")
    assert_problem(
        client.get(API_PREFIX + "/_test/whoami", headers=bearer(as_b)), 401, "token_invalid"
    )
    # Validly signed but claiming another site: refused (the server checks the row).
    moved = jwt.encode(claims | {"site_id": str(world.sites["B"])}, key, "HS256")
    assert_problem(
        client.get(API_PREFIX + "/_test/whoami", headers=bearer(moved)), 401, "token_invalid"
    )
    gone = jwt.encode(claims | {"sub": str(uuid.uuid4())}, key, "HS256")
    assert_problem(
        client.get(API_PREFIX + "/_test/whoami", headers=bearer(gone)), 401, "token_invalid"
    )


def test_a_workstation_moved_to_another_site_needs_a_new_token(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, secret = enrolled(client, owner_engine, clock, world, "A")
    tok = access_token(client, ws, secret)
    with Session(owner_engine) as db, db.begin():
        row = db.get(Workstation, ws)
        assert row is not None
        row.line_id = world.lines["B"]
    assert_problem(
        client.get(API_PREFIX + "/_test/whoami", headers=bearer(tok)), 401, "token_invalid"
    )
    fresh = access_token(client, ws, secret)
    assert client.get(API_PREFIX + "/_test/whoami", headers=bearer(fresh)).json()["site_id"] == str(
        world.sites["B"]
    )


# --- version headers ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/enroll", "/token", "/secret/rotate"])
def test_unsupported_schema_is_426_before_anything_else(client: TestClient, path: str) -> None:
    r = client.post(
        API_PREFIX + path,
        content=b"not json",
        headers={"X-AG-Schema": "0", "X-AG-App-Version": "0.1"},
    )
    assert_problem(r, 426, "update_required")


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"X-AG-App-Version": "1.0"}, "schema_header_invalid"),
        ({"X-AG-Schema": "two", "X-AG-App-Version": "1.0"}, "schema_header_invalid"),
        ({"X-AG-Schema": str(SCHEMA_REVISION)}, "app_version_invalid"),
        (
            {"X-AG-Schema": str(SCHEMA_REVISION), "X-AG-App-Version": "1.0 <script>"},
            "app_version_invalid",
        ),
        (
            {"X-AG-Schema": str(SCHEMA_REVISION), "X-AG-App-Version": "1" * 65},
            "app_version_invalid",
        ),
    ],
)
def test_missing_or_bad_version_headers_are_400(
    client: TestClient, headers: dict[str, str], code: str
) -> None:
    r = client.post(
        API_PREFIX + "/token",
        json={"workstation_id": str(uuid.uuid4()), "device_secret": "x" * 43},
        headers=headers,
    )
    assert_problem(r, 400, code)


def test_a_newer_schema_is_accepted(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, secret = enrolled(client, owner_engine, clock, world)
    r = client.post(
        API_PREFIX + "/token",
        json={"workstation_id": str(ws), "device_secret": secret, "future_field": 1},
        headers=HEADERS | {"X-AG-Schema": str(SCHEMA_REVISION + 5)},
    )
    assert r.status_code == 200


def test_token_records_the_app_version(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, secret = enrolled(client, owner_engine, clock, world)
    client.post(
        API_PREFIX + "/token",
        json={"workstation_id": str(ws), "device_secret": secret},
        headers=HEADERS | {"X-AG-App-Version": "1.3.0"},
    )
    assert workstation_row(owner_engine, ws).app_version == "1.3.0"


# --- rate limits --------------------------------------------------------------------------


TIGHT = RateLimits(
    token_per_ip=Limit(5, 60.0), token_per_workstation=Limit(3, 60.0), enroll_per_ip=Limit(2, 600.0)
)


@pytest.fixture
def tight_client(settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    with TestClient(build_app(settings, clock, TIGHT), base_url=BASE, follow_redirects=False) as c:
        yield c


def test_token_is_rate_limited_per_workstation_and_per_ip(
    tight_client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    c = tight_client
    ws, secret = enrolled(c, owner_engine, clock, world)  # one enroll used
    assert [token_status(c, ws, secret)[0] for _ in range(3)] == [200, 200, 200]
    r = c.post(
        API_PREFIX + "/token",
        json={"workstation_id": str(ws), "device_secret": secret},
        headers=HEADERS,
    )
    assert_problem(r, 429, "rate_limited")
    assert int(r.headers["retry-after"]) >= 1
    # Another workstation from the same IP still has IP budget (5): 4 used.
    other = uuid.uuid4()
    assert token_status(c, other, "x" * 43) == (401, "invalid_credentials")
    assert token_status(c, other, "x" * 43) == (429, "rate_limited"), "per-IP budget spent"
    # The window slides on the injected clock.
    clock.advance(seconds=61)
    assert token_status(c, ws, secret) == (200, None)


def test_enroll_is_rate_limited_per_ip(tight_client: TestClient) -> None:
    c = tight_client
    for _ in range(2):
        assert_problem(
            c.post(API_PREFIX + "/enroll", json=enroll_body("z" * 43), headers=HEADERS),
            401,
            "enrollment_invalid",
        )
    r = c.post(API_PREFIX + "/enroll", json=enroll_body("z" * 43), headers=HEADERS)
    assert_problem(r, 429, "rate_limited")


# --- secrets never logged -----------------------------------------------------------------


def test_no_secret_or_token_reaches_the_logs(
    client: TestClient,
    owner_engine: Engine,
    clock: FakeClock,
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    ws = new_workstation(owner_engine, world)
    token = issue_token(client, owner_engine, clock, world, ws)
    _, secret = enroll(client, token)
    tok = access_token(client, ws, secret)
    new = client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(tok)).json()[
        "device_secret"
    ]
    token_status(client, ws, "wrong-" + secret)
    logged = "\n".join(r.getMessage() + repr(r.args) for r in caplog.records)
    for value in (token, secret, tok, new):
        assert value not in logged


def test_tokens_of_a_replaced_secret_die_with_it(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    """Revoke, re-enroll (new PC): tokens of the old PC must not come back to life."""
    ws, old = enrolled(client, owner_engine, clock, world)
    old_tok = access_token(client, ws, old)
    headers = login_as(client, owner_engine, clock, world.users["admin_A"])
    assert client.post(f"/admin/workstations/{ws}/revoke", headers=headers).status_code == 200
    client.cookies.clear()
    enroll(client, issue_token(client, owner_engine, clock, world, ws))
    r = client.get(API_PREFIX + "/_test/whoami", headers=bearer(old_tok))
    assert_problem(r, 401, "token_invalid")


def test_tokens_of_the_previous_secret_end_with_the_overlap(
    client: TestClient, owner_engine: Engine, clock: FakeClock, world: World
) -> None:
    ws, old = enrolled(client, owner_engine, clock, world)
    old_tok = access_token(client, ws, old)
    client.post(API_PREFIX + "/secret/rotate", json={}, headers=bearer(old_tok))
    clock.advance(seconds=599)
    assert client.get(API_PREFIX + "/_test/whoami", headers=bearer(old_tok)).status_code == 200
    clock.advance(seconds=1)
    r = client.get(API_PREFIX + "/_test/whoami", headers=bearer(old_tok))
    assert_problem(r, 401, "token_invalid")
