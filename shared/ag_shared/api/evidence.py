"""Evidence image upload (`api-contract.md` section 4.4).

`PUT /evidence/{image_id}` with a JPEG body; the metadata travels in
`X-AG-*` headers, modelled by `EvidenceUploadHeaders`.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID

from pydantic import Field

from ag_shared.api.common import ApiModel, CameraSlot, NonNegativeInt, UtcDatetime
from ag_shared.api.errors import ProblemCode

MAX_EVIDENCE_BYTES: Final = 2 * 1024 * 1024
EVIDENCE_CONTENT_TYPE: Final = "image/jpeg"

HEADER_EVENT_ID: Final = "X-AG-Event-Id"
HEADER_CAMERA_SLOT: Final = "X-AG-Camera-Slot"
HEADER_SEQ: Final = "X-AG-Seq"
HEADER_OFFSET_MS: Final = "X-AG-Offset-Ms"
HEADER_CAPTURED_AT: Final = "X-AG-Captured-At"
HEADER_SHA256: Final = "X-AG-SHA256"

_FIELD_HEADERS: Final = {
    "event_id": HEADER_EVENT_ID,
    "camera_slot": HEADER_CAMERA_SLOT,
    "seq": HEADER_SEQ,
    "offset_ms": HEADER_OFFSET_MS,
    "captured_at": HEADER_CAPTURED_AT,
    "sha256": HEADER_SHA256,
}


class EvidenceUploadHeaders(ApiModel):
    """The metadata headers of one evidence upload."""

    event_id: UUID
    camera_slot: CameraSlot
    seq: NonNegativeInt
    offset_ms: int
    """Relative to the event; negative for pre-frames."""
    captured_at: UtcDatetime
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    """Lower-case hex SHA-256 of the JPEG body."""

    def to_headers(self) -> dict[str, str]:
        """HTTP header values for this upload."""
        data = self.model_dump(mode="json")
        return {header: str(data[name]) for name, header in _FIELD_HEADERS.items()}

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "EvidenceUploadHeaders":
        """Parse request headers (names are case-insensitive).

        Raises `pydantic.ValidationError` when a header is missing or invalid.
        """
        lowered = {k.lower(): v for k, v in headers.items()}
        return cls.model_validate(
            {
                name: lowered[header.lower()]
                for name, header in _FIELD_HEADERS.items()
                if header.lower() in lowered
            }
        )


class UploadOutcome(StrEnum):
    """What the client does after an evidence upload response."""

    DONE = "done"
    """201 stored or 200 already stored: delete from the upload queue."""
    RETRY_LATER = "retry_later"
    """404 event_unknown, 429, 5xx, timeout: keep and retry (event first)."""
    DELETE_LOCAL = "delete_local"
    """403 consent_missing: permanent; delete the local image."""
    HASH_MISMATCH = "hash_mismatch"
    """409: the id exists with other bytes; dead-letter and report."""
    DEAD_LETTER = "dead_letter"
    """Other permanent failures (400, 413, 422, other 403)."""
    REFRESH_TOKEN = "refresh_token"  # noqa: S105 - an action label, not a credential
    """401."""
    UPDATE_REQUIRED = "update_required"
    """426."""


def upload_outcome(status: int | None, code: str | None = None) -> UploadOutcome:
    """Map an evidence upload response to the client's reaction (contract 4.4, 7)."""
    if status in (200, 201):
        return UploadOutcome.DONE
    if status == 403:
        if code == ProblemCode.CONSENT_MISSING:
            return UploadOutcome.DELETE_LOCAL
        return UploadOutcome.DEAD_LETTER
    if status == 409:
        return UploadOutcome.HASH_MISMATCH
    if status in (400, 413, 422):
        return UploadOutcome.DEAD_LETTER
    if status == 401:
        return UploadOutcome.REFRESH_TOKEN
    if status == 426:
        return UploadOutcome.UPDATE_REQUIRED
    return UploadOutcome.RETRY_LATER
