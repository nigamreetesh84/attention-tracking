"""Seed data: the three system roles and the built-in PPE profiles."""

from __future__ import annotations

from ag_server.models import PpeProfile, Role
from ag_server.seed import SYSTEM_ROLE_PERMISSIONS, seed_all
from ag_shared.ppe import BUILTIN_PROFILES, PpeProfileKind
from ag_shared.ppe import PpeProfile as SharedPpeProfile
from sqlalchemy import Engine, func, select, update

# Copied from the matrix in docs/security-privacy.md section 2 ("yes",
# "assigned lines" and "own only" all grant the key; scoping is separate).
MATRIX = {
    "admin": {
        "config.manage",
        "users.manage",
        "consent.manage",
        "live.view",
        "reports.view",
        "events.review",
        "evidence.view",
        "evidence.delete.request",
        "evidence.delete.approve",
        "alerts.manage",
        "workers.assign",
        "audit.view",
        "system.manage",
        "scan",
    },
    "supervisor": {
        "live.view",
        "reports.view",
        "events.review",
        "evidence.view",
        "alerts.manage",
        "workers.assign",
    },
    "workstation_user": {"reports.view", "scan"},
}


def test_role_permission_constant_matches_the_security_matrix() -> None:
    assert {name: set(keys) for name, keys in SYSTEM_ROLE_PERMISSIONS.items()} == MATRIX


def test_migration_seeds_the_three_system_roles(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows = conn.execute(select(Role.name, Role.permissions, Role.is_system)).all()
    assert {r.name: set(r.permissions) for r in rows} == MATRIX
    assert all(r.is_system for r in rows)
    for r in rows:
        assert r.permissions == sorted(r.permissions), "stored sorted for stable diffs"


def test_migration_seeds_every_builtin_ppe_profile_from_ag_shared(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as conn:
        profiles = conn.execute(select(PpeProfile.__table__).where(PpeProfile.is_builtin)).all()
    assert {p.kind for p in profiles} == {k.value for k in BUILTIN_PROFILES}
    for p in profiles:
        assert p.site_id is None, "built-ins belong to no site"
        assert p.threshold_profile_id is None
        assert p.name
        builtin = BUILTIN_PROFILES[p.kind]
        # The stored signals rebuild exactly the shared definition: no copy of
        # the table lives in the server. `glare_tolerance` has no column in
        # schema.md (see PROGRESS 3.2), so it comes from the shared built-in.
        rebuilt = SharedPpeProfile.model_validate(
            {
                "kind": p.kind,
                "signals": p.signals,
                "glare_tolerance": builtin.glare_tolerance,
            }
        )
        assert rebuilt == builtin


def test_seeding_again_is_idempotent(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        seed_all(conn)
        seed_all(conn)
    with migrated_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Role)).scalar_one() == 3
        builtin_count = conn.execute(
            select(func.count()).select_from(PpeProfile).where(PpeProfile.is_builtin)
        ).scalar_one()
    assert builtin_count == len(BUILTIN_PROFILES)


def test_seeding_restores_a_drifted_builtin(migrated_engine: Engine) -> None:
    # A later migration re-runs the seed to roll out changed definitions.
    with migrated_engine.begin() as conn:
        conn.execute(
            update(PpeProfile)
            .where(PpeProfile.is_builtin, PpeProfile.kind == "mask")
            .values(signals={})
        )
        conn.execute(update(Role).where(Role.name == "supervisor").values(permissions=[]))
        seed_all(conn)
    with migrated_engine.connect() as conn:
        mask = conn.execute(
            select(PpeProfile.signals).where(PpeProfile.is_builtin, PpeProfile.kind == "mask")
        ).scalar_one()
        perms = conn.execute(select(Role.permissions).where(Role.name == "supervisor")).scalar_one()
    rebuilt = SharedPpeProfile.model_validate({"kind": "mask", "signals": mask})
    assert rebuilt == BUILTIN_PROFILES[PpeProfileKind.MASK]
    assert set(perms) == MATRIX["supervisor"]
