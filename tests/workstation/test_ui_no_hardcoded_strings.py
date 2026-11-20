"""UI modules must not pass literal user-facing text to Qt (CLAUDE.md rule 13).

Text comes from the translator. Literals with no letters (empty, "", "0", "/")
are allowed.
"""

import ast
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parents[2] / "workstation" / "ag_workstation" / "ui"

SETTERS = {
    "setText", "setWindowTitle", "setPlaceholderText", "setToolTip", "setStatusTip",
    "setWhatsThis", "setTitle", "setLabelText", "setInformativeText", "setDetailedText",
    "setHtml", "setPlainText", "setTabText", "setItemText", "setAccessibleName",
    "setAccessibleDescription", "setHeaderLabels", "setHorizontalHeaderLabels",
    "setVerticalHeaderLabels", "addItem", "addItems", "insertItem", "showMessage",
    "setSuffix", "setPrefix", "setButtonText", "addAction", "addMenu", "addTab",
    "setSpecialValueText", "setPlaceholder", "addRow",
}  # fmt: skip
CONSTRUCTORS = {
    "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox", "QAction",
    "QToolButton", "QLineEdit", "QMenu", "QTextEdit", "QPlainTextEdit", "QCommandLinkButton",
}  # fmt: skip
STATIC_DIALOGS = {"information", "warning", "critical", "question", "about", "getText"}
TEXT_KEYWORDS = {"text", "title", "label", "placeholderText", "toolTip", "windowTitle"}


def _has_literal_text(node: ast.AST) -> bool:
    """True if `node` holds a string literal with a letter in it.

    Nested calls are not entered: `t("scan.ok")` is a key, and a call that
    is itself a Qt text call is checked on its own.
    """
    if isinstance(node, ast.Call):
        return False
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and any(ch.isalpha() for ch in node.value)
    return any(_has_literal_text(child) for child in ast.iter_child_nodes(node))


def _call_name(call: ast.Call) -> tuple[str, str | None]:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return func.attr, owner
    return "", None


def find_hardcoded(source: str) -> list[int]:
    """Line numbers of Qt text calls given literal text instead of a translator result."""
    bad: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name, owner = _call_name(node)
        is_text_call = (
            name in SETTERS
            or name in CONSTRUCTORS
            or (name in STATIC_DIALOGS and owner is not None and owner.startswith("Q"))
        )
        if not is_text_call:
            continue
        args: list[ast.AST] = list(node.args)
        args += [kw.value for kw in node.keywords if kw.arg in TEXT_KEYWORDS or name in SETTERS]
        if any(_has_literal_text(a) for a in args):
            bad.append(node.lineno)
    return sorted(bad)


def test_ui_modules_have_no_hardcoded_text() -> None:
    offenders = []
    for path in sorted(UI_DIR.rglob("*.py")) if UI_DIR.is_dir() else []:
        offenders += [f"{path.name}:{line}" for line in find_hardcoded(path.read_text("utf-8"))]
    assert not offenders, f"hard-coded user-facing strings: {offenders}"


@pytest.mark.parametrize(
    "snippet",
    [
        'label.setText("Scan recorded.")',
        "self.setWindowTitle('AttentionGuard')",
        'edit.setPlaceholderText("Employee ID")',
        'QLabel("Hello")',
        'QPushButton(text="OK")',
        'btn.setToolTip(f"Wait {n}")',
        'box.addItem("Line 1")',
        'QMessageBox.warning(self, "Oops", "No")',
        'label.setText("Hi " + name)',
    ],
)
def test_checker_flags_literals(snippet: str) -> None:
    assert find_hardcoded(snippet) == [1]


@pytest.mark.parametrize(
    "snippet",
    [
        'label.setText(t("scan.ok"))',
        "label.setText(text(result.message_key))",
        'label.setText("")',
        'btn = QPushButton("7")',
        'QLabel(t("a.b"), parent)',
        'label.setObjectName("statusLabel")',
        'label.setStyleSheet("color: red")',
    ],
)
def test_checker_allows_translated_or_non_text(snippet: str) -> None:
    assert find_hardcoded(snippet) == []
