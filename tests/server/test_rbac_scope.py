"""Site and line scoping on a real PostgreSQL (task 3.5, security-privacy 2).

A world of two sites (A with lines A1 and A2, B with line B1) gets one row of
every table per line, site or global owner. Each scoped model is then read
through `scoped_select` under every kind of scope, and the rows that come back
must be exactly the expected owners. The HTTP tests use the real app with
routes declared through `require`, connecting as the limited app role.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Annotated

import pytest
from ag_server.app import create_app
from ag_server.auth import ConsoleSession, NextStep
from ag_server.auth.deps import DbSession
from ag_server.auth.sessions import SESSION_COOKIE, csrf_token_for, new_token, session_key
from ag_server.migrations.runner import upgrade
from ag_server.models import (
    Alert,
    AlertDelivery,
    AppRelease,
    AttentionBucket,
    AttentionEvent,
    AuditLog,
    Base,
    Camera,
    ConsentPolicy,
    ConsentRecord,
    DeviceHealth,
    Dispute,
    EvidenceDeletionItem,
    EvidenceDeletionRequest,
    EvidenceImage,
    IngestBatch,
    Line,
    LiveViewSession,
    PpeProfile,
    PurgeLog,
    ReportRun,
    ReportSchedule,
    Role,
    ScanRecord,
    Shift,
    ShiftBreak,
    Site,
    StationAssignment,
    SupervisorLine,
    ThresholdProfile,
    User,
    WebSession,
    WorkerSession,
    Workstation,
    WorkstationConfig,
)
from ag_server.models.enums import (
    AlertKind,
    ConsentMethod,
    ConsentStatus,
    DeliveryChannel,
    LiveViewMode,
    PurgeKind,
    ReleaseChannel,
    ReportFormat,
)
from ag_server.rbac import (
    GLOBAL_MODELS,
    SCOPED_MODELS,
    Permission,
    Principal,
    Scope,
    get_scoped,
    load_scope,
    require,
    scope_filter,
    scoped_select,
    signed_in,
)
from ag_server.settings import Settings, load_settings
from ag_shared.api.events import AttentionEventKind, SessionMethod
from ag_shared.config import CameraKind, CameraRole
from ag_shared.ppe import PpeProfileKind
from ag_shared.states import AttentionState, ScanOutcome
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, inspect, select, update
from sqlalchemy.orm import Session

BASE = "https://testserver"
START = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)

LINES = ("A1", "A2", "B1")
SITE_OF = {"A1": "A", "A2": "A", "B1": "B"}
# Site-level rows a line-scoped Supervisor may read (reference data and the
# roster of their own site).
SITE_VISIBLE = {Site, ThresholdProfile, PpeProfile, ConsentPolicy, User, Shift, ShiftBreak}
# Null-site rows visible to any non-empty scope (the built-in PPE profiles).
GLOBAL_IN_ANY_SCOPE = {PpeProfile}

Identity = tuple[object, ...]


def identity(obj: object) -> Identity:
    ident = inspect(obj).identity
    assert ident is not None
    return tuple(ident)


@dataclass
class World:
    sites: dict[str, uuid.UUID] = field(default_factory=dict)
    lines: dict[str, uuid.UUID] = field(default_factory=dict)
    workstations: dict[str, uuid.UUID] = field(default_factory=dict)
    events: dict[str, uuid.UUID] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)
    # {model: {owner: identities}}; owner is a line, a site or "global".
    rows: dict[type[Base], dict[str, set[Identity]]] = field(default_factory=dict)

    def add(self, db: Session, owner: str, obj: Base) -> None:
        db.add(obj)
        db.flush()
        self.rows.setdefault(type(obj), {}).setdefault(owner, set()).add(identity(obj))

    def owned(self, model: type[Base], owners: set[str]) -> set[Identity]:
        return {i for o, ids in self.rows.get(model, {}).items() if o in owners for i in ids}


def _role_ids(db: Session) -> dict[str, uuid.UUID]:
    return {r.name: r.id for r in db.execute(select(Role)).scalars()}


def build_world(db: Session) -> World:
    w = World()
    roles = _role_ids(db)
    builtin_ppe = db.execute(
        select(PpeProfile).where(PpeProfile.is_builtin, PpeProfile.kind == "open_face")
    ).scalar_one()
    for p in db.execute(select(PpeProfile).where(PpeProfile.is_builtin)).scalars():
        w.rows.setdefault(PpeProfile, {}).setdefault("global", set()).add(identity(p))
    for r in db.execute(select(Role)).scalars():
        w.rows.setdefault(Role, {}).setdefault("global", set()).add(identity(r))

    def user(owner: str, name: str, role: str, site: str | None) -> User:
        u = User(
            site_id=w.sites[site] if site else None,
            role_id=roles[role],
            display_name=name,
            username=f"{name}-{uuid.uuid4().hex[:6]}",
        )
        w.add(db, owner, u)
        w.users[name] = u.id
        return u

    for s in ("A", "B"):
        site = Site(name=f"site {s}", timezone="Asia/Kolkata")
        w.add(db, s, site)
        w.sites[s] = site.id
    admin_all = user("global", "admin_all", "admin", None)
    for s in ("A", "B"):
        admin = user(s, f"admin_{s}", "admin", s)
        w.add(db, s, ThresholdProfile(site_id=w.sites[s], name=f"tp {s}"))
        w.add(
            db,
            s,
            PpeProfile(site_id=w.sites[s], name=f"ppe {s}", kind=PpeProfileKind.CUSTOM, signals={}),
        )
        policy = ConsentPolicy(
            site_id=w.sites[s], version=1, language="en", body="notice",
            effective_from=START, created_by=admin.id,
        )  # fmt: skip
        w.add(db, s, policy)
        w.add(
            db,
            s,
            Shift(
                site_id=w.sites[s], line_id=None, name=f"site shift {s}",
                start_time=time(6), end_time=time(14), days_of_week=[0, 1, 2],
            ),
        )  # fmt: skip
        schedule = ReportSchedule(
            site_id=w.sites[s], report_kind="daily", format=ReportFormat.PDF,
            cron="0 6 * * *", created_by=admin.id,
        )  # fmt: skip
        w.add(db, s, schedule)
        w.add(db, s, ReportRun(report_schedule_id=schedule.id, status="ok"))
        w.add(db, s, PurgeLog(site_id=w.sites[s], kind=PurgeKind.IMAGES, cutoff=START, status="ok"))
        w.add(
            db,
            s,
            AuditLog(site_id=w.sites[s], action="test.row", hash=b"x" * 32, detail={}),
        )
        w.add(
            db,
            s,
            Alert(
                site_id=w.sites[s], workstation_id=None, kind=AlertKind.DISK_FULL,
                severity="high", dedupe_key=f"site-{s}",
            ),
        )  # fmt: skip
        w.add(
            db,
            s,
            WebSession(
                id=session_key(new_token()), user_id=admin.id, created_at=START,
                last_seen_at=START, expires_at=START + timedelta(hours=1),
            ),
        )  # fmt: skip

    for name in LINES:
        s = SITE_OF[name]
        line = Line(site_id=w.sites[s], name=f"line {name}")
        w.add(db, name, line)
        w.lines[name] = line.id
        ws = Workstation(line_id=line.id, name=f"ws {name}", ppe_profile_id=builtin_ppe.id)
        w.add(db, name, ws)
        w.workstations[name] = ws.id
        cam = Camera(workstation_id=ws.id, slot=1, role=CameraRole.PRIMARY, kind=CameraKind.USB)
        w.add(db, name, cam)
        worker = user(s, f"worker_{name}", "workstation_user", s)
        session = WorkerSession(
            id=uuid.uuid4(), workstation_id=ws.id, user_id=worker.id,
            method=next(iter(SessionMethod)), started_at=START,
        )  # fmt: skip
        w.add(db, name, session)
        scan = ScanRecord(
            id=uuid.uuid4(), workstation_id=ws.id, worker_session_id=session.id,
            user_id=worker.id, barcode="X1", scanned_at=START,
            state_at_scan=AttentionState.ATTENTIVE, confidence=0.9,
            outcome=next(iter(ScanOutcome)),
        )  # fmt: skip
        w.add(db, name, scan)
        event = AttentionEvent(
            id=uuid.uuid4(), workstation_id=ws.id, user_id=worker.id,
            worker_session_id=session.id, kind=next(iter(AttentionEventKind)),
            state=AttentionState.ATTENTIVE, confidence=0.5, started_at=START,
            signals={}, camera_states={}, engine_version="1", config_version=1,
        )  # fmt: skip
        w.add(db, name, event)
        w.events[name] = event.id
        w.add(
            db,
            name,
            EvidenceImage(
                id=uuid.uuid4(), attention_event_id=event.id, camera_id=cam.id, seq=0,
                offset_ms=0, captured_at=START, sha256="0" * 64, size_bytes=1,
            ),
        )  # fmt: skip
        w.add(
            db,
            name,
            AttentionBucket(
                workstation_id=ws.id, user_id=worker.id, bucket_start=START, seconds={}
            ),
        )
        w.add(
            db,
            name,
            Dispute(
                id=uuid.uuid4(), attention_event_id=event.id, raised_by=worker.id,
                raised_at=START,
            ),
        )  # fmt: skip
        alert = Alert(
            site_id=w.sites[s], workstation_id=ws.id, kind=AlertKind.SLEEPING,
            severity="high", dedupe_key=f"ws-{name}",
        )  # fmt: skip
        w.add(db, name, alert)
        w.add(
            db,
            name,
            AlertDelivery(alert_id=alert.id, channel=DeliveryChannel.EMAIL, recipient="r"),
        )
        w.add(db, name, DeviceHealth(workstation_id=ws.id, at=START))
        w.add(
            db,
            name,
            LiveViewSession(
                user_id=admin_all.id, workstation_id=ws.id, camera_id=cam.id,
                mode=LiveViewMode.STILL,
            ),
        )  # fmt: skip
        w.add(db, name, WorkstationConfig(workstation_id=ws.id, version=1, payload={}))
        w.add(
            db,
            name,
            StationAssignment(
                workstation_id=ws.id, user_id=worker.id, assigned_by=admin_all.id,
                valid_until=START + timedelta(hours=8),
            ),
        )  # fmt: skip
        w.add(
            db,
            name,
            IngestBatch(workstation_id=ws.id, batch_id=uuid.uuid4(), item_count=1, result={}),
        )
        shift = Shift(
            site_id=w.sites[s], line_id=line.id, name=f"shift {name}",
            start_time=time(6), end_time=time(14), days_of_week=[0],
        )  # fmt: skip
        w.add(db, name, shift)
        w.add(
            db,
            name,
            ShiftBreak(shift_id=shift.id, name="tea", start_time=time(10), end_time=time(10, 15)),
        )

    # Consent records and deletion requests hang off site-level users/requests.
    for s in ("A", "B"):
        first_line = next(n for n in LINES if SITE_OF[n] == s)
        policy_id = db.execute(
            select(ConsentPolicy.id).where(ConsentPolicy.site_id == w.sites[s])
        ).scalar_one()
        w.add(
            db,
            s,
            ConsentRecord(
                user_id=w.users[f"worker_{first_line}"], consent_policy_id=policy_id,
                status=ConsentStatus.GRANTED, method=ConsentMethod.DIGITAL_SIGNATURE,
                granted_at=START,
            ),
        )  # fmt: skip
        request = EvidenceDeletionRequest(
            site_id=w.sites[s], requested_by=w.users[f"admin_{s}"], reason="test"
        )
        w.add(db, s, request)
        w.add(
            db,
            s,
            EvidenceDeletionItem(request_id=request.id, attention_event_id=w.events[first_line]),
        )

    # Supervisors: A1 only; one wrongly assigned to B1 although at site A; one with none.
    sup = user("A", "sup_A1", "supervisor", "A")
    w.add(db, "A1", SupervisorLine(user_id=sup.id, line_id=w.lines["A1"]))
    sup2 = user("A", "sup_A1_B1", "supervisor", "A")
    w.add(db, "A1", SupervisorLine(user_id=sup2.id, line_id=w.lines["A1"]))
    w.add(db, "B1", SupervisorLine(user_id=sup2.id, line_id=w.lines["B1"]))
    user("A", "sup_none", "supervisor", "A")
    sup_nosite = user("global", "sup_nosite", "supervisor", None)
    w.add(db, "A2", SupervisorLine(user_id=sup_nosite.id, line_id=w.lines["A2"]))

    # Global rows.
    w.add(db, "global", AuditLog(site_id=None, action="test.global", hash=b"y" * 32, detail={}))
    w.add(db, "global", PurgeLog(site_id=None, kind=PurgeKind.AUDIT, cutoff=START, status="ok"))
    w.add(db, "global", ReportRun(report_schedule_id=None, status="ok"))
    w.add(
        db,
        "global",
        AppRelease(
            version="1.0.0", channel=ReleaseChannel.STABLE, sha256="0" * 64,
            signature="sig", file_key="k",
        ),
    )  # fmt: skip
    return w


@pytest.fixture(scope="module")
def scope_dsn(pg_database_factory: Callable[[], str], pg_app_role: str) -> str:
    dsn = pg_database_factory()
    upgrade(dsn, "head", app_role=pg_app_role)
    return dsn


@pytest.fixture(scope="module")
def owner_engine(scope_dsn: str) -> Iterator[Engine]:
    engine = create_engine(scope_dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def world(owner_engine: Engine) -> World:
    with Session(owner_engine) as db, db.begin():
        return build_world(db)


# --- scope construction -----------------------------------------------------------------


def _session_for(db: Session, user_id: uuid.UUID) -> ConsoleSession:
    u = db.get(User, user_id)
    assert u is not None
    role = db.get(Role, u.role_id)
    assert role is not None
    unused = "unused"  # token fields are never read by load_scope
    return ConsoleSession(
        token=unused, key=b"k", user_id=u.id, username=u.username or "", display_name="x",
        role=role.name, permissions=frozenset(role.permissions), site_id=u.site_id,
        mfa_passed=True, next_step=NextStep.HOME,
        csrf_token=unused, created_at=START, expires_at=START,
    )  # fmt: skip


def test_admin_without_site_sees_all_sites(owner_engine: Engine, world: World) -> None:
    with Session(owner_engine) as db:
        scope = load_scope(db, _session_for(db, world.users["admin_all"]), Permission.LIVE_VIEW)
    assert scope == Scope.everything()


def test_site_admin_is_scoped_to_the_site(owner_engine: Engine, world: World) -> None:
    with Session(owner_engine) as db:
        scope = load_scope(db, _session_for(db, world.users["admin_A"]), Permission.LIVE_VIEW)
    assert scope == Scope.site(world.sites["A"])
    assert not scope.line_restricted


def test_supervisor_is_scoped_to_assigned_lines(owner_engine: Engine, world: World) -> None:
    with Session(owner_engine) as db:
        scope = load_scope(db, _session_for(db, world.users["sup_A1"]), Permission.LIVE_VIEW)
    assert scope.line_restricted
    assert scope.line_ids == frozenset({world.lines["A1"]})
    assert scope.site_ids == frozenset({world.sites["A"]})


def test_line_of_another_site_is_dropped(owner_engine: Engine, world: World) -> None:
    with Session(owner_engine) as db:
        scope = load_scope(db, _session_for(db, world.users["sup_A1_B1"]), Permission.LIVE_VIEW)
    assert scope.line_ids == frozenset({world.lines["A1"]})
    assert scope.site_ids == frozenset({world.sites["A"]})


def test_supervisor_without_lines_sees_nothing(owner_engine: Engine, world: World) -> None:
    with Session(owner_engine) as db:
        scope = load_scope(db, _session_for(db, world.users["sup_none"]), Permission.LIVE_VIEW)
    assert scope == Scope.nothing()


def test_supervisor_without_site_takes_the_sites_of_their_lines(
    owner_engine: Engine, world: World
) -> None:
    with Session(owner_engine) as db:
        scope = load_scope(db, _session_for(db, world.users["sup_nosite"]), Permission.LIVE_VIEW)
    assert scope.line_ids == frozenset({world.lines["A2"]})
    assert scope.site_ids == frozenset({world.sites["A"]})


def test_permission_not_granted_to_the_role_gives_an_empty_scope(
    owner_engine: Engine, world: World
) -> None:
    with Session(owner_engine) as db:
        scope = load_scope(db, _session_for(db, world.users["sup_A1"]), Permission.AUDIT_VIEW)
    assert scope == Scope.nothing()


# --- the filter, model by model ---------------------------------------------------------


def test_every_model_is_registered_as_scoped_or_global() -> None:
    models = {m.class_ for m in Base.registry.mappers}
    assert set(SCOPED_MODELS) | set(GLOBAL_MODELS) == models
    assert not set(SCOPED_MODELS) & set(GLOBAL_MODELS)
    assert set(GLOBAL_MODELS) == {Role, AppRelease}


def test_an_unregistered_model_is_refused() -> None:
    class NotAModel:
        pass

    with pytest.raises(LookupError):
        scope_filter(NotAModel, Scope.everything())  # type: ignore[arg-type]  # deliberately wrong


def _visible(owner_engine: Engine, world: World, model: type[Base], scope: Scope) -> set[Identity]:
    # Only the world's rows count: the HTTP tests below add sessions of their own.
    known = {i for ids in world.rows[model].values() for i in ids}
    with Session(owner_engine) as db:
        rows = db.execute(scoped_select(model, scope)).scalars()
        return {identity(o) for o in rows} & known


def _expected(world: World, model: type[Base], kind: str) -> set[str]:
    owners: set[str] = set()
    if model in GLOBAL_MODELS or kind == "all":
        return {"A", "B", "A1", "A2", "B1", "global"}
    if kind == "nothing":
        return set()
    if kind == "site_A":
        owners = {"A", "A1", "A2"}
    elif kind == "lines_A1":
        owners = {"A1"} | ({"A"} if model in SITE_VISIBLE else set())
    if model in GLOBAL_IN_ANY_SCOPE:
        owners.add("global")
    return owners


ALL_MODELS: list[type[Base]] = sorted([*SCOPED_MODELS, *GLOBAL_MODELS], key=lambda m: m.__name__)


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
@pytest.mark.parametrize("kind", ["all", "site_A", "lines_A1", "nothing"])
def test_scope_filter_returns_exactly_the_rows_in_scope(
    owner_engine: Engine, world: World, model: type[Base], kind: str
) -> None:
    assert world.rows.get(model), f"the world has no {model.__name__} rows"
    scope = {
        "all": Scope.everything(),
        "site_A": Scope.site(world.sites["A"]),
        "lines_A1": Scope.lines({(world.lines["A1"], world.sites["A"])}),
        "nothing": Scope.nothing(),
    }[kind]
    expected = world.owned(model, _expected(world, model, kind))
    assert _visible(owner_engine, world, model, scope) == expected


def test_site_wide_alert_is_not_shown_to_a_line_supervisor(
    owner_engine: Engine, world: World
) -> None:
    scope = Scope.lines({(world.lines["A1"], world.sites["A"])})
    visible = _visible(owner_engine, world, Alert, scope)
    site_wide = world.rows[Alert]["A"]
    assert visible and not visible & site_wide


def test_get_scoped_raises_404_for_out_of_scope_and_missing_ids(
    owner_engine: Engine, world: World
) -> None:
    from fastapi import HTTPException

    scope = Scope.site(world.sites["A"])
    with Session(owner_engine) as db:
        ws = get_scoped(db, Workstation, world.workstations["A2"], scope)
        assert ws.id == world.workstations["A2"]
        for bad in (world.workstations["B1"], uuid.uuid4()):
            with pytest.raises(HTTPException) as info:
                get_scoped(db, Workstation, bad, scope)
            assert info.value.status_code == 404


def test_get_scoped_accepts_composite_keys(owner_engine: Engine, world: World) -> None:
    from fastapi import HTTPException

    with Session(owner_engine) as db:
        key = (world.workstations["A1"], 1)
        cfg = get_scoped(
            db, WorkstationConfig, key, Scope.lines({(world.lines["A1"], world.sites["A"])})
        )
        assert cfg.version == 1
        with pytest.raises(HTTPException):
            get_scoped(
                db, WorkstationConfig, key, Scope.lines({(world.lines["A2"], world.sites["A"])})
            )


def test_scope_allows_helpers(world: World) -> None:
    a1, a2, b1 = world.lines["A1"], world.lines["A2"], world.lines["B1"]
    site_a = Scope.site(world.sites["A"])
    assert site_a.allows_site(world.sites["A"]) and not site_a.allows_site(world.sites["B"])
    lines = Scope.lines({(a1, world.sites["A"])})
    assert lines.allows_line(a1) and not lines.allows_line(a2)
    assert Scope.everything().allows_line(b1) and Scope.everything().allows_site(world.sites["B"])
    assert not Scope.nothing().allows_site(world.sites["A"])
    # A site scope cannot answer for a line id alone (it does not know the line's site).
    with pytest.raises(ValueError):
        site_a.allows_line(a1)


# --- end to end through the app ----------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(scope="module")
def settings(scope_dsn: str, as_app_role: Callable[[str], str]) -> Settings:
    return load_settings({"AG_ENV": "test", "AG_DATABASE_URL": as_app_role(scope_dsn)})


LiveView = Annotated[Principal, Depends(require(Permission.LIVE_VIEW))]
Assign = Annotated[Principal, Depends(require(Permission.WORKERS_ASSIGN))]
Audit = Annotated[Principal, Depends(require(Permission.AUDIT_VIEW))]


def build_app(settings: Settings) -> FastAPI:
    app = create_app(settings, clock=FakeClock())

    @app.get("/t/workstations")
    def list_ws(p: LiveView, db: DbSession) -> list[str]:
        with db.begin():
            return sorted(w.name for w in db.execute(scoped_select(Workstation, p.scope)).scalars())

    @app.get("/t/workstations/{ws_id}")
    def get_ws(ws_id: uuid.UUID, p: LiveView, db: DbSession) -> dict[str, str]:
        with db.begin():
            return {"name": get_scoped(db, Workstation, ws_id, p.scope).name}

    @app.get("/t/events/{event_id}", dependencies=[Depends(require(Permission.EVENTS_REVIEW))])
    def get_event(
        event_id: uuid.UUID,
        p: Annotated[Principal, Depends(require(Permission.EVENTS_REVIEW))],
        db: DbSession,
    ) -> dict[str, str]:
        with db.begin():
            return {"id": str(get_scoped(db, AttentionEvent, event_id, p.scope).id)}

    @app.post("/t/assign/{ws_id}")
    def assign(ws_id: uuid.UUID, p: Assign, db: DbSession) -> dict[str, str]:
        with db.begin():
            get_scoped(db, Workstation, ws_id, p.scope)
        return {"ok": "yes"}

    @app.get("/t/audit")
    def audit(p: Audit) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/t/me")
    def me(s: Annotated[ConsoleSession, Depends(signed_in())]) -> dict[str, str]:
        return {"role": s.role}

    return app


@pytest.fixture(scope="module")
def client(settings: Settings) -> Iterator[TestClient]:
    app = build_app(settings)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c


def test_test_routes_are_declared(settings: Settings) -> None:
    from ag_server.rbac import undeclared_routes

    assert undeclared_routes(build_app(settings)) == []


def login_as(
    client: TestClient, owner_engine: Engine, user_id: uuid.UUID, *, mfa_passed: bool = True
) -> str:
    """A live session straight in the database (sign-in itself is covered by 3.4)."""
    token = new_token()
    with Session(owner_engine) as db, db.begin():
        db.add(
            WebSession(
                id=session_key(token), user_id=user_id, created_at=START, last_seen_at=START,
                expires_at=START + timedelta(hours=8), mfa_passed=mfa_passed,
            )
        )  # fmt: skip
    client.cookies.clear()
    # No domain: httpx does not send a cookie set for the bare host "testserver".
    client.cookies.set(SESSION_COOKIE, token)
    return token


def csrf(client: TestClient, token: str) -> dict[str, str]:
    key = client.app.state.auth.csrf_key  # type: ignore[attr-defined]  # FastAPI app at runtime
    return {"X-CSRF-Token": csrf_token_for(key, token)}


def test_site_admin_gets_404_for_another_site(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    login_as(client, owner_engine, world.users["admin_A"])
    assert client.get(f"/t/workstations/{world.workstations['A2']}").status_code == 200
    other = client.get(f"/t/workstations/{world.workstations['B1']}")
    missing = client.get(f"/t/workstations/{uuid.uuid4()}")
    assert other.status_code == 404 and missing.status_code == 404
    assert other.json() == missing.json(), "out of scope looks exactly like missing"
    assert client.get(f"/t/events/{world.events['B1']}").status_code == 404
    assert client.get("/t/workstations").json() == ["ws A1", "ws A2"]


def test_supervisor_gets_404_for_another_line(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    token = login_as(client, owner_engine, world.users["sup_A1"])
    assert client.get(f"/t/workstations/{world.workstations['A1']}").status_code == 200
    assert client.get(f"/t/workstations/{world.workstations['A2']}").status_code == 404
    assert client.get(f"/t/workstations/{world.workstations['B1']}").status_code == 404
    assert client.get(f"/t/events/{world.events['A1']}").status_code == 200
    assert client.get(f"/t/events/{world.events['A2']}").status_code == 404
    assert client.get("/t/workstations").json() == ["ws A1"]
    # Writes that name an object are scoped the same way.
    headers = csrf(client, token)
    assert client.post(f"/t/assign/{world.workstations['A1']}", headers=headers).status_code == 200
    assert client.post(f"/t/assign/{world.workstations['A2']}", headers=headers).status_code == 404


def test_all_sites_admin_reaches_every_site(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    login_as(client, owner_engine, world.users["admin_all"])
    assert client.get(f"/t/workstations/{world.workstations['B1']}").status_code == 200
    assert client.get("/t/workstations").json() == ["ws A1", "ws A2", "ws B1"]
    assert client.get("/t/audit").status_code == 200


def test_missing_permission_is_403(client: TestClient, owner_engine: Engine, world: World) -> None:
    login_as(client, owner_engine, world.users["sup_A1"])
    r = client.get("/t/audit")
    assert r.status_code == 403
    assert r.headers["cache-control"] == "no-store"


def test_no_session_is_refused(client: TestClient) -> None:
    client.cookies.clear()
    r = client.get("/t/audit")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    assert client.get("/t/audit", headers={"HX-Request": "true"}).status_code == 401
    assert client.post(f"/t/assign/{uuid.uuid4()}").status_code == 401


def test_mfa_pending_session_is_refused(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    token = login_as(client, owner_engine, world.users["admin_A"], mfa_passed=False)
    r = client.get("/t/workstations")
    assert r.status_code == 303 and r.headers["location"] == "/auth/mfa/enroll"
    headers = csrf(client, token)
    assert client.post(f"/t/assign/{world.workstations['A1']}", headers=headers).status_code == 403


def test_inactive_user_is_refused(client: TestClient, owner_engine: Engine, world: World) -> None:
    uid = world.users["sup_A1"]
    login_as(client, owner_engine, uid)
    assert client.get("/t/workstations").status_code == 200
    with Session(owner_engine) as db, db.begin():
        db.execute(update(User).where(User.id == uid).values(is_active=False))
    try:
        r = client.get("/t/workstations")
        assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    finally:
        with Session(owner_engine) as db, db.begin():
            db.execute(update(User).where(User.id == uid).values(is_active=True))


def test_workstation_user_has_no_console_access(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    login_as(client, owner_engine, world.users["worker_A1"])
    assert client.get("/t/workstations").status_code == 303
    assert client.get("/t/me").status_code == 303


def test_role_change_applies_on_the_next_request(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    uid = world.users["sup_A1"]
    with Session(owner_engine) as db:
        roles = _role_ids(db)
    # A Supervisor without MFA: the session never passed MFA and needs none.
    login_as(client, owner_engine, uid, mfa_passed=False)
    assert client.get("/t/audit").status_code == 403
    try:
        with Session(owner_engine) as db, db.begin():
            db.execute(update(User).where(User.id == uid).values(role_id=roles["admin"]))
        # Same cookie: the promotion needs MFA first (3.4 step-up) ...
        assert client.get("/t/audit").headers.get("location") == "/auth/mfa/enroll"
        # ... and once the session has passed MFA, the Admin grant applies at once.
        with Session(owner_engine) as db, db.begin():
            db.execute(update(WebSession).where(WebSession.user_id == uid).values(mfa_passed=True))
        assert client.get("/t/audit").status_code == 200
        assert client.get(f"/t/workstations/{world.workstations['A2']}").status_code == 200
        # Demoted again: refused on the very next request with the same cookie.
        with Session(owner_engine) as db, db.begin():
            db.execute(update(User).where(User.id == uid).values(role_id=roles["supervisor"]))
        assert client.get("/t/audit").status_code == 403
        assert client.get(f"/t/workstations/{world.workstations['A2']}").status_code == 404
    finally:
        with Session(owner_engine) as db, db.begin():
            db.execute(update(User).where(User.id == uid).values(role_id=roles["supervisor"]))


def test_line_assignment_change_applies_on_the_next_request(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    uid = world.users["sup_A1"]
    login_as(client, owner_engine, uid)
    ws_a2 = f"/t/workstations/{world.workstations['A2']}"
    assert client.get(ws_a2).status_code == 404
    try:
        with Session(owner_engine) as db, db.begin():
            db.add(SupervisorLine(user_id=uid, line_id=world.lines["A2"]))
        assert client.get(ws_a2).status_code == 200
    finally:
        with Session(owner_engine) as db, db.begin():
            db.execute(
                delete(SupervisorLine).where(
                    SupervisorLine.user_id == uid, SupervisorLine.line_id == world.lines["A2"]
                )
            )
    assert client.get(ws_a2).status_code == 404


def test_role_permission_edit_applies_on_the_next_request(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    login_as(client, owner_engine, world.users["sup_A1"])
    assert client.get("/t/workstations").status_code == 200
    with Session(owner_engine) as db:
        before = db.execute(select(Role.permissions).where(Role.name == "supervisor")).scalar_one()
    try:
        narrowed = [p for p in before if p != "live.view"]
        with Session(owner_engine) as db, db.begin():
            db.execute(update(Role).where(Role.name == "supervisor").values(permissions=narrowed))
        assert client.get("/t/workstations").status_code == 403
        # A tampered row cannot widen a system role beyond the registry.
        with Session(owner_engine) as db, db.begin():
            db.execute(
                update(Role)
                .where(Role.name == "supervisor")
                .values(permissions=[*before, "audit.view"])
            )
        assert client.get("/t/audit").status_code == 403
    finally:
        with Session(owner_engine) as db, db.begin():
            db.execute(update(Role).where(Role.name == "supervisor").values(permissions=before))


def test_signed_in_marker_gives_the_session_and_no_permission(
    client: TestClient, owner_engine: Engine, world: World
) -> None:
    login_as(client, owner_engine, world.users["sup_none"])
    assert client.get("/t/me").json() == {"role": "supervisor"}
    # No lines: every scoped list is empty and every object is 404.
    assert client.get("/t/workstations").json() == []
    assert client.get(f"/t/workstations/{world.workstations['A1']}").status_code == 404
