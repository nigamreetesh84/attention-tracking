"""Route access declarations (security-privacy 2): default deny.

Every route declares exactly one of:

- `require(Permission.X)`: a complete console session whose role holds X
  now (read from the database on every request). Returns a `Principal`
  with the request's `Scope`. No session -> login (401 for non-GET/HTMX),
  MFA still owed -> MFA step (403), inactive user or workstation-user role
  -> no session, permission missing -> 403.
- `PUBLIC` (`dependencies=[PUBLIC]`): no authentication; only health probes
  and the sign-in form.
- `device(authenticate)`: a device-token route (task 3.6). The marker wraps
  the authenticating dependency, so it cannot be applied without it.
- `signed_in()` / `signed_in(pending_ok=True)`: a console session with no
  permission, for the user's own session only (the MFA steps, logout and
  the landing page). It never reads scoped data.

The declaration is an attribute of the dependency callable, so
`route_access` can find it in each route's dependency tree; the test suite
walks the real app and fails on any route without exactly one declaration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Request
from fastapi.params import Depends as DependsParam
from fastapi.responses import PlainTextResponse, Response
from fastapi.routing import APIRoute

from ag_server.auth.deps import DbSession, current_session, pending_session
from ag_server.auth.service import ConsoleSession
from ag_server.rbac.permissions import Permission, effective_permissions, parse_permission
from ag_server.rbac.scope import Scope, load_scope

ACCESS_ATTR: Final = "__ag_access__"


class AccessKind(StrEnum):
    PUBLIC = "public"
    DEVICE = "device"
    SIGNED_IN = "signed_in"
    PERMISSION = "permission"


@dataclass(frozen=True)
class Access:
    """One route's access declaration."""

    kind: AccessKind
    permission: Permission | None = None
    pending_ok: bool = False

    def __post_init__(self) -> None:
        if (self.kind is AccessKind.PERMISSION) != (self.permission is not None):
            raise ValueError("a permission declaration needs exactly one permission")
        if self.pending_ok and self.kind is not AccessKind.SIGNED_IN:
            raise ValueError("pending_ok applies to signed_in only")

    def openapi(self) -> dict[str, object]:
        """The `x-ag-access` value in the OpenAPI document."""
        out: dict[str, object] = {"kind": self.kind.value}
        if self.permission is not None:
            out["permission"] = self.permission.value
        if self.pending_ok:
            out["pending_ok"] = True
        return out


def _mark(func: Callable[..., Any], access: Access) -> None:
    # Any: dependencies have arbitrary FastAPI signatures.
    setattr(func, ACCESS_ATTR, access)


def access_of(func: object) -> Access | None:
    value = getattr(func, ACCESS_ATTR, None)
    return value if isinstance(value, Access) else None


# --- errors --------------------------------------------------------------------------------


class PermissionDeniedError(Exception):
    """A complete session without the route's permission: 403."""


async def _permission_denied(_request: Request, _exc: Exception) -> Response:
    response = PlainTextResponse("Forbidden", status_code=403)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# --- declarations -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """The caller of a permission route: who, which permission, and what they may see."""

    session: ConsoleSession
    permission: Permission
    scope: Scope


@cache
def _require(permission: Permission) -> Callable[..., Any]:
    async def dependency(
        session: Annotated[ConsoleSession, Depends(current_session)], db: DbSession
    ) -> Principal:
        if permission not in effective_permissions(session.role, session.permissions):
            raise PermissionDeniedError
        return Principal(session, permission, load_scope(db, session, permission))

    dependency.__name__ = f"require_{permission.value.replace('.', '_')}"
    _mark(dependency, Access(AccessKind.PERMISSION, permission))
    return dependency


def require(permission: Permission) -> Callable[..., Any]:
    """The dependency for a route that needs `permission`; one object per permission.

    Use as `p: Annotated[Principal, Depends(require(Permission.X))]` and pass
    `p.scope` to `scoped_select` / `get_scoped`.
    """
    return _require(parse_permission(permission))


def _public() -> None:
    """Marks a route as intentionally unauthenticated."""


_mark(_public, Access(AccessKind.PUBLIC))
PUBLIC: Final[DependsParam] = Depends(_public)


