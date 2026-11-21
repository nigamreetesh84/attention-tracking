"""Database constraints from `docs/schema.md`, checked on a real PostgreSQL.

Each test runs in a transaction that is rolled back; expected violations run in
a savepoint so the test can go on afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from ag_server.models import (
    Alert,
    AttentionBucket,
    AttentionEvent,
    Camera,
    ConsentPolicy,
    EvidenceDeletionRequest,
    IngestBatch,
    Line,
    PpeProfile,
    Role,
    ScanRecord,
    Site,
    User,
    Workstation,
    WorkstationConfig,
)
from sqlalchemy import Connection, Engine, Table, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DataError, IntegrityError

NOW = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)


@pytest.fixture
def conn(migrated_engine: Engine) -> Iterator[Connection]:
    with migrated_engine.connect() as connection:
        tx = connection.begin()
        try:
            yield connection
        finally:
            tx.rollback()


def _id() -> uuid.UUID:
    return uuid.uuid4()


def _add(conn: Connection, model: Any, **values: Any) -> uuid.UUID:
    """Insert one row through the model's table and return its id (generated if absent)."""
    table = model.__table__
    if "id" in table.columns and "id" not in values:
        values["id"] = _id()
    conn.execute(insert(table).values(**values))
    return cast(uuid.UUID, values["id"])


def _violates(conn: Connection, model: Any, **values: Any) -> None:
    with pytest.raises(IntegrityError), conn.begin_nested():
        _add(conn, model, **values)


@pytest.fixture
def world(conn: Connection) -> dict[str, uuid.UUID]:
    """A site with one line, one workstation, an admin role and two admins."""
    site = _add(conn, Site, name="Plant 1", timezone="Asia/Kolkata", settings={})
    line = _add(conn, Line, site_id=site, name="Line A")
    ppe = conn.execute(
        select(PpeProfile.id).where(PpeProfile.is_builtin, PpeProfile.kind == "open_face")
    ).scalar_one()
    ws = _add(conn, Workstation, line_id=line, name="WS-1", ppe_profile_id=ppe)
    admin_role = conn.execute(select(Role.id).where(Role.name == "admin")).scalar_one()
    a = _add(conn, User, site_id=site, role_id=admin_role, display_name="A", username="a")
    b = _add(conn, User, site_id=site, role_id=admin_role, display_name="B", username="b")
    return {"site": site, "line": line, "ws": ws, "ppe": ppe, "admin_a": a, "admin_b": b}


def _camera(ws: uuid.UUID, slot: int, role: str) -> dict[str, Any]:
    return {"workstation_id": ws, "slot": slot, "role": role, "kind": "usb"}


# --- camera ------------------------------------------------------------------


