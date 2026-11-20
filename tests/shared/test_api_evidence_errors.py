"""Evidence upload headers, upload outcomes, problem bodies and error reactions."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from ag_shared.api import (
    ClientAction,
    EvidenceUploadHeaders,
    Problem,
    ProblemCode,
    UploadOutcome,
    client_action,
    upload_outcome,
)
from pydantic import ValidationError

EVENT = UUID("0193a0b0-0000-7000-8000-0000000000ee")
SHA = hashlib.sha256(b"jpeg").hexdigest()


def _headers() -> EvidenceUploadHeaders:
    return EvidenceUploadHeaders(
        event_id=EVENT,
        camera_slot=1,
        seq=3,
        offset_ms=-1000,
        captured_at=datetime(2026, 11, 20, 12, 0, 0, 250000, tzinfo=UTC),
        sha256=SHA,
    )


def test_headers_round_trip_case_insensitively() -> None:
    sent = _headers().to_headers()
    assert sent == {
        "X-AG-Event-Id": str(EVENT),
        "X-AG-Camera-Slot": "1",
        "X-AG-Seq": "3",
        "X-AG-Offset-Ms": "-1000",
        "X-AG-Captured-At": "2026-11-20T12:00:00.250Z",
        "X-AG-SHA256": SHA,
    }
    received = {k.lower(): v for k, v in sent.items()} | {"content-type": "image/jpeg"}
    assert EvidenceUploadHeaders.from_headers(received) == _headers()


@pytest.mark.parametrize(
    ("header", "value"),
    [("X-AG-SHA256", SHA.upper()), ("X-AG-Camera-Slot", "0"), ("X-AG-Captured-At", "yesterday")],
)
def test_invalid_headers_are_refused(header: str, value: str) -> None:
    sent = _headers().to_headers() | {header: value}
    with pytest.raises(ValidationError):
        EvidenceUploadHeaders.from_headers(sent)


def test_missing_header_is_refused() -> None:
    sent = _headers().to_headers()
    del sent["X-AG-Seq"]
    with pytest.raises(ValidationError):
        EvidenceUploadHeaders.from_headers(sent)


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [
        (201, None, UploadOutcome.DONE),
        (200, None, UploadOutcome.DONE),
        (409, "hash_mismatch", UploadOutcome.HASH_MISMATCH),
        (404, "event_unknown", UploadOutcome.RETRY_LATER),
        (403, "consent_missing", UploadOutcome.DELETE_LOCAL),
        (403, "device_revoked", UploadOutcome.DEAD_LETTER),
        (413, None, UploadOutcome.DEAD_LETTER),
        (401, None, UploadOutcome.REFRESH_TOKEN),
        (426, None, UploadOutcome.UPDATE_REQUIRED),
        (503, None, UploadOutcome.RETRY_LATER),
        (None, None, UploadOutcome.RETRY_LATER),
    ],
)
def test_upload_outcome(status: int | None, code: str | None, outcome: UploadOutcome) -> None:
    assert upload_outcome(status, code) is outcome


@pytest.mark.parametrize(
    ("status", "action"),
    [
        (400, ClientAction.DEAD_LETTER),
        (422, ClientAction.DEAD_LETTER),
        (401, ClientAction.REFRESH_TOKEN),
        (403, ClientAction.REPORT),
        (404, ClientAction.RETRY_LATER),
        (409, ClientAction.ENDPOINT_RULE),
        (413, ClientAction.SPLIT_BATCH),
        (426, ClientAction.UPDATE_REQUIRED),
        (429, ClientAction.BACKOFF),
        (500, ClientAction.BACKOFF),
        (503, ClientAction.BACKOFF),
        (None, ClientAction.BACKOFF),
    ],
)
def test_client_action_follows_section_7(status: int | None, action: ClientAction) -> None:
    assert client_action(status) is action


def test_problem_body_parses_with_unknown_fields_and_codes() -> None:
    problem = Problem.model_validate(
        {
            "type": "https://ag/errors/consent",
            "title": "Consent missing",
            "status": 403,
            "code": "consent_missing",
            "instance": "/evidence/x",
        }
    )
    assert problem.code == ProblemCode.CONSENT_MISSING
    assert Problem.model_validate({"status": 500, "code": "brand_new"}).code == "brand_new"
    with pytest.raises(ValidationError):
        Problem.model_validate({"status": 99})
