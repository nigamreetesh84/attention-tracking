"""A fault-injecting fake of the device API (`api-contract.md` section 8).

An in-process stand-in for the server, used as an `httpx.MockTransport`
handler so the sync client's real httpx path is exercised. It implements the
contract's server-side rules that the client depends on (idempotent ingest
by item id and `batch_id`, dependency order and `retry_later`, token expiry
and revocation, schema `426`, evidence hash and consent checks, `If-None-Match`
on config, commands repeated until acknowledged) and injects faults:

- `DROP_REQUEST`: the request never arrives (connect error);
- `DROP_RESPONSE`: processed, but the response is lost (read timeout);
- `DUPLICATE`: the request is processed twice (a proxy retry);
- `DEFER`: the request is held and processed later (`release_deferred`),
  after newer requests: reordering, plus a lost response;
- `STATUS`: answer with a given status and problem code without processing;
- `HANG`: block until `release_hang()` (a dead server holding the socket).

For tests only; production code never imports it.
"""

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

import httpx
from ag_shared.api import (
    API_PREFIX,
    HEADER_SCHEMA,
    INGEST_ORDER,
    PROBLEM_CONTENT_TYPE,
    AttentionEventItem,
    EnrollRequest,
    EvidenceUploadHeaders,
    HeartbeatRequest,
    RawEventBatch,
    TokenRequest,
    parse_event_item,
)
from ag_shared.ids import new_uuid7
from pydantic import JsonValue, ValidationError

from ag_workstation.clock import Clock

TOKEN_TTL_S: Final = 900
SECRET_GRACE: Final = timedelta(minutes=10)


class Fault(StrEnum):
    """What to do to a matching request."""

    DROP_REQUEST = "drop_request"
    DROP_RESPONSE = "drop_response"
    DUPLICATE = "duplicate"
    DEFER = "defer"
    STATUS = "status"
    HANG = "hang"


@dataclass(slots=True)
class FaultRule:
    """Apply `fault` to the next `times` requests whose path starts with `path`."""

    path: str
    fault: Fault
    times: int = 1
    status: int = 503
    code: str | None = None


@dataclass(slots=True)
class _Token:
    expires: float
    """Server monotonic seconds."""


