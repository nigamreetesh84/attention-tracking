"""Low-level device API calls: headers, access tokens, enrollment, secret rotation.

Every request carries `X-AG-Schema` and `X-AG-App-Version` (contract 2) and,
except enrollment and `/token`, a bearer token (contract 3). Tokens are
cached until shortly before they expire. A `401` is answered by fetching a
new token once and resending; a second `401` raises `ApiError` and the
caller backs off (contract 7). Other statuses are returned to the caller,
which applies the endpoint's own rules.

Tokens and secrets are never logged.
"""

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from ag_shared.api import (
    API_PREFIX,
    HEADER_APP_VERSION,
    HEADER_AUTHORIZATION,
    HEADER_SCHEMA,
    PROBLEM_CONTENT_TYPE,
    SCHEMA_REVISION,
    EnrollRequest,
    EnrollResponse,
    Problem,
    RotateSecretResponse,
    TokenRequest,
    TokenResponse,
)
from pydantic import SecretStr, ValidationError

from ag_workstation.clock import Clock
from ag_workstation.sync.clock_offset import ClockOffsetEstimator
from ag_workstation.sync.credentials import CredentialStore, DeviceCredentials
from ag_workstation.sync.transport import (
    DEFAULT_TIMEOUT_S,
    HttpResponse,
    SyncTransport,
    TransportError,
)

logger = logging.getLogger(__name__)

TOKEN_MARGIN_S: Final = 60.0
"""A token is renewed this long before it expires."""
JSON_TYPE: Final = "application/json"


class NotEnrolledError(Exception):
    """No device credentials: the workstation has not been enrolled."""


class ApiError(Exception):
    """A request the caller must treat as failed with `status` (and problem `code`)."""

    def __init__(self, status: int, code: str | None, endpoint: str) -> None:
        super().__init__(f"{endpoint}: HTTP {status}" + (f" ({code})" if code else ""))
        self.status = status
        self.code = code
        self.endpoint = endpoint


class CredentialSaveError(Exception):
    """A new device secret could not be stored; it is kept in memory and saved later."""


@dataclass(frozen=True, slots=True)
class Exchange:
    """One response with the device wall times around it (for clock offset)."""

    response: HttpResponse
    sent_at: datetime
    received_at: datetime
    rtt_s: float = field(default=0.0)

    @property
    def status(self) -> int:
        """HTTP status."""
        return self.response.status

    @property
    def problem_code(self) -> str | None:
        """The problem `code`, if the body is a problem document."""
        return problem_code(self.response)


def problem_code(response: HttpResponse) -> str | None:
    """`code` of an `application/problem+json` (or JSON) error body; None otherwise."""
    if response.ok or not response.content:
        return None
    ctype = response.headers.get("content-type", "")
    if PROBLEM_CONTENT_TYPE not in ctype and JSON_TYPE not in ctype:
        return None
    try:
        return Problem.model_validate_json(response.content).code
    except ValidationError:
        return None


