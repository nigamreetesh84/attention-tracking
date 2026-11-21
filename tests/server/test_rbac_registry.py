"""Permission registry (task 3.5): the single source of keys and system-role grants.

The role x permission matrix is parsed from docs/security-privacy.md section 2,
so the registry cannot drift from the design doc unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ag_server import seed
from ag_server.rbac import (
    SYSTEM_ROLE_GRANTS,
    GrantLevel,
    Permission,
    effective_permissions,
    grant_level,
    require,
    system_role_permissions,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "security-privacy.md"
ROLE_COLUMNS = {
    "Admin": "admin",
    "Supervisor": "supervisor",
    "Workstation user": "workstation_user",
}


def _cell_level(cell: str) -> GrantLevel | None:
    text = cell.strip().lower()
    if text == "no":
        return None
    if text.startswith("yes"):
        return GrantLevel.ALL
    if text == "assigned lines":
        return GrantLevel.ASSIGNED_LINES
    if text.startswith("own only"):
        return GrantLevel.OWN_ONLY
    raise AssertionError(f"unknown matrix cell {cell!r}: teach the test or fix the doc")


def doc_matrix() -> dict[str, dict[str, GrantLevel]]:
    """{role: {permission key: level}} from the table in section 2."""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## 2. Roles and server-side RBAC", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    assert header[0] == "Permission"
    roles = [ROLE_COLUMNS[h] for h in header[1:]]
    matrix: dict[str, dict[str, GrantLevel]] = {r: {} for r in roles}
    for row in rows[2:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        keys = re.findall(r"`([a-z.]+)`", cells[0])
        assert keys, row
        for role, cell in zip(roles, cells[1:], strict=True):
            level = _cell_level(cell)
            if level is not None:
                for key in keys:
                    matrix[role][key] = level
    return matrix


def test_the_doc_table_is_parsed() -> None:
    matrix = doc_matrix()
    # Sanity: the parser found the whole table, including the shared row.
    assert len({k for grants in matrix.values() for k in grants}) == 14
    assert matrix["admin"]["evidence.delete.approve"] is GrantLevel.ALL


def test_every_permission_key_appears_in_the_doc_and_nothing_else() -> None:
    doc_keys = {k for grants in doc_matrix().values() for k in grants}
    assert {p.value for p in Permission} == doc_keys


def test_role_by_permission_matrix_matches_the_doc() -> None:
    expected = doc_matrix()
    actual = {
        role: {p.value: level for p, level in grants.items()}
        for role, grants in SYSTEM_ROLE_GRANTS.items()
    }
    assert actual == expected


@pytest.mark.parametrize("role", ["admin", "supervisor", "workstation_user"])
@pytest.mark.parametrize("permission", list(Permission))
def test_grant_level_cell_by_cell(role: str, permission: Permission) -> None:
    assert grant_level(role, permission) is doc_matrix()[role].get(permission.value)


def test_unknown_role_has_no_grant() -> None:
    assert all(grant_level("auditor", p) is None for p in Permission)


def test_seed_uses_the_registry() -> None:
    # The seed module keeps no copy of the matrix of its own.
    assert system_role_permissions() == seed.SYSTEM_ROLE_PERMISSIONS
    assert {role: {p.value for p in grants} for role, grants in SYSTEM_ROLE_GRANTS.items()} == {
        role: set(keys) for role, keys in system_role_permissions().items()
    }
    for keys in system_role_permissions().values():
        assert list(keys) == sorted(keys)


def test_effective_permissions_ignore_unknown_keys() -> None:
    assert effective_permissions("supervisor", ["live.view", "root", "", 7]) == {
        Permission.LIVE_VIEW
    }


def test_a_tampered_system_role_row_cannot_widen_the_matrix() -> None:
    # A Supervisor row edited in the database to hold audit.view gains nothing.
    stored = ["live.view", "audit.view", "users.manage"]
    assert effective_permissions("supervisor", stored) == {Permission.LIVE_VIEW}


def test_a_system_role_row_can_narrow_the_matrix() -> None:
    assert effective_permissions("admin", ["audit.view"]) == {Permission.AUDIT_VIEW}


def test_require_accepts_only_registry_permissions() -> None:
    with pytest.raises(ValueError, match="unknown permission"):
        require("audit.viewer")  # type: ignore[arg-type]  # deliberately wrong key
    # The string form of a registry key is accepted and normalised.
    assert require("audit.view") is require(Permission.AUDIT_VIEW)  # type: ignore[arg-type]  # str form


def test_require_returns_one_dependency_per_permission() -> None:
    assert require(Permission.LIVE_VIEW) is require(Permission.LIVE_VIEW)
    assert require(Permission.LIVE_VIEW) is not require(Permission.EVENTS_REVIEW)
