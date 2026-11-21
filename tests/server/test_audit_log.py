"""Audit log service on a real PostgreSQL (task 3.3).

Covers the hash chain, tamper detection, the append-only trigger, the app
role's INSERT/SELECT-only grants, concurrent writers and the verify command.
Each test that writes gets its own freshly migrated database, so a tamper in
one test cannot leak into another.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ag_server.audit import (
    AuditEntry,
    AuditError,
    log_change,
    log_login_failure,
    verify_chain,
    write_audit,
)
from ag_server.audit.__main__ import main as audit_main
from ag_server.audit.scrub import REDACTED
from ag_server.migrations.runner import upgrade
from ag_server.models import AuditLog
from ag_shared.api import LoginFailurePayload
from sqlalchemy import Engine, Text, create_engine, delete, func, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

SITE_A = uuid.UUID("01890000-0000-7000-8000-00000000000a")
SITE_B = uuid.UUID("01890000-0000-7000-8000-00000000000b")
ADMIN = uuid.UUID("01890000-0000-7000-8000-0000000000ad")
STATION = uuid.UUID("01890000-0000-7000-8000-0000000000ee")
WORKER = uuid.UUID("01890000-0000-7000-8000-0000000000aa")
ROOT = Path(__file__).resolve().parents[2]
AUDIT = AuditLog.__table__


class StepClock:
    """Deterministic UTC clock that advances one millisecond per call."""

    def __init__(self) -> None:
        self._t = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            self._t += timedelta(milliseconds=1)
            return self._t


@pytest.fixture
def audit_dsn(pg_database_factory: Callable[[], str], pg_app_role: str) -> str:
    """A fresh database at head, migrated as the owner, with app-role grants."""
    dsn = pg_database_factory()
    upgrade(dsn, "head", app_role=pg_app_role)
    return dsn


@pytest.fixture
def owner(audit_dsn: str) -> Iterator[Engine]:
    engine = create_engine(audit_dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def app(audit_dsn: str, as_app_role: Callable[[str], str]) -> Iterator[Engine]:
    engine = create_engine(as_app_role(audit_dsn), pool_size=10)
    try:
        yield engine
    finally:
        engine.dispose()


def _entry(i: int = 0, site: uuid.UUID | None = SITE_A, **detail: object) -> AuditEntry:
    return AuditEntry(
        action="evidence.view",
        actor_user_id=ADMIN,
        site_id=site,
        object_type="evidence_image",
        object_id=str(i),
        ip="10.0.0.1",
        detail={"n": i, **detail},
    )


def _write_many(engine: Engine, n: int, clock: StepClock, site: uuid.UUID | None = SITE_A) -> None:
    for i in range(n):
        with engine.begin() as conn:
            write_audit(conn, _entry(i, site), now=clock)


# --- chain integrity ---------------------------------------------------------


def test_rows_form_a_valid_chain_per_site(app: Engine) -> None:
    clock = StepClock()
    _write_many(app, 3, clock, SITE_A)
    _write_many(app, 2, clock, SITE_B)
    _write_many(app, 2, clock, None)
    _write_many(app, 1, clock, SITE_A)

    with app.connect() as conn:
        result = verify_chain(conn)
        rows = conn.execute(select(AUDIT).order_by(AuditLog.id)).all()
    assert result.ok, result.problems
    assert result.rows == 8
    assert set(result.heads) == {SITE_A, SITE_B, None}

    a_rows = [r for r in rows if r.site_id == SITE_A]
    assert a_rows[0].prev_hash is None
    for prev, row in itertools.pairwise(a_rows):
        assert row.prev_hash == prev.hash
    assert result.heads[SITE_A].hash == a_rows[-1].hash
    assert result.heads[SITE_A].last_id == a_rows[-1].id
    assert result.heads[SITE_A].rows == 4


def test_write_returns_the_stored_row(app: Engine) -> None:
    clock = StepClock()
    with app.begin() as conn:
        first = write_audit(conn, _entry(1), now=clock)
        second = write_audit(conn, _entry(2), now=clock)
    assert second.prev_hash == first.hash
    assert second.id > first.id
    with app.connect() as conn:
        stored = conn.execute(select(AUDIT).where(AuditLog.id == second.id)).one()
    assert stored.hash == second.hash
    assert stored.at == second.at
    assert stored.detail == {"n": 2}
    assert str(stored.ip) == "10.0.0.1"


def test_detail_is_stored_in_database_normal_form(app: Engine) -> None:
    # Floats and nesting survive the JSONB round trip with a verifiable hash.
    clock = StepClock()
    detail = {"threshold": 0.35, "big": 1e300, "tiny": 1e-7, "list": [1, 2.5, "x"], "u": "é✓"}
    with app.begin() as conn:
        write_audit(
            conn, AuditEntry(action="config.update", site_id=SITE_A, detail=detail), now=clock
        )
    with app.connect() as conn:
        assert verify_chain(conn).ok


def test_ipv6_and_missing_fields_verify(app: Engine) -> None:
    clock = StepClock()
    with app.begin() as conn:
        write_audit(conn, AuditEntry(action="system.backup", ip="2001:db8::1"), now=clock)
        write_audit(conn, AuditEntry(action="system.restore"), now=clock)
    with app.connect() as conn:
        assert verify_chain(conn).ok


def test_rolled_back_write_leaves_no_gap_in_the_chain(app: Engine) -> None:
    clock = StepClock()
    _write_many(app, 2, clock)
    with app.connect() as conn:
        tx = conn.begin()
        write_audit(conn, _entry(99), now=clock)
        tx.rollback()
    _write_many(app, 2, clock)
    with app.connect() as conn:
        result = verify_chain(conn)
    assert result.ok, result.problems
    assert result.rows == 4


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "Evidence View"},
        {"action": "evidence"},
        {"action": ""},
        {"ip": "not-an-ip"},
        {"ip": "10.0.0.0/24"},
    ],
)
def test_invalid_entries_are_rejected(app: Engine, bad: dict[str, str]) -> None:
    fields: dict[str, object] = {"action": "evidence.view", **bad}
    with pytest.raises(AuditError), app.begin() as conn:
        write_audit(conn, AuditEntry(**fields))  # type: ignore[arg-type]  # built from a dict


def test_naive_clock_is_rejected(app: Engine) -> None:
    with pytest.raises(AuditError), app.begin() as conn:
        write_audit(conn, _entry(), now=lambda: datetime(2026, 1, 1))


def test_write_refuses_isolation_levels_that_could_fork_the_chain(app: Engine) -> None:
    # Under REPEATABLE READ the snapshot may predate the lock and miss the
    # previous head; the writer must refuse rather than fork the chain.
    with (
        pytest.raises(AuditError, match="READ COMMITTED"),
        app.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        write_audit(conn, _entry())


# --- secrets never reach the table ------------------------------------------


def test_write_scrubs_secrets_from_detail(app: Engine) -> None:
    clock = StepClock()
    detail: dict[str, object] = {
        "pin": "1234",
        "password": "hunter2-hunter2",
        "token": "tok_abcdef",
        "nested": {"device_secret": "s", "image_bytes": b"\xff\xd8\xff"},
        "camera_uri": "rtsp://admin:camPass99@10.1.1.1/s",
        "auth": "Bearer eyJabc.def.ghi",
        "ok": "kept",
    }
    with app.begin() as conn:
        write_audit(
            conn, AuditEntry(action="user.update", site_id=SITE_A, detail=detail), now=clock
        )
    with app.connect() as conn:
        raw = conn.execute(select(AuditLog.detail.cast(Text))).scalar_one()
        assert verify_chain(conn).ok
    for secret in ("1234", "hunter2", "tok_abcdef", "camPass99", "eyJabc", "\\xff"):
        assert secret not in raw, secret
    assert "kept" in raw
    assert REDACTED in raw


def test_log_change_records_before_after_and_changed_keys(app: Engine) -> None:
    clock = StepClock()
    before = {"display_name": "Asha", "pin_hash": "$argon2id$v=19$old", "is_active": True}
    after = {"display_name": "Asha K", "pin_hash": "$argon2id$v=19$new", "is_active": True}
    with app.begin() as conn:
        rec = log_change(
            conn,
            action="user.update",
            object_type="user",
            object_id=str(WORKER),
            before=before,
            after=after,
            actor_user_id=ADMIN,
            site_id=SITE_A,
            ip="10.0.0.9",
            now=clock,
        )
    with app.connect() as conn:
        row = conn.execute(select(AUDIT).where(AuditLog.id == rec.id)).one()
    assert row.action == "user.update"
    assert row.object_type == "user" and row.object_id == str(WORKER)
    assert row.detail == {
        "before": {"display_name": "Asha", "pin_hash": REDACTED, "is_active": True},
        "after": {"display_name": "Asha K", "pin_hash": REDACTED, "is_active": True},
        # A PIN reset is visible as a changed key without revealing either value.
        "changed": ["display_name", "pin_hash"],
    }


def test_log_change_for_create_and_delete(app: Engine) -> None:
    clock = StepClock()
    with app.begin() as conn:
        created = log_change(
            conn, action="line.create", object_type="line", object_id="L1",
            before=None, after={"name": "L1"}, site_id=SITE_A, now=clock,
        )  # fmt: skip
        deleted = log_change(
            conn, action="line.delete", object_type="line", object_id="L1",
            before={"name": "L1"}, after=None, site_id=SITE_A, now=clock,
        )  # fmt: skip
    with app.connect() as conn:
        rows = {
            r.id: r.detail for r in conn.execute(select(AUDIT)).all()
        }  # fmt: skip
    assert rows[created.id] == {"before": None, "after": {"name": "L1"}, "changed": ["name"]}
    assert rows[deleted.id] == {"before": {"name": "L1"}, "after": None, "changed": ["name"]}


def test_login_failure_is_one_row_without_the_typed_id(app: Engine) -> None:
    clock = StepClock()
    payload = LoginFailurePayload(
        method="employee_id_pin", user_id=WORKER, known_worker=True, locked=True
    )
    with app.begin() as conn:
        rec = log_login_failure(
            conn, payload, workstation_id=STATION, site_id=SITE_A, ip="10.2.0.4", now=clock
        )
        unknown = log_login_failure(
            conn,
            LoginFailurePayload(method="employee_id_pin", known_worker=False),
            workstation_id=STATION,
            site_id=SITE_A,
            now=clock,
        )
    with app.connect() as conn:
        rows = conn.execute(select(AUDIT).order_by(AuditLog.id)).all()
        assert verify_chain(conn).ok
    assert len(rows) == 2
    row = rows[0]
    assert row.id == rec.id
    assert row.action == "worker.login_failure"
    assert row.actor_workstation_id == STATION
    assert row.actor_user_id is None
    assert row.site_id == SITE_A
    assert row.object_type == "user" and row.object_id == str(WORKER)
    assert row.detail == {"method": "employee_id_pin", "known_worker": True, "locked": True}
    other = rows[1]
    assert other.id == unknown.id
    assert other.object_id is None
    assert other.detail == {"method": "employee_id_pin", "known_worker": False, "locked": False}


def test_login_failure_for_an_unknown_worker_never_names_a_user(app: Engine) -> None:
    payload = LoginFailurePayload(method="employee_id_pin", user_id=WORKER, known_worker=False)
    with pytest.raises(AuditError), app.begin() as conn:
        log_login_failure(conn, payload, workstation_id=STATION, site_id=SITE_A)


# --- append-only enforcement -------------------------------------------------


def _expect_rejected(engine: Engine, sql: str, match: str) -> None:
    with pytest.raises(DBAPIError, match=match), engine.begin() as conn:
        conn.execute(text(sql))


def test_trigger_blocks_update_delete_truncate_even_for_the_owner(owner: Engine) -> None:
    _write_many(owner, 2, StepClock())
    _expect_rejected(owner, "UPDATE audit_log SET action = 'x.y'", "append-only")
    _expect_rejected(owner, "DELETE FROM audit_log", "append-only")
    _expect_rejected(owner, "TRUNCATE audit_log", "append-only")
    with owner.connect() as conn:
        assert conn.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 2


def test_trigger_fires_in_replica_mode(owner: Engine) -> None:
    # ENABLE ALWAYS: session_replication_role = replica must not bypass it.
    _write_many(owner, 1, StepClock())
    with pytest.raises(DBAPIError, match="append-only"), owner.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM audit_log"))


def test_update_is_rejected_even_with_the_purge_switch(owner: Engine) -> None:
    _write_many(owner, 1, StepClock())
    with pytest.raises(DBAPIError, match="append-only"), owner.begin() as conn:
        conn.execute(text("SET LOCAL ag.audit_purge = 'on'"))
        conn.execute(text("UPDATE audit_log SET action = 'x.y'"))


def test_privileged_purge_can_delete_with_the_purge_switch(owner: Engine) -> None:
    # The retention job (separate privileged role) deletes old rows this way.
    _write_many(owner, 3, StepClock())
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL ag.audit_purge = 'on'"))
        oldest = conn.execute(select(func.min(AuditLog.id))).scalar_one()
        conn.execute(delete(AuditLog).where(AuditLog.id == oldest))
    with owner.connect() as conn:
        assert conn.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 2


def test_app_role_can_insert_and_select_only(app: Engine, owner: Engine) -> None:
    _write_many(app, 2, StepClock())
    with app.connect() as conn:
        assert conn.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 2
    for sql in (
        "UPDATE audit_log SET action = 'x.y'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
        "ALTER TABLE audit_log DISABLE TRIGGER USER",
        "DROP TRIGGER audit_log_append_only ON audit_log",
    ):
        _expect_rejected(app, sql, "permission denied|must be owner")
    # Even with the purge switch the app role has no DELETE privilege.
    with pytest.raises(ProgrammingError, match="permission denied"), app.begin() as conn:
        conn.execute(text("SET LOCAL ag.audit_purge = 'on'"))
        conn.execute(text("DELETE FROM audit_log"))
    with owner.connect() as conn:
        assert conn.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 2


def test_app_role_has_full_dml_on_ordinary_tables(app: Engine) -> None:
    # Grants are not only for audit_log: the app must still be able to work.
    with app.begin() as conn:
        site = conn.execute(
            text("INSERT INTO site (name, timezone) VALUES ('S', 'UTC') RETURNING id")
        ).scalar_one()
        conn.execute(text("UPDATE site SET name = 'S2' WHERE id = :i"), {"i": site})
        conn.execute(text("DELETE FROM site WHERE id = :i"), {"i": site})
        conn.execute(
            text("INSERT INTO purge_log (kind, cutoff, status) VALUES ('images', now(), 'ok')")
        )
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
    _expect_rejected(app, "DELETE FROM alembic_version", "permission denied")
    _expect_rejected(app, "CREATE TABLE sneaky (id int)", "permission denied")


def test_migration_without_the_app_role_still_installs_the_trigger(
    pg_database_factory: Callable[[], str],
) -> None:
    dsn = pg_database_factory()
    upgrade(dsn, "head", app_role="ag_role_that_does_not_exist")
    engine = create_engine(dsn)
    try:
        _write_many(engine, 1, StepClock())
        _expect_rejected(engine, "DELETE FROM audit_log", "append-only")
    finally:
        engine.dispose()


# --- concurrency -------------------------------------------------------------


def test_concurrent_writers_keep_one_valid_chain_per_site(app: Engine) -> None:
    threads = 8
    per_thread = 15
    clock = StepClock()
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def work(t: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                site = SITE_A if (t + i) % 3 else SITE_B
                with app.begin() as conn:
                    write_audit(conn, _entry(t * 100 + i, site), now=clock)
                    # Hold the transaction briefly so writers really overlap.
                    conn.execute(text("SELECT pg_sleep(0.001)"))
        except BaseException as exc:  # surfaced by the assert below
            errors.append(exc)

    workers = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert errors == []

    with app.connect() as conn:
        result = verify_chain(conn)
        hashes = conn.execute(select(AuditLog.prev_hash, AuditLog.site_id)).all()
    assert result.ok, result.problems
    assert result.rows == threads * per_thread
    # One chain per site: no two rows share a predecessor (no fork).
    for site in (SITE_A, SITE_B):
        prevs = [p for p, s in hashes if s == site]
        assert len(prevs) == len(set(prevs)), "chain forked"
        assert sum(1 for p in prevs if p is None) == 1


# --- tamper detection --------------------------------------------------------


def _tamper(owner: Engine, *sql: str) -> None:
    """Change rows as the owner with the trigger switched off (an attacker with DB superuser)."""
    with owner.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        for statement in sql:
            conn.execute(text(statement))
        conn.execute(text("ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_append_only"))
        conn.execute(text("ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_no_truncate"))


def _ids(owner: Engine) -> list[int]:
    with owner.connect() as conn:
        return list(conn.execute(select(AuditLog.id).order_by(AuditLog.id)).scalars())


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("UPDATE audit_log SET detail = '{{\"n\": 42}}' WHERE id = {mid}", "hash mismatch"),
        ("UPDATE audit_log SET action = 'evidence.export' WHERE id = {mid}", "hash mismatch"),
        ("UPDATE audit_log SET at = at + interval '1 second' WHERE id = {mid}", "hash mismatch"),
        ("UPDATE audit_log SET ip = '10.9.9.9' WHERE id = {mid}", "hash mismatch"),
        ("DELETE FROM audit_log WHERE id = {mid}", "broken link"),
        ("DELETE FROM audit_log WHERE id = {first}", "chain start"),
        (
            "UPDATE audit_log SET site_id = '01890000-0000-7000-8000-00000000000b'"
            " WHERE id = {mid}",
            "hash mismatch",
        ),
        ("UPDATE audit_log SET hash = sha256(hash) WHERE id = {mid}", "hash mismatch"),
        (
            "INSERT INTO audit_log (at, action, site_id, detail, prev_hash, hash)"
            " SELECT now(), 'evidence.view', site_id, '{{}}', hash, sha256(hash)"
            " FROM audit_log WHERE id = {mid}",
            "hash mismatch",
        ),
    ],
)
def test_tampering_is_detected(owner: Engine, tamper: str, reason: str) -> None:
    _write_many(owner, 5, StepClock())
    ids = _ids(owner)
    _tamper(owner, tamper.format(mid=ids[2], first=ids[0]))
    with owner.connect() as conn:
        result = verify_chain(conn)
    assert not result.ok
    assert any(reason in p.reason for p in result.problems), result.problems


def test_purge_anchor_lets_a_purged_chain_verify(owner: Engine) -> None:
    _write_many(owner, 4, StepClock())
    with owner.connect() as conn:
        rows = conn.execute(select(AUDIT).order_by(AuditLog.id)).all()
    anchor = rows[1].hash.hex()
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL ag.audit_purge = 'on'"))
        conn.execute(delete(AuditLog).where(AuditLog.id <= rows[1].id))
    with owner.connect() as conn:
        assert not verify_chain(conn).ok  # no anchor recorded yet
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO purge_log (site_id, kind, cutoff, status, detail)"
                " VALUES (:s, 'audit', now(), 'ok', CAST(:d AS jsonb))"
            ),
            {"s": SITE_A, "d": f'{{"chain_anchor": "{anchor}"}}'},
        )
    with owner.connect() as conn:
        result = verify_chain(conn)
    assert result.ok, result.problems
    assert result.rows == 2


def test_anchor_of_another_site_does_not_count(owner: Engine) -> None:
    _write_many(owner, 3, StepClock())
    with owner.connect() as conn:
        rows = conn.execute(select(AUDIT).order_by(AuditLog.id)).all()
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL ag.audit_purge = 'on'"))
        conn.execute(delete(AuditLog).where(AuditLog.id == rows[0].id))
        conn.execute(
            text(
                "INSERT INTO purge_log (site_id, kind, cutoff, status, detail)"
                " VALUES (:s, 'audit', now(), 'ok', CAST(:d AS jsonb))"
            ),
            {"s": SITE_B, "d": f'{{"chain_anchor": "{rows[0].hash.hex()}"}}'},
        )
    with owner.connect() as conn:
        assert not verify_chain(conn).ok


# --- verify command ----------------------------------------------------------


def test_verify_command_exit_codes(
    owner: Engine, audit_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"AG_DATABASE_URL": audit_dsn}
    assert audit_main(["verify"], env=env) == 0  # empty log is valid
    _write_many(owner, 3, StepClock())
    assert audit_main(["verify"], env=env) == 0
    out = capsys.readouterr().out
    assert "3 rows" in out and "OK" in out

    _tamper(owner, f"UPDATE audit_log SET detail = '{{}}' WHERE id = {_ids(owner)[1]}")  # noqa: S608
    assert audit_main(["verify"], env=env) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and str(_ids(owner)[1]) in out


def test_verify_command_reports_unusable_settings_without_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {"AG_DATABASE_URL": "postgresql+psycopg://u:SuperSecretPw@127.0.0.1:1/x"}
    assert audit_main(["verify"], env=env) == 2
    err = capsys.readouterr().err
    assert "SuperSecretPw" not in err
    assert audit_main(["verify"], env={"AG_ENV": "bogus"}) == 2


def test_verify_command_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        audit_main(["nonsense"], env={})
    assert exc.value.code == 2


def test_python_dash_m_entry_point_exits_nonzero_on_tamper(owner: Engine, audit_dsn: str) -> None:
    _write_many(owner, 2, StepClock())
    env = {
        **os.environ,
        "AG_DATABASE_URL": audit_dsn,
        "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in ("server", "shared")),
    }
    env.pop("AG_DATABASE_URL_FILE", None)
    cmd = [sys.executable, "-m", "ag_server.audit", "verify"]
    ok = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    assert ok.returncode == 0, ok.stderr
    _tamper(owner, f"UPDATE audit_log SET object_id = 'x' WHERE id = {_ids(owner)[0]}")  # noqa: S608
    bad = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    assert bad.returncode == 1
