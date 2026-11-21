"""Server-side RBAC and scoping (task 3.5, security-privacy 2).

- `Permission`, `SYSTEM_ROLE_GRANTS`: the permission registry (single source
  of keys and system-role grants; the seed is generated from it).
- `require(Permission.X)`, `PUBLIC`, `device(...)`, `signed_in()`: the only
  ways a route declares access. Default deny; a test walks the route table.
- `Scope`, `scoped_select`, `get_scoped`, `scope_filter`: every console query
  of a scoped table goes through these. Out of scope is 404, like missing.
- `ag_server.rbac.lint`: static check that route modules use the helpers.
"""

from ag_server.rbac.access import (
    PUBLIC,
    Access,
    AccessKind,
    PermissionDeniedError,
    Principal,
    RouteAccess,
    device,
    install_rbac,
    require,
    route_access,
    signed_in,
    undeclared_routes,
)
from ag_server.rbac.permissions import (
    SYSTEM_ROLE_GRANTS,
    GrantLevel,
    Permission,
    effective_permissions,
    grant_level,
    parse_permission,
    system_role_permissions,
)
from ag_server.rbac.scope import (
    GLOBAL_MODELS,
    SCOPED_MODELS,
    NotFoundError,
    Scope,
    get_scoped,
    load_scope,
    scope_filter,
    scoped_select,
)

__all__ = [
    "GLOBAL_MODELS",
    "PUBLIC",
    "SCOPED_MODELS",
    "SYSTEM_ROLE_GRANTS",
    "Access",
    "AccessKind",
    "GrantLevel",
    "NotFoundError",
    "Permission",
    "PermissionDeniedError",
    "Principal",
    "RouteAccess",
    "Scope",
    "device",
    "effective_permissions",
    "get_scoped",
    "grant_level",
    "install_rbac",
    "load_scope",
    "parse_permission",
    "require",
    "route_access",
    "scope_filter",
    "scoped_select",
    "signed_in",
    "system_role_permissions",
    "undeclared_routes",
]
