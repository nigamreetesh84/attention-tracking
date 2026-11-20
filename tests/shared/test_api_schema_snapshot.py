"""Contract snapshot (`api-contract.md` section 8).

Fails when an API model's JSON schema changes without a `SCHEMA_REVISION`
bump. After an approved change: bump `SCHEMA_REVISION` in
`ag_shared/api/common.py`, then regenerate with
`AG_UPDATE_SCHEMA_SNAPSHOT=1 pytest tests/shared/test_api_schema_snapshot.py`.
"""

import json
import os
from pathlib import Path

import pytest
from ag_shared.api import SCHEMA_REVISION
from ag_shared.api.schema import API_MODELS, api_json_schema

SNAPSHOT = Path(__file__).parent.parent / "fixtures" / "api_schema.json"


def _render(data: object) -> str:
    return json.dumps(data, indent=1, sort_keys=True) + "\n"


def test_schema_matches_snapshot_or_revision_was_bumped() -> None:
    current = api_json_schema()
    if os.environ.get("AG_UPDATE_SCHEMA_SNAPSHOT") == "1":
        SNAPSHOT.write_text(_render(current), encoding="utf-8")
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if stored["schema_revision"] == SCHEMA_REVISION:
        assert stored["schemas"] == current["schemas"], (
            "API JSON schema changed without bumping SCHEMA_REVISION "
            "(ag_shared/api/common.py); bump it, then regenerate the snapshot"
        )
    else:
        pytest.fail(
            f"SCHEMA_REVISION is {SCHEMA_REVISION} but the snapshot is revision "
            f"{stored['schema_revision']}: regenerate the snapshot (see module docstring)"
        )


def test_snapshot_file_is_canonical() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    assert text == _render(json.loads(text))


def test_every_top_level_model_is_in_the_snapshot() -> None:
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(stored["schemas"]) == {m.__name__ for m in API_MODELS}