def test_only_one_primary_camera_per_workstation(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    ws = world["ws"]
    _add(conn, Camera, **_camera(ws, 1, "primary"))
    _add(conn, Camera, **_camera(ws, 2, "secondary"))
    _add(conn, Camera, **_camera(ws, 3, "secondary"))
    _violates(conn, Camera, **_camera(ws, 4, "primary"))


def test_each_workstation_has_its_own_primary(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    other = _add(conn, Workstation, line_id=world["line"], name="WS-2", ppe_profile_id=world["ppe"])
    _add(conn, Camera, **_camera(world["ws"], 1, "primary"))
    _add(conn, Camera, **_camera(other, 1, "primary"))


def test_camera_slot_is_unique_per_workstation_and_between_1_and_4(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    ws = world["ws"]
    _add(conn, Camera, **_camera(ws, 1, "primary"))
    _violates(conn, Camera, **_camera(ws, 1, "secondary"))
    _violates(conn, Camera, **_camera(ws, 0, "secondary"))
    _violates(conn, Camera, **_camera(ws, 5, "secondary"))


def test_camera_count_is_between_1_and_4(conn: Connection, world: dict[str, uuid.UUID]) -> None:
    row = conn.execute(select(Workstation.camera_count).where(Workstation.id == world["ws"]))
    assert row.scalar_one() == 2  # schema default
    for bad in (0, 5):
        _violates(
            conn, Workstation, line_id=world["line"], name=f"bad{bad}",
            ppe_profile_id=world["ppe"], camera_count=bad,
        )  # fmt: skip


def test_enum_columns_reject_unknown_values(conn: Connection, world: dict[str, uuid.UUID]) -> None:
    with pytest.raises(DataError), conn.begin_nested():
        conn.execute(
            text(
                "insert into camera (id, workstation_id, slot, role, kind)"
                " values (gen_random_uuid(), :ws, 1, 'tertiary', 'usb')"
            ),
            {"ws": world["ws"]},
        )


# --- evidence deletion -------------------------------------------------------


def test_evidence_deletion_needs_a_different_decider(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    base = {"site_id": world["site"], "requested_by": world["admin_a"], "reason": "wrong worker"}
    pending = _add(conn, EvidenceDeletionRequest, **base)  # undecided: decided_by is null
    _violates(
        conn, EvidenceDeletionRequest, **base, status="approved",
        decided_by=world["admin_a"], decided_at=NOW,
    )  # fmt: skip
    _add(
        conn, EvidenceDeletionRequest, **base, status="approved",
        decided_by=world["admin_b"], decided_at=NOW,
    )  # fmt: skip
    # The CHECK also holds on UPDATE, the path the approval service uses.
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            update(EvidenceDeletionRequest)
            .where(EvidenceDeletionRequest.id == pending)
            .values(decided_by=world["admin_a"], status="approved")
        )


# --- unique keys --------------------------------------------------------------


def test_line_name_is_unique_per_site(conn: Connection, world: dict[str, uuid.UUID]) -> None:
    _violates(conn, Line, site_id=world["site"], name="Line A")
    other_site = _add(conn, Site, name="Plant 2", timezone="UTC", settings={})
    _add(conn, Line, site_id=other_site, name="Line A")


def test_employee_id_is_unique_per_site_and_username_globally(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    role = conn.execute(select(Role.id).where(Role.name == "workstation_user")).scalar_one()
    worker = {"role_id": role, "display_name": "W"}
    _add(conn, User, site_id=world["site"], employee_id="E100", **worker)
    _violates(conn, User, site_id=world["site"], employee_id="E100", **worker)
    other_site = _add(conn, Site, name="Plant 2", timezone="UTC", settings={})
    _add(conn, User, site_id=other_site, employee_id="E100", **worker)
    _violates(conn, User, site_id=other_site, username="a", **worker)


def test_consent_policy_version_language_is_unique_per_site(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    policy = {
        "site_id": world["site"],
        "version": 1,
        "body": "text",
        "effective_from": NOW,
        "created_by": world["admin_a"],
    }
    _add(conn, ConsentPolicy, language="en", **policy)
    _add(conn, ConsentPolicy, language="hi", **policy)
    _violates(conn, ConsentPolicy, language="en", **policy)


def test_scan_has_at_most_one_attention_event(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    scan = _add(
        conn, ScanRecord, workstation_id=world["ws"], barcode="X1", scanned_at=NOW,
        state_at_scan="drowsy", confidence=0.9, outcome="warned",
    )  # fmt: skip
    event = {
        "workstation_id": world["ws"],
        "kind": "scan_warning",
        "state": "drowsy",
        "confidence": 0.9,
        "low_confidence": False,
        "started_at": NOW,
        "scan_record_id": scan,
        "signals": {},
        "camera_states": {},
        "engine_version": "1",
        "config_version": 1,
    }
    _add(conn, AttentionEvent, **event)
    _violates(conn, AttentionEvent, **event)
    # Episodes carry no scan, and many may exist.
    episode = {**event, "kind": "state_episode", "scan_record_id": None}
    _add(conn, AttentionEvent, **episode)
    _add(conn, AttentionEvent, **episode)


def test_open_alert_dedupe_key_is_unique_until_closed(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    alert = {"site_id": world["site"], "kind": "camera_offline", "severity": "warning",
             "dedupe_key": "ws1:cam2", "opened_at": NOW, "payload": {}}  # fmt: skip
    first = _add(conn, Alert, **alert)
    _violates(conn, Alert, **alert)
    _violates(conn, Alert, **alert, status="acknowledged")
    conn.execute(update(Alert).where(Alert.id == first).values(status="closed"))
    _add(conn, Alert, **alert)
    _add(conn, Alert, **{**alert, "status": "closed"})


def test_workstation_config_version_and_ingest_batch_are_keyed_per_workstation(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    cfg = {"workstation_id": world["ws"], "version": 1, "payload": {}, "created_at": NOW}
    conn.execute(insert(WorkstationConfig).values(**cfg))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(insert(WorkstationConfig).values(**cfg))
    batch = {"workstation_id": world["ws"], "batch_id": _id(), "item_count": 1, "result": {}}
    conn.execute(insert(IngestBatch).values(**batch))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(insert(IngestBatch).values(**batch))


def test_attention_bucket_is_unique_per_minute_and_user_including_no_user(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    table = cast(Table, AttentionBucket.__table__)
    minute = NOW.replace(second=0)
    for user_id in (None, world["admin_a"]):
        row = {"workstation_id": world["ws"], "user_id": user_id, "bucket_start": minute,
               "seconds": {"attentive": 30}}  # fmt: skip
        conn.execute(insert(table).values(**row))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(insert(table).values(**row))
        # A retried bucket is an upsert on the same key (ingest idempotency).
        upsert = pg_insert(table).values(**{**row, "seconds": {"attentive": 60}})
        conn.execute(
            upsert.on_conflict_do_update(
                index_elements=["workstation_id", "bucket_start", "user_id"],
                set_={"seconds": upsert.excluded.seconds},
            )
        )
    rows = conn.execute(select(table.c.seconds).where(table.c.workstation_id == world["ws"]))
    assert [r[0] for r in rows] == [{"attentive": 60}] * 2  # one row per key, both updated
    conn.execute(
        insert(table).values(workstation_id=world["ws"], user_id=None,
                             bucket_start=minute + timedelta(minutes=1), seconds={})
    )  # fmt: skip


# --- foreign keys --------------------------------------------------------------


def test_foreign_keys_are_enforced(conn: Connection, world: dict[str, uuid.UUID]) -> None:
    _violates(conn, Line, site_id=_id(), name="orphan")
    _violates(conn, Camera, **_camera(_id(), 1, "primary"))
    _violates(
        conn, ScanRecord, workstation_id=world["ws"], worker_session_id=_id(), barcode="X",
        scanned_at=NOW, state_at_scan="attentive", confidence=1.0, outcome="attentive",
    )  # fmt: skip


def test_audit_log_accepts_ids_of_rows_that_do_not_exist(conn: Connection) -> None:
    # audit_log references other rows by type and id only (no FK), so it can
    # outlive what it describes.
    conn.execute(
        text(
            "insert into audit_log (actor_user_id, action, object_type, object_id, detail, hash)"
            " values (:u, 'evidence.view', 'evidence_image', :o, '{}', '\\x00')"
        ),
        {"u": _id(), "o": str(_id())},
    )
    ids = conn.execute(text("select id from audit_log order by id")).scalars().all()
    assert ids and all(isinstance(i, int) for i in ids)


def test_orm_round_trip_including_a_bucket_without_a_worker(
    conn: Connection, world: dict[str, uuid.UUID]
) -> None:
    from ag_shared.config import CameraRole
    from sqlalchemy.orm import Session

    with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
        camera = Camera(workstation_id=world["ws"], slot=1, role=CameraRole.PRIMARY, kind="usb")
        bucket = AttentionBucket(
            workstation_id=world["ws"], user_id=None, bucket_start=NOW, seconds={"unknown": 60}
        )
        session.add_all([camera, bucket])
        session.flush()
        assert camera.id is not None  # server-generated UUID came back
        session.expire_all()
        loaded = session.get(AttentionBucket, (world["ws"], NOW, None))
        assert loaded is not None and loaded.seconds == {"unknown": 60}
        reloaded = session.get(Camera, camera.id)
        assert reloaded is not None and reloaded.role is CameraRole.PRIMARY
