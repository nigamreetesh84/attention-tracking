"""Problem details and the client's reaction per status (`api-contract.md` section 7)."""

from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field

from ag_shared.api.common import ApiModel

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"


class ProblemCode(StrEnum):
    """Machine-readable `code` values named in the contract.

    `Problem.code` stays a plain string so a newer server's codes still parse;
    compare against these members.
    """

    DEVICE_REVOKED = "device_revoked"
    HASH_MISMATCH = "hash_mismatch"
    EVENT_UNKNOWN = "event_unknown"
    CONSENT_MISSING = "consent_missing"
    UPDATE_REQUIRED = "update_required"


class Problem(ApiModel):
    """An `application/problem+json` body."""

    type: str = "about:blank"
    title: Annotated[str, Field(max_length=200)] = ""
    status: Annotated[int, Field(ge=100, le=599)]
    detail: Annotated[str, Field(max_length=2000)] | None = None
    code: Annotated[str, Field(max_length=100)] | None = None


class ClientAction(StrEnum):
    """What the client does with a failed request."""

    DEAD_LETTER = "dead_letter"
    """Permanent for that item (400, 422)."""
    REFRESH_TOKEN = "refresh_token"  # noqa: S105 - an action label, not a credential
    """Refresh the token once, then back off (401)."""
    REPORT = "report"
    """Permanent; report in health (403)."""
    RETRY_LATER = "retry_later"
    """A parent is missing (404 on a parent)."""
    ENDPOINT_RULE = "endpoint_rule"
    """See the endpoint's own rules (409)."""
    SPLIT_BATCH = "split_batch"
    """Payload too large (413)."""
    UPDATE_REQUIRED = "update_required"
    """Keep buffering and raise the health flag (426)."""
    BACKOFF = "backoff"
    """429, 5xx, timeout and anything not listed."""


_ACTIONS: dict[int, ClientAction] = {
    400: ClientAction.DEAD_LETTER,
    422: ClientAction.DEAD_LETTER,
    401: ClientAction.REFRESH_TOKEN,
    403: ClientAction.REPORT,
    404: ClientAction.RETRY_LATER,
    409: ClientAction.ENDPOINT_RULE,
    413: ClientAction.SPLIT_BATCH,
    426: ClientAction.UPDATE_REQUIRED,
}


def client_action(status: int | None) -> ClientAction:
    """The section 7 reaction to an HTTP status; None means a timeout or no response."""
    if status is None:
        return ClientAction.BACKOFF
    return _ACTIONS.get(status, ClientAction.BACKOFF)
