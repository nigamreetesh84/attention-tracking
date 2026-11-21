"""The real workstation `DeviceApi` against the real server app (task 3.6, contract 8).

The workstation's `HttpxTransport` is given the in-process ASGI transport of
Starlette's `TestClient` (the synchronous counterpart of `httpx.ASGITransport`;
`DeviceApi` is synchronous), so the client's own request code, headers and
response parsing run against the FastAPI routes and PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from ag_server.app import create_app
from ag_server.auth.sessions import SESSION_COOKIE, csrf_token_for, new_token, session_key
from ag_server.devices import DevicePolicy
from ag_server.migrations.runner import upgrade
from ag_server.models import Line, PpeProfile, Role, Site, User, WebSession, Workstation
from ag_server.settings import load_settings
from ag_shared.api import API_PREFIX, EnrollRequest
from ag_workstation.clock import FakeClock as DeviceClock
from ag_workstation.sync.api import ApiError, DeviceApi
from ag_workstation.sync.clock_offset import ClockOffsetEstimator
from ag_workstation.sync.credentials import DeviceCredentials, InMemoryCredentialStore
from ag_workstation.sync.transport import HttpxTransport
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

START = datetime(2026, 11, 22, 8, 0, 0, tzinfo=UTC)
BASE = "https://testserver"


class ServerClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(scope="module")
def owner_engine(pg_database_factory: Callable[[], str], pg_app_role: str) -> Iterator[Engine]:
    dsn = pg_database_factory()
    upgrade(dsn, "head", app_role=pg_app_role)
    engine = create_engine(dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def setup(
    owner_engine: Engine, as_app_role: Callable[[str], str]
) -> Iterator[tuple[FastAPI, ServerClock, uuid.UUID, uuid.UUID]]:
    with Session(owner_engine) as db, db.begin():
        roles = {r.name: r.id for r in db.execute(select(Role)).scalars()}
        ppe = db.execute(
            select(PpeProfile.id).where(PpeProfile.is_builtin, PpeProfile.kind == "open_face")
        ).scalar_one()
        site = Site(name=f"site {uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
        db.add(site)
        db.flush()
        line = Line(site_id=site.id, name="line 1")
        db.add(line)
        db.flush()
        ws = Workstation(line_id=line.id, name="ws e2e", ppe_profile_id=ppe)
        admin = User(
            site_id=site.id,
            role_id=roles["admin"],
            display_name="admin",
            username=f"admin-{uuid.uuid4().hex[:6]}",
        )
        db.add_all([ws, admin])
        db.flush()
        ws_id, admin_id = ws.id, admin.id
    settings = load_settings(
        {"AG_ENV": "test", "AG_DATABASE_URL": as_app_role(owner_engine.url.render_as_string(False))}
    )
    clock = ServerClock()
    yield create_app(settings, clock=clock, device_policy=DevicePolicy()), clock, ws_id, admin_id


def admin_issue(tc: TestClient, owner_engine: Engine, admin_id: uuid.UUID, ws_id: uuid.UUID) -> str:
    token = new_token()
    with Session(owner_engine) as db, db.begin():
        db.add(
            WebSession(
                id=session_key(token),
                user_id=admin_id,
                created_at=START,
                last_seen_at=START,
                expires_at=START + timedelta(hours=8),
                mfa_passed=True,
            )
        )
    tc.cookies.set(SESSION_COOKIE, token)
    csrf = csrf_token_for(tc.app.state.auth.csrf_key, token)  # type: ignore[attr-defined]  # FastAPI app at runtime
    r = tc.post(f"/admin/workstations/{ws_id}/enrollment-token", headers={"X-CSRF-Token": csrf})
    tc.cookies.clear()
    assert r.status_code == 201, r.text
    value: str = r.json()["enrollment_token"]
    return value


def test_workstation_client_enrolls_gets_tokens_and_rotates(
    setup: tuple[FastAPI, ServerClock, uuid.UUID, uuid.UUID], owner_engine: Engine
) -> None:
    app, server_clock, ws_id, admin_id = setup
    with TestClient(app, base_url=BASE) as tc:
        enrollment = admin_issue(tc, owner_engine, admin_id, ws_id)
        transport = HttpxTransport(BASE, transport=tc._transport)  # the in-process ASGI transport
        store = InMemoryCredentialStore(None)
        device_clock = DeviceClock(start_utc=START)
        api = DeviceApi(transport, store, device_clock, ClockOffsetEstimator(), app_version="2.0.1")

        reply = api.enroll(
            EnrollRequest(
                enrollment_token=SecretStr(enrollment),
                hostname="PACK-07",
                hardware_fingerprint="fp",
            )
        )
        assert reply.workstation_id == ws_id
        creds = store.load()
        assert creds is not None and creds.workstation_id == ws_id
        first_secret = creds.device_secret.get_secret_value()

        api.token()

        api.rotate_secret()
        rotated = store.load()
        assert rotated is not None
        assert rotated.device_secret.get_secret_value() != first_secret
        # The client fetches a new token with the new secret after rotating.
        api.token()

        # Past the overlap only the new secret works.
        server_clock.now += timedelta(minutes=11)
        api.invalidate_token()
        api.token()
        stale = DeviceApi(
            transport,
            InMemoryCredentialStore(DeviceCredentials(ws_id, SecretStr(first_secret))),
            device_clock,
            ClockOffsetEstimator(),
            app_version="2.0.1",
        )
        with pytest.raises(ApiError) as err:
            stale.token()
        assert err.value.status == 401 and err.value.code == "invalid_credentials"

        # Enrolling twice with the same token is refused.
        with pytest.raises(ApiError) as again:
            api.enroll(
                EnrollRequest(
                    enrollment_token=SecretStr(enrollment),
                    hostname="PACK-07",
                    hardware_fingerprint="fp",
                )
            )
        assert again.value.status == 401 and again.value.code == "enrollment_invalid"
        transport.close()


def test_problem_bodies_parse_with_the_shared_model(
    setup: tuple[FastAPI, ServerClock, uuid.UUID, uuid.UUID],
) -> None:
    from ag_shared.api import Problem

    app, _clock, _ws, _admin = setup
    with TestClient(app, base_url=BASE) as tc:
        r: httpx.Response = tc.post(
            API_PREFIX + "/token",
            json={"workstation_id": str(uuid.uuid4()), "device_secret": "x" * 43},
            headers={"X-AG-Schema": "0", "X-AG-App-Version": "1"},
        )
        problem = Problem.model_validate_json(r.content)
        assert problem.status == 426 and problem.code == "update_required"