@cache
def signed_in(*, pending_ok: bool = False) -> Callable[..., Any]:
    """A console session without a permission check (own-session routes only)."""
    if pending_ok:

        async def pending(
            session: Annotated[ConsoleSession, Depends(pending_session)],
        ) -> ConsoleSession:
            return session

        _mark(pending, Access(AccessKind.SIGNED_IN, pending_ok=True))
        return pending

    async def complete(
        session: Annotated[ConsoleSession, Depends(current_session)],
    ) -> ConsoleSession:
        return session

    _mark(complete, Access(AccessKind.SIGNED_IN))
    return complete


@cache
def device(authenticate: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a device-token route by wrapping its authenticating dependency (task 3.6).

    The returned dependency runs `authenticate` and returns its result.
    """

    # A default, not Annotated: string annotations cannot see the closure variable.
    def dependency(identity: object = Depends(authenticate)) -> object:
        return identity

    dependency.__name__ = f"device_{getattr(authenticate, '__name__', 'auth')}"
    _mark(dependency, Access(AccessKind.DEVICE))
    return dependency


# --- the route table ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteAccess:
    """One served route and the access declarations found in its dependency tree."""

    path: str
    methods: frozenset[str]
    api: bool
    declarations: tuple[Access, ...]
    kind: str


def _declarations(dependant: Any) -> tuple[Access, ...]:
    # Any: fastapi.dependencies.models.Dependant is internal and changes shape.
    found: list[Access] = []
    stack = [dependant]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        access = access_of(getattr(node, "call", None))
        if access is not None and access not in found:
            found.append(access)
        stack.extend(getattr(node, "dependencies", ()))
    return tuple(found)


def _iter_contexts(app: FastAPI) -> Iterator[Any]:
    # FastAPI 0.165 keeps included routers as `_IncludedRouter` entries in
    # `app.routes`; `iter_route_contexts` expands them with the effective
    # path and dependencies. Older versions flatten the routes themselves.
    from fastapi import routing

    expand = getattr(routing, "iter_route_contexts", None)
    if expand is None:  # pragma: no cover - older FastAPI
        yield from app.routes
    else:
        yield from expand(app.routes)


def _framework_paths(app: FastAPI) -> frozenset[str]:
    """FastAPI's own docs routes (disabled in production)."""
    paths = {app.openapi_url, app.docs_url, app.redoc_url}
    if app.docs_url and app.swagger_ui_oauth2_redirect_url:
        paths.add(app.swagger_ui_oauth2_redirect_url)
    return frozenset(p for p in paths if p)


def route_access(app: FastAPI) -> list[RouteAccess]:
    """Every route the app serves, with its declarations."""
    framework = _framework_paths(app)
    out: list[RouteAccess] = []
    for ctx in _iter_contexts(app):
        original = getattr(ctx, "original_route", ctx)
        path = str(getattr(ctx, "path_format", None) or getattr(ctx, "path", "") or "")
        methods = frozenset(getattr(ctx, "methods", None) or ())
        if isinstance(original, APIRoute):
            out.append(RouteAccess(path, methods, True, _declarations(ctx.dependant), "api"))
        else:
            kind = "framework" if path in framework else type(original).__name__
            out.append(RouteAccess(path, methods, False, (), kind))
    return out


def undeclared_routes(app: FastAPI) -> list[str]:
    """Problems, one line each: routes without exactly one declaration, or unknown routes."""
    problems: list[str] = []
    for route in route_access(app):
        label = f"{' '.join(sorted(route.methods)) or '*'} {route.path}"
        if not route.api:
            if route.kind != "framework":
                problems.append(f"{label}: not an API route ({route.kind}); declare it in rbac")
        elif not route.declarations:
            problems.append(f"{label}: no access declaration")
        elif len(route.declarations) > 1:
            problems.append(f"{label}: more than one access declaration")
    return problems


def install_rbac(app: FastAPI) -> None:
    """Register the 403 handler and add `x-ag-access` to every OpenAPI operation."""
    app.add_exception_handler(PermissionDeniedError, _permission_denied)
    build_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        # Any: the OpenAPI document is free-form JSON.
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = build_openapi()
        for route in route_access(app):
            if len(route.declarations) != 1:
                continue
            ops = schema.get("paths", {}).get(route.path, {})
            for method in route.methods:
                op = ops.get(method.lower())
                if op is not None:
                    op["x-ag-access"] = route.declarations[0].openapi()
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI's documented override
