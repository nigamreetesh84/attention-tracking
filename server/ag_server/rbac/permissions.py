"""Permission registry: the single source of permission keys and system-role grants.

The matrix is `docs/security-privacy.md` section 2; a test parses the doc and
compares it cell by cell. The seed data (`ag_server.seed`) is generated from
this module, so the roles in the database and the checks in `require` cannot
disagree about the keys.

This module imports nothing from FastAPI or the models, so migrations can use
it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Permission(StrEnum):
    """Every permission key. A route can only require one of these."""

    CONFIG_MANAGE = "config.manage"
    USERS_MANAGE = "users.manage"
    CONSENT_MANAGE = "consent.manage"
    LIVE_VIEW = "live.view"
    REPORTS_VIEW = "reports.view"
    EVENTS_REVIEW = "events.review"
    EVIDENCE_VIEW = "evidence.view"
    EVIDENCE_DELETE_REQUEST = "evidence.delete.request"
    EVIDENCE_DELETE_APPROVE = "evidence.delete.approve"
    ALERTS_MANAGE = "alerts.manage"
    WORKERS_ASSIGN = "workers.assign"
    AUDIT_VIEW = "audit.view"
    SYSTEM_MANAGE = "system.manage"
    SCAN = "scan"


class GrantLevel(StrEnum):
    """How far a granted permission reaches (the matrix's cell values)."""

    # "yes": every site the user's site scope covers (null site = all sites).
    ALL = "all"
    # "assigned lines": the Supervisor's lines in `supervisor_line`.
    ASSIGNED_LINES = "assigned_lines"
    # "own only, on the workstation": served by the device API, never the console.
    OWN_ONLY = "own_only"


_ALL: Final = GrantLevel.ALL
_LINES: Final = GrantLevel.ASSIGNED_LINES

SYSTEM_ROLE_GRANTS: Final[Mapping[str, Mapping[Permission, GrantLevel]]] = MappingProxyType(
    {
        "admin": MappingProxyType(dict.fromkeys(Permission, _ALL)),
        "supervisor": MappingProxyType(
            {
                Permission.LIVE_VIEW: _LINES,
                Permission.REPORTS_VIEW: _LINES,
                Permission.EVENTS_REVIEW: _LINES,
                Permission.EVIDENCE_VIEW: _LINES,
                Permission.ALERTS_MANAGE: _LINES,
                Permission.WORKERS_ASSIGN: _LINES,
            }
        ),
        "workstation_user": MappingProxyType(
            {Permission.REPORTS_VIEW: GrantLevel.OWN_ONLY, Permission.SCAN: _ALL}
        ),
    }
)


def system_role_permissions() -> Mapping[str, tuple[str, ...]]:
    """{role name: sorted permission keys}: what the seed stores in `role.permissions`."""
    return MappingProxyType(
        {
            role: tuple(sorted(p.value for p in grants))
            for role, grants in SYSTEM_ROLE_GRANTS.items()
        }
    )


def grant_level(role: str, permission: Permission) -> GrantLevel | None:
    """The matrix cell for a system role; None for "no" and for any other role."""
    grants = SYSTEM_ROLE_GRANTS.get(role)
    return None if grants is None else grants.get(permission)


def effective_permissions(role: str, stored: Iterable[object]) -> frozenset[Permission]:
    """The permissions a role really has.

    `stored` is `role.permissions` from the database, read on every request so
    a change applies at once. Unknown keys are ignored. For a system role the
    result never exceeds the registry: an edited row can narrow the role but
    not widen it.
    """
    known = {Permission(k) for k in stored if isinstance(k, str) and k in _KEYS}
    grants = SYSTEM_ROLE_GRANTS.get(role)
    if grants is not None:
        known &= set(grants)
    return frozenset(known)


_KEYS: Final = frozenset(p.value for p in Permission)


def parse_permission(value: Permission | str) -> Permission:
    """A registry permission, or ValueError for any other key."""
    if isinstance(value, Permission):
        return value
    if value in _KEYS:
        return Permission(value)
    raise ValueError(f"unknown permission {value!r}: add it to ag_server.rbac.permissions")
