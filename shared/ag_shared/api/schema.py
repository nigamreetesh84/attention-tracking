"""The JSON schema of every API payload model, for the contract snapshot test.

`api_json_schema()` is compared with `tests/fixtures/api_schema.json`; any
difference without a `SCHEMA_REVISION` bump fails the test. Regenerate the
snapshot with `AG_UPDATE_SCHEMA_SNAPSHOT=1 pytest tests/shared/test_api_schema_snapshot.py`.
"""

from pydantic import BaseModel, JsonValue

from ag_shared.api.auth import (
    EnrollRequest,
    EnrollResponse,
    RotateSecretRequest,
    RotateSecretResponse,
    TokenRequest,
    TokenResponse,
)
from ag_shared.api.common import SCHEMA_REVISION
from ag_shared.api.config_bundle import ConfigBundle
from ag_shared.api.errors import Problem
from ag_shared.api.events import BatchResult, EventBatch, RawEventBatch
from ag_shared.api.evidence import EvidenceUploadHeaders
from ag_shared.api.heartbeat import HeartbeatRequest, HeartbeatResponse

API_MODELS: tuple[type[BaseModel], ...] = (
    EnrollRequest,
    EnrollResponse,
    TokenRequest,
    TokenResponse,
    RotateSecretRequest,
    RotateSecretResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    ConfigBundle,
    EventBatch,
    RawEventBatch,
    BatchResult,
    EvidenceUploadHeaders,
    Problem,
)
"""Top-level payloads; nested models are covered through them."""


def api_json_schema() -> dict[str, JsonValue]:
    """`{"schema_revision": N, "schemas": {model name: JSON schema}}`."""
    schemas: dict[str, JsonValue] = {m.__name__: m.model_json_schema() for m in API_MODELS}
    return {"schema_revision": SCHEMA_REVISION, "schemas": schemas}
