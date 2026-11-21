"""Seed data: the system roles and the built-in PPE profiles.

Built-in PPE profiles are generated from `ag_shared.ppe.BUILTIN_PROFILES`, so
the signal table exists in one place only. Seeding is idempotent and also
restores drifted rows, so a later migration can call `seed_all` again to roll
out changed definitions.

Lightweight `table()` constructs are used instead of the ORM models so that
migrations calling this module do not depend on the current model classes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ag_shared.ppe import BUILTIN_PROFILES, PpeProfileKind
from sqlalchemy import (
    Boolean,
    ColumnElement,
    Connection,
    Insert,
    Text,
    Uuid,
    cast,
    column,
    literal,
    select,
    table,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB

from ag_server.rbac.permissions import system_role_permissions

# docs/security-privacy.md section 2, from the permission registry (the single
# source). "assigned lines" and "own only" grant the key; the scope filter
# narrows it.
SYSTEM_ROLE_PERMISSIONS: Final[Mapping[str, tuple[str, ...]]] = system_role_permissions()

# Display names from docs/scope-v1.md 4.3 (the console translates by kind).
BUILTIN_PROFILE_NAMES: Final[MappingProxyType[PpeProfileKind, str]] = MappingProxyType(
    {
        PpeProfileKind.OPEN_FACE: "Open face",
        PpeProfileKind.CLEAR_GLASSES: "Clear glasses",
        PpeProfileKind.SUNGLASSES: "Sunglasses",
        PpeProfileKind.MASK: "Mask",
        PpeProfileKind.HELMET_SHIELD: "Helmet / face shield",
        PpeProfileKind.CUSTOM: "Custom (template)",
    }
)

_role = table(
    "role",
    column("id", Uuid),
    column("name", Text),
    column("permissions", JSONB),
    column("is_system", Boolean),
)
_ppe = table(
    "ppe_profile",
    column("id", Uuid),
    column("site_id", Uuid),
    column("name", Text),
    column(
        "kind",
        ENUM(*(k.value for k in PpeProfileKind), name="ppe_profile_kind", create_type=False),
    ),
    column("signals", JSONB),
    column("threshold_profile_id", Uuid),
    column("is_builtin", Boolean),
)


def builtin_signals_json(kind: PpeProfileKind) -> dict[str, dict[str, object]]:
    """The `ppe_profile.signals` document of a built-in profile."""
    profile = BUILTIN_PROFILES[kind]
    return {
        name.value: setting.model_dump(mode="json") for name, setting in profile.signals.items()
    }


def seed_roles(conn: Connection) -> None:
    """Create or restore the three system roles."""
    for name, permissions in SYSTEM_ROLE_PERMISSIONS.items():
        values = {"permissions": sorted(permissions), "is_system": True}
        existing = conn.execute(select(_role.c.id).where(_role.c.name == name)).scalar()
        if existing is None:
            conn.execute(_role.insert().values(name=name, **values))
        else:
            conn.execute(_role.update().where(_role.c.id == existing).values(**values))


def seed_builtin_ppe_profiles(conn: Connection) -> None:
    """Create or restore one built-in profile per `PpeProfileKind`."""
    for kind in BUILTIN_PROFILES:
        values = {
            "name": BUILTIN_PROFILE_NAMES[kind],
            "signals": builtin_signals_json(kind),
            "threshold_profile_id": None,
        }
        where = (_ppe.c.is_builtin.is_(True), _ppe.c.kind == kind.value)
        existing = conn.execute(select(_ppe.c.id).where(*where)).scalar()
        if existing is None:
            conn.execute(
                _ppe.insert().values(site_id=None, kind=kind.value, is_builtin=True, **values)
            )
        else:
            conn.execute(_ppe.update().where(_ppe.c.id == existing).values(**values))


def _jsonb(value: object) -> ColumnElement[object]:
    # A cast text literal: offline (--sql) mode cannot render a JSONB bind value.
    return cast(literal(json.dumps(value, sort_keys=True), Text), JSONB)


def initial_seed_statements() -> list[Insert]:
    """Plain INSERTs for a freshly created schema.

    Used by the initial migration, which must also work in offline (`--sql`)
    mode where nothing can be selected first. Later migrations use `seed_all`.
    """
    statements: list[Insert] = [
        _role.insert().values(name=name, permissions=_jsonb(sorted(perms)), is_system=True)
        for name, perms in SYSTEM_ROLE_PERMISSIONS.items()
    ]
    statements += [
        _ppe.insert().values(
            site_id=None,
            name=BUILTIN_PROFILE_NAMES[kind],
            kind=kind.value,
            signals=_jsonb(builtin_signals_json(kind)),
            threshold_profile_id=None,
            is_builtin=True,
        )
        for kind in BUILTIN_PROFILES
    ]
    return statements


def seed_all(conn: Connection) -> None:
    """Run every seed step in the caller's transaction."""
    seed_roles(conn)
    seed_builtin_ppe_profiles(conn)
