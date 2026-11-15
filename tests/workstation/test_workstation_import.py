"""Import smoke test for ag_workstation."""

import importlib


def test_import() -> None:
    assert importlib.import_module("ag_workstation").__name__ == "ag_workstation"
