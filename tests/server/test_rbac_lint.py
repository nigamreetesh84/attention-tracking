"""Route modules must read scoped tables only through the scope helpers (task 3.5).

`find_unscoped_queries` is an AST check over every module of `ag_server` that
defines routes. It flags `select(...)`, `update(...)`, `delete(...)`,
`insert(...)` and `Session.get(...)` on a scoped model, and any
`.query(...)`. Route code uses `scoped_select` / `get_scoped`; a justified
exception carries `# rbac: unscoped-ok <reason>` on the same line.
"""

from __future__ import annotations

from pathlib import Path

from ag_server.rbac.lint import find_unscoped_queries, is_route_module, scan_package

SERVER = Path(__file__).resolve().parents[2] / "server" / "ag_server"

BAD = """
from fastapi import APIRouter
from sqlalchemy import select, update, delete
from ag_server.models import Workstation, AttentionEvent, Role
from ag_server import models

router = APIRouter()

@router.get("/x")
def x(db):
    a = db.execute(select(Workstation)).all()
    b = db.execute(select(AttentionEvent.id).where(AttentionEvent.id == 1))
    c = db.get(Workstation, 1)
    d = db.query(Workstation).all()
    e = db.execute(update(models.AttentionEvent).values(escalated=True))
    f = db.execute(delete(Workstation))
    g = db.execute(select(Role))  # global model: fine
    h = db.execute(select(Workstation))  # rbac: unscoped-ok counted for a health probe
    return a, b, c, d, e, f, g, h
"""

GOOD = """
from fastapi import APIRouter
from ag_server.models import Workstation
from ag_server.rbac import get_scoped, scoped_select

router = APIRouter()

@router.get("/x")
def x(db, p):
    rows = db.execute(scoped_select(Workstation, p.scope)).all()
    return rows, get_scoped(db, Workstation, 1, p.scope)
"""

NOT_ROUTES = """
from sqlalchemy import select
from ag_server.models import Workstation

def service(db):
    return db.execute(select(Workstation)).all()
"""


def test_the_check_flags_each_direct_query_on_a_scoped_model() -> None:
    problems = find_unscoped_queries(BAD, "bad.py")
    lines = sorted(p.line for p in problems)
    assert lines == [11, 12, 13, 14, 15, 16], problems
    assert all("bad.py" in str(p) for p in problems)


def test_scope_helpers_pass() -> None:
    assert find_unscoped_queries(GOOD, "good.py") == []


def test_route_module_detection() -> None:
    assert is_route_module(BAD)
    assert is_route_module(GOOD)
    assert not is_route_module(NOT_ROUTES)
    assert is_route_module("def f(app):\n    app.add_api_route('/x', f)\n")
    assert is_route_module("@app.post('/y')\ndef y():\n    pass\n")


def test_no_route_module_of_the_server_queries_scoped_tables_directly() -> None:
    checked, problems = scan_package(SERVER)
    assert problems == []
    # The scan really looked at the route modules that exist today.
    names = {p.relative_to(SERVER).as_posix() for p in checked}
    assert {"app.py", "auth/routes.py"} <= names