class DeviceApi:
    """Authenticated requests to `/api/v1/device`. Thread-safe."""

    def __init__(
        self,
        transport: SyncTransport,
        credentials: CredentialStore,
        clock: Clock,
        offset: ClockOffsetEstimator,
        *,
        app_version: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        token_margin_s: float = TOKEN_MARGIN_S,
    ) -> None:
        self._transport = transport
        self._store = credentials
        self._clock = clock
        self._offset = offset
        self._app_version = app_version
        self._timeout_s = timeout_s
        self._margin_s = token_margin_s
        self._lock = threading.RLock()
        self._token: SecretStr | None = None
        self._token_expires = 0.0
        self._unsaved: DeviceCredentials | None = None
        """A rotated secret the store refused; it wins over the stored one."""

    # -- credentials ---------------------------------------------------------

    def credentials(self) -> DeviceCredentials | None:
        """The credentials in force (an unsaved rotated secret first)."""
        with self._lock:
            if self._unsaved is not None:
                return self._unsaved
        return self._store.load()

    @property
    def has_unsaved_credentials(self) -> bool:
        """A rotated secret waits to be stored."""
        with self._lock:
            return self._unsaved is not None

    def retry_credential_save(self) -> bool:
        """Try again to store a rotated secret; True when nothing is left unsaved."""
        with self._lock:
            pending = self._unsaved
            if pending is None:
                return True
            try:
                self._store.save(pending)
            except Exception as exc:
                logger.warning("device credentials still not stored (%s)", type(exc).__name__)
                return False
            self._unsaved = None
            return True

    def invalidate_token(self) -> None:
        """Forget the cached token."""
        with self._lock:
            self._token = None

    # -- requests ------------------------------------------------------------

    def _headers(self, content_type: str | None) -> dict[str, str]:
        headers = {
            HEADER_SCHEMA: str(SCHEMA_REVISION),
            HEADER_APP_VERSION: self._app_version,
            "Accept": f"{JSON_TYPE}, {PROBLEM_CONTENT_TYPE}",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    def _exchange(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None
    ) -> Exchange:
        sent_at, started = self._clock.now(), self._clock.monotonic()
        response = self._transport.request(
            method, API_PREFIX + path, headers=headers, content=body, timeout_s=self._timeout_s
        )
        return Exchange(response, sent_at, self._clock.now(), self._clock.monotonic() - started)

    def sample_clock(self, server_time: datetime | None, exchange: Exchange) -> None:
        """Feed a response's `server_time` to the offset estimator."""
        if server_time is not None:
            self._offset.add_sample(server_time, exchange.sent_at, exchange.received_at)

    def enroll(self, request: EnrollRequest) -> EnrollResponse:
        """`POST /enroll`, then store the credentials.

        Raises `ApiError`, `TransportError`, or the store's error.
        """
        ex = self._exchange(
            "POST", "/enroll", self._headers(JSON_TYPE), request.model_dump_json().encode()
        )
        if ex.status not in (200, 201):
            raise ApiError(ex.status, ex.problem_code, "enroll")
        reply = EnrollResponse.model_validate_json(ex.response.content)
        self.sample_clock(reply.server_time, ex)
        self._store.save(DeviceCredentials(reply.workstation_id, reply.device_secret))
        with self._lock:
            self._unsaved = None
            self._token = None
        logger.info("workstation enrolled as %s", reply.workstation_id)
        return reply

    def token(self) -> SecretStr:
        """A valid access token, fetched with `POST /token` when needed.

        Raises `NotEnrolledError`, `ApiError` (non-200 from `/token`) or
        `TransportError` (also for an unreadable `/token` reply).
        """
        with self._lock:
            if self._token is not None and self._clock.monotonic() < self._token_expires:
                return self._token
            creds = self.credentials()
            if creds is None:
                raise NotEnrolledError("no device credentials")
            body = TokenRequest(
                workstation_id=creds.workstation_id, device_secret=creds.device_secret
            )
            ex = self._exchange(
                "POST", "/token", self._headers(JSON_TYPE), body.model_dump_json().encode()
            )
            if ex.status != 200:
                raise ApiError(ex.status, ex.problem_code, "token")
            try:
                reply = TokenResponse.model_validate_json(ex.response.content)
            except ValidationError as exc:
                raise TransportError("unreadable token response") from exc
            self.sample_clock(reply.server_time, ex)
            lifetime = max(reply.expires_in - self._margin_s, reply.expires_in / 2)
            self._token = reply.access_token
            self._token_expires = self._clock.monotonic() + lifetime
            return self._token

    def send(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = JSON_TYPE,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Exchange:
        """An authenticated request. On `401`: one new token and one resend.

        Returns the exchange for any other status. Raises `ApiError` for a
        second `401` or a failed token fetch, `NotEnrolledError`, `TransportError`.
        """
        for attempt in (1, 2):
            headers = self._headers(content_type if body is not None else None)
            if extra_headers:
                headers.update(extra_headers)
            headers[HEADER_AUTHORIZATION] = f"Bearer {self.token().get_secret_value()}"
            ex = self._exchange(method, path, headers, body)
            if ex.status != 401:
                return ex
            self.invalidate_token()
            if attempt == 2:
                raise ApiError(401, ex.problem_code, path)
            logger.info("%s answered 401; fetching a new token once", path)
        raise AssertionError("unreachable")  # pragma: no cover

    def rotate_secret(self) -> None:
        """`POST /secret/rotate` and store the new secret.

        Raises `ApiError`/`TransportError` (nothing changed) or
        `CredentialSaveError` (rotated; the secret is kept in memory).
        """
        ex = self.send("POST", "/secret/rotate", body=b"{}")
        if ex.status != 200:
            raise ApiError(ex.status, ex.problem_code, "/secret/rotate")
        reply = RotateSecretResponse.model_validate_json(ex.response.content)
        self.sample_clock(reply.server_time, ex)
        creds = self.credentials()
        if creds is None:  # pragma: no cover - send() needed credentials
            raise NotEnrolledError("no device credentials")
        rotated = DeviceCredentials(creds.workstation_id, reply.device_secret)
        with self._lock:
            self._unsaved = rotated
            self._token = None
        if not self.retry_credential_save():
            raise CredentialSaveError("rotated secret not stored")
        logger.info("device secret rotated")