@dataclass(slots=True)
class FakeDeviceServer:
    """The fake server's state. Thread-safe; call it as an httpx handler."""

    clock: Clock
    workstation_id: UUID
    secret: str
    skew: timedelta = timedelta(0)
    """Added to the server's `server_time` (a server whose clock differs)."""
    min_schema: int = 1
    token_ttl_s: int = TOKEN_TTL_S
    revoked: bool = False
    enrollment_token: str | None = None
    """One-time token `/enroll` accepts."""
    config_body: bytes | None = None
    config_version: int = 0
    rules: list[FaultRule] = field(default_factory=list)
    items: dict[UUID, dict[str, JsonValue]] = field(default_factory=dict)
    item_arrivals: dict[UUID, int] = field(default_factory=dict)
    """How many times each item id was offered (duplicates included)."""
    batches: dict[UUID, bytes] = field(default_factory=dict)
    reject_items: set[UUID] = field(default_factory=set)
    evidence: dict[UUID, tuple[str, bytes]] = field(default_factory=dict)
    consent_missing_events: set[UUID] = field(default_factory=set)
    heartbeats: list[HeartbeatRequest] = field(default_factory=list)
    commands: list[dict[str, JsonValue]] = field(default_factory=list)
    acked: list[UUID] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    """`METHOD path` of every request that arrived (dropped ones excluded)."""
    rotations: int = 0
    _previous_secret: tuple[str, float] | None = None
    _tokens: dict[str, _Token] = field(default_factory=dict)
    _deferred: list[httpx.Request] = field(default_factory=list)
    _hang: threading.Event = field(default_factory=threading.Event)
    _hanging: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # -- control -------------------------------------------------------------

    def inject(
        self, path: str, fault: Fault, *, times: int = 1, status: int = 503, code: str | None = None
    ) -> None:
        """Add a fault rule (`path` relative to the API prefix, e.g. `/events:batch`)."""
        with self._lock:
            self.rules.append(FaultRule(path, fault, times, status, code))

    def release_deferred(self) -> int:
        """Process held requests now (their responses are lost); returns how many."""
        with self._lock:
            held, self._deferred = self._deferred, []
        for request in held:
            self._process(request)
        return len(held)

    def release_hang(self) -> None:
        """Let hung requests continue."""
        self._hang.set()

    def wait_hanging(self, timeout: float = 2.0) -> bool:
        """Wait until a request is hung."""
        return self._hanging.wait(timeout)

    def expire_tokens(self) -> None:
        """Invalidate every issued token (as if they all expired)."""
        with self._lock:
            self._tokens.clear()

    def set_config(self, body: bytes, version: int) -> None:
        """Serve `body` as the bundle with `version`."""
        with self._lock:
            self.config_body = body
            self.config_version = version

    def add_command(self, command: dict[str, JsonValue]) -> None:
        """Queue a command; it is repeated until acknowledged."""
        with self._lock:
            self.commands.append(command)

    def unique_items(self, item_type: str | None = None) -> list[UUID]:
        """Stored item ids, optionally of one type."""
        with self._lock:
            return [i for i, d in self.items.items() if item_type is None or d["type"] == item_type]

    # -- handler -------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """The `httpx.MockTransport` handler."""
        rule = self._match(request.url.path.removeprefix(API_PREFIX))
        if rule is not None and rule.fault is Fault.DROP_REQUEST:
            raise httpx.ConnectError("fault: request dropped", request=request)
        with self._lock:
            self.requests.append(f"{request.method} {request.url.path.removeprefix(API_PREFIX)}")
        if rule is None:
            return self._process(request)
        if rule.fault is Fault.STATUS:
            return self._problem(rule.status, rule.code)
        if rule.fault is Fault.HANG:
            self._hanging.set()
            self._hang.wait()
            raise httpx.ReadTimeout("fault: hung", request=request)
        if rule.fault is Fault.DEFER:
            with self._lock:
                self._deferred.append(request)
            raise httpx.ReadTimeout("fault: deferred", request=request)
        response = self._process(request)
        if rule.fault is Fault.DUPLICATE:
            return self._process(request)
        if rule.fault is Fault.DROP_RESPONSE:
            raise httpx.ReadTimeout("fault: response dropped", request=request)
        return response

    def _match(self, path: str) -> FaultRule | None:
        with self._lock:
            for rule in self.rules:
                if rule.times > 0 and path.startswith(rule.path):
                    rule.times -= 1
                    return rule
        return None

    def _process(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(API_PREFIX)
        with self._lock:
            if int(request.headers.get(HEADER_SCHEMA, "0")) < self.min_schema:
                return self._problem(426, "update_required")
            if path == "/token":
                return self._token(request)
            if path == "/enroll":
                return self._enroll(request)
            if request.headers.get("authorization") is None or not self._authorized(request):
                return self._problem(401, "device_revoked" if self.revoked else "token_invalid")
            if path == "/heartbeat":
                return self._heartbeat(request)
            if path == "/config":
                return self._config(request)
            if path == "/events:batch":
                return self._batch(request)
            if path == "/secret/rotate":
                return self._rotate()
            if path.startswith("/evidence/"):
                return self._evidence(path.removeprefix("/evidence/"), request)
        return self._problem(404, None)

    # -- helpers -------------------------------------------------------------

    def _now_iso(self) -> str:
        return (self.clock.now() + self.skew).isoformat(timespec="milliseconds")

    def _json(self, status: int, body: dict[str, JsonValue]) -> httpx.Response:
        return httpx.Response(status, json=body)

    def _problem(self, status: int, code: str | None) -> httpx.Response:
        body: dict[str, JsonValue] = {"type": "about:blank", "title": "error", "status": status}
        if code is not None:
            body["code"] = code
        return httpx.Response(
            status,
            content=json.dumps(body).encode(),
            headers={"content-type": PROBLEM_CONTENT_TYPE},
        )

    def _authorized(self, request: httpx.Request) -> bool:
        if self.revoked:
            return False
        token = request.headers["authorization"].removeprefix("Bearer ")
        entry = self._tokens.get(token)
        return entry is not None and self.clock.monotonic() < entry.expires

    # -- endpoints -----------------------------------------------------------

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.revoked:
            return self._problem(401, "device_revoked")
        try:
            body = TokenRequest.model_validate_json(request.content)
        except ValidationError:
            return self._problem(422, None)
        secret = body.device_secret.get_secret_value()
        previous_ok = (
            self._previous_secret is not None
            and secret == self._previous_secret[0]
            and self.clock.monotonic() < self._previous_secret[1]
        )
        if body.workstation_id != self.workstation_id or (
            secret != self.secret and not previous_ok
        ):
            return self._problem(401, "bad_credentials")
        token = new_uuid7().hex
        self._tokens[token] = _Token(self.clock.monotonic() + self.token_ttl_s)
        return self._json(
            200,
            {"access_token": token, "expires_in": self.token_ttl_s, "server_time": self._now_iso()},
        )

    def _enroll(self, request: httpx.Request) -> httpx.Response:
        try:
            body = EnrollRequest.model_validate_json(request.content)
        except ValidationError:
            return self._problem(422, None)
        if body.enrollment_token.get_secret_value() != self.enrollment_token:
            return self._problem(401, "enrollment_invalid")
        self.enrollment_token = None
        return self._json(
            201,
            {
                "workstation_id": str(self.workstation_id),
                "device_secret": self.secret,
                "server_time": self._now_iso(),
            },
        )

    def _rotate(self) -> httpx.Response:
        self._previous_secret = (self.secret, self.clock.monotonic() + SECRET_GRACE.total_seconds())
        self.secret = new_uuid7().hex
        self.rotations += 1
        return self._json(200, {"device_secret": self.secret, "server_time": self._now_iso()})

    def _heartbeat(self, request: httpx.Request) -> httpx.Response:
        try:
            body = HeartbeatRequest.model_validate_json(request.content)
        except ValidationError:
            return self._problem(422, None)
        self.heartbeats.append(body)
        acked = set(body.acked_commands)
        self.acked.extend(a for a in body.acked_commands if a not in self.acked)
        self.commands = [c for c in self.commands if UUID(str(c["id"])) not in acked]
        return self._json(
            200,
            {
                "server_time": self._now_iso(),
                "config_version": self.config_version,
                "commands": list(self.commands),
            },
        )

    def _config(self, request: httpx.Request) -> httpx.Response:
        if self.config_body is None:
            return self._problem(404, None)
        if request.headers.get("if-none-match") == f'"{self.config_version}"':
            return httpx.Response(304)
        return httpx.Response(
            200, content=self.config_body, headers={"content-type": "application/json"}
        )

    def _batch(self, request: httpx.Request) -> httpx.Response:
        try:
            batch = RawEventBatch.model_validate_json(request.content)
        except ValidationError:
            return self._problem(422, None)
        stored = self.batches.get(batch.batch_id)
        if stored is not None:
            return httpx.Response(200, content=stored, headers={"content-type": "application/json"})
        order = {t.value: n for n, t in enumerate(INGEST_ORDER)}
        indexed = sorted(
            enumerate(batch.items),
            key=lambda pair: (order.get(str(pair[1].get("type")), len(order)), pair[0]),
        )
        results: list[JsonValue] = []
        for _, raw in indexed:
            results.append(self._ingest(raw))
        body = json.dumps(
            {"batch_id": str(batch.batch_id), "server_time": self._now_iso(), "results": results}
        ).encode()
        self.batches[batch.batch_id] = body
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    def _ingest(self, raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
        rid = str(raw.get("id"))
        try:
            item = parse_event_item(raw)
        except ValidationError:
            return {"id": rid, "status": "rejected", "reason": "invalid"}
        self.item_arrivals[item.id] = self.item_arrivals.get(item.id, 0) + 1
        if item.id in self.reject_items:
            return {"id": rid, "status": "rejected", "reason": "test rejection"}
        if item.id in self.items:
            return {"id": rid, "status": "duplicate", "reason": None}
        if isinstance(item, AttentionEventItem):
            scan_id = item.payload.scan_id
            if scan_id is not None and scan_id not in self.items:
                return {"id": rid, "status": "retry_later", "reason": "scan not yet received"}
        self.items[item.id] = raw
        return {"id": rid, "status": "accepted", "reason": None}

    def _evidence(self, raw_id: str, request: httpx.Request) -> httpx.Response:
        try:
            image_id = UUID(raw_id)
            headers = EvidenceUploadHeaders.from_headers(dict(request.headers))
        except (ValueError, ValidationError):
            return self._problem(400, None)
        body = request.content
        if hashlib.sha256(body).hexdigest() != headers.sha256:
            return self._problem(400, "hash_invalid")
        if headers.event_id not in self.items:
            return self._problem(404, "event_unknown")
        if headers.event_id in self.consent_missing_events:
            return self._problem(403, "consent_missing")
        existing = self.evidence.get(image_id)
        if existing is not None:
            if existing[0] == headers.sha256:
                return httpx.Response(200)
            return self._problem(409, "hash_mismatch")
        self.evidence[image_id] = (headers.sha256, body)
        return httpx.Response(201)
