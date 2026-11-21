"""A static check that route modules use the scope helpers (task 3.5).

In a module that defines routes, these are flagged when their first argument
is a scoped model (`ag_server.rbac.scope.SCOPED_MODELS`), written as `Model`,
`Model.column` or `models.Model`:

- `select(...)`, `update(...)`, `delete(...)`, `insert(...)`
- `<session>.get(Model, ...)`

and any `<session>.query(...)` call. Route code uses `scoped_select` and
`get_scoped` (or a service that does). A justified exception carries
`# rbac: unscoped-ok <reason>` on the flagged line.

It is a lint, not a proof: a query built in a service module is not
checked here, which is why the scope helpers are the documented way.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ag_server.rbac.scope import SCOPED_MODELS

EXEMPT_MARK: Final = "# rbac: unscoped-ok"
_ROUTE_DECORATORS: Final = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "websocket"}
)
_ROUTE_CALLS: Final = frozenset({"add_api_route", "add_api_websocket_route", "add_route"})
_STATEMENTS: Final = frozenset({"select", "update", "delete", "insert"})
_SCOPED_NAMES: Final = frozenset(m.__name__ for m in SCOPED_MODELS)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def is_route_module(source: str) -> bool:
    """True if the module declares routes (decorators or `add_api_route`)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr in _ROUTE_DECORATORS
                ):
                    return True
        if isinstance(node, ast.Call) and _call_name(node) in _ROUTE_CALLS:
            return True
    return False


def _model_name(node: ast.expr) -> str | None:
    """`Model`, `Model.col`, `models.Model` or `models.Model.col` -> the model name."""
    while True:
        if isinstance(node, ast.Name):
            return node.id if node.id in _SCOPED_NAMES else None
        if isinstance(node, ast.Attribute):
            if node.attr in _SCOPED_NAMES:
                return node.attr
            node = node.value
            continue
        return None


def find_unscoped_queries(source: str, path: str) -> list[Finding]:
    """Direct queries on scoped models in one route module's source."""
    lines = source.splitlines()
    findings: list[Finding] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        message: str | None = None
        if name == "query" and isinstance(node.func, ast.Attribute):
            message = "Session.query: use scoped_select"
        elif name in _STATEMENTS and isinstance(node.func, ast.Name):
            model = next((m for m in map(_model_name, node.args) if m), None)
            if model is not None:
                message = f"{name}({model}) on a scoped table: use scoped_select/get_scoped"
        elif name == "get" and isinstance(node.func, ast.Attribute) and node.args:
            model = _model_name(node.args[0])
            if model is not None:
                message = f"get({model}, ...) on a scoped table: use get_scoped"
        if message is None:
            continue
        text = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        if EXEMPT_MARK in text:
            continue
        findings.append(Finding(path, node.lineno, message))
    return findings


def scan_package(root: Path) -> tuple[list[Path], list[Finding]]:
    """Check every route module under `root`; returns (route modules, findings)."""
    checked: list[Path] = []
    findings: list[Finding] = []
    for file in sorted(root.rglob("*.py")):
        if "migrations" in file.parts:
            continue
        source = file.read_text(encoding="utf-8")
        if not is_route_module(source):
            continue
        checked.append(file)
        findings += find_unscoped_queries(source, str(file))
    return checked, findings
