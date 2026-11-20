"""Repository-wide i18n checks: keys used in code exist, catalogs agree with English."""

import ast
import re
from pathlib import Path

import pytest
from ag_shared.i18n import CATALOG_DIR, check_against_english, load_catalog
from ag_workstation.auth.messages import MessageKey
from ag_workstation.scan.messages import ScanMessageKey

ROOT = Path(__file__).resolve().parents[2]
ENGLISH = load_catalog(CATALOG_DIR / "en.json")


def _py_files() -> list[Path]:
    return [
        p
        for top in ("shared", "workstation", "server")
        for p in (ROOT / top).rglob("*.py")
        if "i18n" not in p.parts
    ]


def _literal_keys(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        first = node.args[0]
        if name == "t" and isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((first.value, node.lineno))
    return found


def test_every_literal_t_key_is_in_english() -> None:
    missing = []
    for path in _py_files():
        for key, line in _literal_keys(ast.parse(path.read_text(encoding="utf-8"))):
            if key not in ENGLISH:
                missing.append(f"{path.relative_to(ROOT)}:{line} {key}")
    assert not missing


def test_every_template_t_key_is_in_english() -> None:
    pattern = re.compile(r"""\bt\(\s*["']([\w.]+)["']""")
    missing = [
        f"{p.relative_to(ROOT)} {key}"
        for p in (ROOT / "server").rglob("*.html")
        for key in pattern.findall(p.read_text(encoding="utf-8"))
        if key not in ENGLISH
    ]
    assert not missing


def test_every_message_enum_key_is_in_english() -> None:
    for enum in (MessageKey, ScanMessageKey):
        for member in enum:
            assert member.value in ENGLISH


@pytest.mark.parametrize("path", sorted(CATALOG_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_catalog_matches_english_placeholders(path: Path) -> None:
    assert check_against_english(load_catalog(path), ENGLISH) == []


def test_catalogs_ship_as_package_data() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "catalogs/*.json" in text
