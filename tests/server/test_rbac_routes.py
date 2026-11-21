"""Every route declares exactly one access requirement (task 3.5, security-privacy 2).

The route table is walked three ways, so a route cannot hide from the check:

1. `route_access(app)`: the effective routes FastAPI serves (included
   routers are expanded; FastAPI 0.165 keeps them out of `app.routes`).
2. The OpenAPI document: every operation must be one of the walked routes.
3. An independent recursion over the router tree (`APIRouter.routes` and the
   routers included in it), so a route left out of the schema
   (`include_in_schema=False`) or of the effective view is still counted.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import pytest
from ag_server.app import create_app
from ag_server.rbac import (
    PUBLIC,
    Access,
    AccessKind,
    Permission,
    Principal,
    device,
    require,
    route_access,
    undeclared_routes,
)
from ag_server.settings import load_settings
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Mount

UNREACHABLE = "postgresql+psycopg://nobody@127.0.0.1:1/none?connect_timeout=1"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def make_app(env: str = "test") -> FastAPI:
    overrides = {"AG_ENV": env, "AG_DATABASE_URL": UNREACHABLE}
    if env == "production":
        overrides |= {
            "AG_KEK": "k" * 8 + "Zx9" * 12,
            "AG_JWT_SIGNING_KEY": "j" * 8 + "Qw3" * 12,
            "AG_DATABASE_URL": "postgresql+psycopg://ag:" + "Rt7" * 10 + "@127.0.0.1:1/ag",
        }
    return create_app(load_settings(overrides))


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return make_app()


def _tree_api_routes(routes: list[BaseRoute], prefix: str = "") -> Iterator[tuple[str, str]]:
    """(method, path) of every APIRoute, recursing into included routers by hand."""
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:  # FastAPI 0.165+ `_IncludedRouter`
            context = route.include_context  # type: ignore[attr-defined]  # private FastAPI type
            yield from _tree_api_routes(original.routes, prefix + (context.prefix or ""))
        elif isinstance(route, APIRoute):
            for method in route.methods or ():
                yield method, prefix + route.path_format


# --- the real application -------------------------------------------------------------


def test_every_route_of_the_real_app_declares_access(app: FastAPI) -> None:
    assert undeclared_routes(app) == []


def test_real_app_declarations(app: FastAPI) -> None:
    found = {(m, r.path): r.declarations[0] for r in route_access(app) if r.api for m in r.methods}
    public = Access(AccessKind.PUBLIC)
    assert found[("GET", "/healthz")] == public
    assert found[("GET", "/readyz")] == public
    assert found[("GET", "/auth/login")] == public
    assert found[("POST", "/auth/login")] == public
    for key in [
        ("GET", "/auth/mfa/verify"),
        ("POST", "/auth/mfa/verify"),
        ("GET", "/auth/mfa/enroll"),
        ("POST", "/auth/mfa/enroll"),
        ("POST", "/auth/logout"),
    ]:
        assert found[key] == Access(AccessKind.SIGNED_IN, pending_ok=True), key
    assert found[("GET", "/")] == Access(AccessKind.SIGNED_IN)


def test_openapi_operations_are_all_walked_and_carry_the_declaration(app: FastAPI) -> None:
    walked = {(m.lower(), r.path): r for r in route_access(app) for m in r.methods}
    schema = app.openapi()
    operations = 0
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if method not in HTTP_METHODS:
                continue
            operations += 1
            route = walked.get((method, path))
            assert route is not None, f"{method.upper()} {path} is in OpenAPI but not walked"
            assert len(route.declarations) == 1
            assert op["x-ag-access"] == route.declarations[0].openapi()
    assert operations >= 10


def test_router_tree_recursion_finds_the_same_routes(app: FastAPI) -> None:
    tree = set(_tree_api_routes(app.router.routes))
    walked = {(m, r.path) for r in route_access(app) if r.api for m in r.methods}
    assert tree == walked
    assert ("POST", "/auth/login") in tree, "included routers must be expanded"


def test_production_app_has_no_framework_routes_and_still_passes() -> None:
    prod = make_app("production")
    assert undeclared_routes(prod) == []
    assert all(r.api for r in route_access(prod))


# --- the check catches what it must ----------------------------------------------------


def _undeclared_paths(app: FastAPI) -> list[str]:
    return undeclared_routes(app)


def test_an_undeclared_app_route_is_caught() -> None:
    app = make_app()

    @app.get("/sneaky")
    def sneaky() -> dict[str, str]:
        return {}

    problems = _undeclared_paths(app)
    assert len(problems) == 1
    assert "GET /sneaky" in problems[0]
    assert "no access declaration" in problems[0]


def test_an_undeclared_route_in_a_nested_included_router_is_caught() -> None:
    app = make_app()
    inner = APIRouter()
    outer = APIRouter()

    @inner.post("/hidden", include_in_schema=False)
    def hidden() -> None:
        return None

    outer.include_router(inner, prefix="/inner")
    app.include_router(outer, prefix="/outer")
    problems = _undeclared_paths(app)
    assert len(problems) == 1
    assert "POST /outer/inner/hidden" in problems[0]
    assert ("POST", "/outer/inner/hidden") in set(_tree_api_routes(app.router.routes))


def test_a_route_with_two_declarations_is_caught() -> None:
    app = make_app()

    @app.get("/both", dependencies=[PUBLIC])
    def both(p: Annotated[Principal, Depends(require(Permission.AUDIT_VIEW))]) -> None:
        return None

    @app.get("/two-permissions", dependencies=[Depends(require(Permission.AUDIT_VIEW))])
    def two(p: Annotated[Principal, Depends(require(Permission.LIVE_VIEW))]) -> None:
        return None

    problems = _undeclared_paths(app)
    assert len(problems) == 2
    assert all("more than one access declaration" in p for p in problems)


def test_a_mounted_sub_application_is_caught() -> None:
    app = make_app()
    sub = FastAPI()
    app.mount("/files", sub)
    problems = _undeclared_paths(app)
    assert any("/files" in p and "not an API route" in p for p in problems)
    assert isinstance(
        next(r for r in app.router.routes if getattr(r, "path", "") == "/files"), Mount
    )


def test_the_same_permission_twice_counts_as_one_declaration() -> None:
    app = make_app()
    dep = require(Permission.LIVE_VIEW)

    @app.get("/same", dependencies=[Depends(dep)])
    def same(p: Principal = Depends(dep)) -> None:  # noqa: B008
        return None

    assert _undeclared_paths(app) == []


def test_router_level_declarations_count() -> None:
    app = make_app()
    router = APIRouter(dependencies=[Depends(require(Permission.REPORTS_VIEW))])

    @router.get("/reports/x")
    def report() -> None:
        return None

    app.include_router(router)
    assert _undeclared_paths(app) == []
    found = {r.path: r for r in route_access(app)}
    assert found["/reports/x"].declarations == (
        Access(AccessKind.PERMISSION, Permission.REPORTS_VIEW),
    )


def test_device_marker_wraps_a_real_authenticating_dependency() -> None:
    calls: list[str] = []

    def fake_device_token() -> str:
        calls.append("checked")
        return "device-1"

    marked = device(fake_device_token)
    app = make_app()

    # A default, not Annotated: string annotations cannot see the local `marked`.
    @app.post("/v1/fake-device-route")
    def route(d: str = Depends(marked)) -> dict[str, str]:
        return {"device": d}

    assert _undeclared_paths(app) == []
    found = {r.path: r for r in route_access(app)}
    assert found["/v1/fake-device-route"].declarations == (Access(AccessKind.DEVICE),)
    assert device(fake_device_token) is marked, "one marker per authenticator"

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/v1/fake-device-route").json() == {"device": "device-1"}
    assert calls == ["checked"], "the marker runs the authenticator"


def test_access_openapi_form() -> None:
    assert Access(AccessKind.PUBLIC).openapi() == {"kind": "public"}
    assert Access(AccessKind.PERMISSION, Permission.EVIDENCE_VIEW).openapi() == {
        "kind": "permission",
        "permission": "evidence.view",
    }
    assert Access(AccessKind.SIGNED_IN, pending_ok=True).openapi() == {
        "kind": "signed_in",
        "pending_ok": True,
    }


def test_access_rejects_inconsistent_forms() -> None:
    with pytest.raises(ValueError):
        Access(AccessKind.PERMISSION)
    with pytest.raises(ValueError):
        Access(AccessKind.PUBLIC, Permission.AUDIT_VIEW)
    with pytest.raises(ValueError):
        Access(AccessKind.PUBLIC, pending_ok=True)
