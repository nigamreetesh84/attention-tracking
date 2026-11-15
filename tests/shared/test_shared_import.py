"""Import smoke test for ag_shared."""

import importlib


def test_import() -> None:
    assert importlib.import_module("ag_shared").__name__ == "ag_shared"
