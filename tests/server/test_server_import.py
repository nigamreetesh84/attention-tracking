"""Import smoke test for ag_server."""

import importlib


def test_import() -> None:
    assert importlib.import_module("ag_server").__name__ == "ag_server"
