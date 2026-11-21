"""Sync client (PLAN 2.9) against the fault-injecting fake server.

Covers the result-status table (4.3), the evidence rules (4.4), the error
table (7), commands and acks (4.1), config apply (4.2), clock offset (6),
and the faults named in the plan: loss, duplication, reordering,
`retry_later`, `426`, `403 consent_missing`, token expiry, invalid config.
"""

import itertools
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from ag_shared.api import (
    API_PREFIX,
    HEADER_APP_VERSION,
    HEADER_SCHEMA,
    SCHEMA_REVISION,
    AttentionEventKind,
    AttentionEventPayload,
    EnrollRequest,
    EventType,
    ItemResult,
    LocalAlertKind,
    ScanPayload,
    SessionMethod,
)
from ag_shared.ids import new_uuid7
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.auth import CurrentSession
from ag_workstation.clock import FakeClock
from ag_workstation.consent import CaptureGate, CapturePermit
from ag_workstation.engine_api import BurstKind, EvidenceBurst, EvidenceFrame, FrameRole
from ag_workstation.evidence import EvidenceStore, RemovalReason
from ag_workstation.fakes import fake_jpeg
from ag_workstation.fakes.device_server import FakeDeviceServer, Fault
from ag_workstation.queue import ApplySummary, ClaimedRecord, EventQueue, InMemoryKeyProvider
from ag_workstation.queue.store import DeadLetterReason, QueueStats
from ag_workstation.sync import (
    ApiError,
    ClockOffsetEstimator,
    CommandInbox,
    ConfigStore,
    DeviceApi,
    DeviceCredentials,
    HttpResponse,
    HttpxTransport,
    InMemoryCredentialStore,
    NotEnrolledError,
    StationStatus,
    SyncClient,
    SyncFlag,
    TransportError,
    bundle_path,
    load_bundle,
)
from pydantic import BaseModel, SecretStr

Json = dict[str, Any]  # Any: free-form JSON fixtures

KEY = bytes(range(32))
T0 = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
STATION = UUID("0193a0b0-0000-7000-8000-00000000d001")  # the bundle fixture's workstation
SECRET = "s3cret-device"  # noqa: S105 - test fixture, not a credential
URL = "https://ag.test"


def bundle_json(version: int = 7) -> Json:
    """A valid one-camera bundle for `STATION`."""
    return {
        "version": version,
        "generated_at": "2026-11-21T07:00:00Z",
        "site": {"timezone": "Asia/Kolkata", "settings": {}},
        "workstation": {
            "id": str(STATION),
            "name": "Line 1 / WS 3",
            "line_id": str(STATION),
            "camera_count": 1,
        },
        "cameras": [{"slot": 1, "role": "primary", "kind": "usb", "uri": "0"}],
        "ppe_profile": builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json"),
        "thresholds": {"absent_seconds": 10.0},
        "roster": [
            {
                "user_id": str(new_uuid7()),
                "employee_id": "EMP001",
                "display_name": "Worker",
                "has_consent": True,
            }
        ],
    }


class _OpenSession:
    def current(self) -> CurrentSession:
        return CurrentSession(
            session_id=new_uuid7(),
            user_id=new_uuid7(),
            employee_id="EMP001",
            display_name="Worker",
            has_consent=True,
            preferred_language=None,
            ppe_profile_id=None,
            method=SessionMethod.EMPLOYEE_ID_PIN,
            station_assignment_id=None,
            started_at=T0 - timedelta(days=1),
            valid_until=None,
        )


class _NoBundle:
    def current_bundle(self) -> None:
        return None


_GATE = CaptureGate(sessions=_OpenSession(), bundles=_NoBundle(), clock=FakeClock(start_utc=T0))


def _jpeg(size: int, tag: str) -> bytes:
    head = fake_jpeg(tag)
    return head + b"\x5a" * max(0, size - len(head))


def burst(event_id: UUID, sizes: tuple[int, ...], at: datetime) -> EvidenceBurst:
    """A key frame then post frames from slot 1."""
    frames = tuple(
        EvidenceFrame(
            camera_slot=1,
            captured_at=at + timedelta(milliseconds=500 * k),
            offset_ms=500 * k,
            seq=100 + k,
            role=FrameRole.KEY if k == 0 else FrameRole.POST,
            jpeg=_jpeg(size, f"{event_id}/{k}"),
        )
        for k, size in enumerate(sizes)
    )
    return EvidenceBurst(
        event_id=event_id,
        kind=BurstKind.SCAN_WARNING,
        triggered_at=at,
        trigger_ts=0.0,
        frames=frames,
        expected_frames=len(frames),
    )


def save(store: EvidenceStore, b: EvidenceBurst) -> Any:  # Any: the store's SaveResult
    permit = _GATE.permit(b)
    assert isinstance(permit, CapturePermit)
    return store.save_burst(b, permit=permit)


# -- rig ----------------------------------------------------------------------


@dataclass
class Rig:
    clock: FakeClock
    server: FakeDeviceServer
    queue: EventQueue
    evidence: EvidenceStore
    config: ConfigStore
    inbox: CommandInbox
    creds: InMemoryCredentialStore
    offset: ClockOffsetEstimator
    api: DeviceApi
    client: SyncClient
    data_dir: Path
    seen: list[httpx.Request] = field(default_factory=list)

    def run(self, seconds: float = 10.0, step: float = 0.5) -> None:
        """Tick the client while `seconds` of fake time pass."""
        for _ in range(int(seconds / step)):
            self.client.tick()
            self.clock.advance(step)

    def until(self, done: Callable[[], bool], seconds: float = 900.0, step: float = 0.5) -> None:
        for _ in range(int(seconds / step)):
            if done():
                return
            self.client.tick()
            self.clock.advance(step)
        assert done(), "condition not reached"

    def scan(self, barcode: str = "B1", scan_id: UUID | None = None) -> UUID:
        payload = ScanPayload(
            barcode=barcode, state_at_scan=AttentionState.ATTENTIVE, outcome=ScanOutcome.ATTENTIVE
        )
        return self.queue.enqueue(EventType.SCAN, payload, self.clock.now(), record_id=scan_id).id

    def event(self, scan_id: UUID, event_id: UUID | None = None) -> UUID:
        payload = AttentionEventPayload(
            kind=AttentionEventKind.SCAN_WARNING,
            state=AttentionState.DISTRACTED,
            started_at=self.clock.now(),
            scan_id=scan_id,
        )
        return self.queue.enqueue(
            EventType.ATTENTION_EVENT, payload, self.clock.now(), record_id=event_id
        ).id

    def paths(self, path: str) -> int:
        return sum(1 for r in self.server.requests if r == path)

    def pending(self) -> int:
        return self.queue.stats().depth


class Recorder:
    """Wraps the server to keep every request it got."""

    def __init__(self, server: FakeDeviceServer, seen: list[httpx.Request]) -> None:
        self.server = server
        self.seen = seen

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.server(request)


def make_rig(
    tmp_path: Path,
    *,
    enrolled: bool = True,
    bundle: Json | None = None,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Rig:
    clock = FakeClock(start_monotonic=1000.0, start_utc=T0)
    keys = InMemoryKeyProvider(KEY)
    server = FakeDeviceServer(clock=clock, workstation_id=STATION, secret=SECRET)
    server.set_config(json.dumps(bundle or bundle_json(7)).encode(), 7)
    queue = EventQueue.open(tmp_path / "queue.db", keys, clock, rng=lambda: 0.5)
    evidence = EvidenceStore.open(
        tmp_path / "evidence", keys, clock, disk_free=lambda _: 10**12, min_free_mb=1
    )
    config = ConfigStore(bundle_path(tmp_path), keys, workstation_id=STATION)
    inbox = CommandInbox.open(tmp_path / "commands.db", clock)
    creds = InMemoryCredentialStore(
        DeviceCredentials(STATION, SecretStr(SECRET)) if enrolled else None
    )
    offset = ClockOffsetEstimator()
    seen: list[httpx.Request] = []
    transport = HttpxTransport(
        URL, transport=httpx.MockTransport(handler or Recorder(server, seen))
    )
    api = DeviceApi(transport, creds, clock, offset, app_version="9.9.9")
    client = SyncClient(
        api=api,
        queue=queue,
        config_store=config,
        clock=clock,
        offset=offset,
        evidence=evidence,
        inbox=inbox,
        rng=lambda: 0.5,
    )
    return Rig(
        clock, server, queue, evidence, config, inbox, creds, offset, api, client, tmp_path, seen
    )


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = make_rig(tmp_path)
    yield r
    r.queue.close()
    r.evidence.close()
    r.inbox.close()


# -- happy path and headers ------------------------------------------------------


def test_everything_reaches_the_server_once(rig: Rig) -> None:
    ids = [rig.scan(f"B{i}") for i in range(5)]
    rig.run(10)
    assert rig.pending() == 0
    assert set(rig.server.unique_items("scan")) == set(ids)
    assert all(rig.server.item_arrivals[i] == 1 for i in ids)
    assert rig.server.heartbeats, "heartbeats are sent"
    assert rig.config.applied_version == 7
    assert rig.client.health().flags == frozenset()


def test_every_request_carries_schema_version_and_token(rig: Rig) -> None:
    rig.scan()
    rig.run(4)
    assert rig.seen
    for request in rig.seen:
        assert request.headers[HEADER_SCHEMA] == str(SCHEMA_REVISION)
        assert request.headers[HEADER_APP_VERSION] == "9.9.9"
        assert request.url.path.startswith(API_PREFIX)
        if not request.url.path.endswith("/token"):
            assert request.headers["authorization"].startswith("Bearer ")
    assert all("Bearer" not in r.content.decode(errors="ignore") for r in rig.seen)


def test_heartbeat_reports_queue_config_and_status(tmp_path: Path) -> None:
    r = make_rig(tmp_path)

    class Status:
        def station_status(self) -> StationStatus:
            return StationStatus(state=AttentionState.DROWSY, confidence=0.7, disk_free_mb=900)

    r.client._status = Status()
    r.scan()
    r.client.tick()  # heartbeat (queue depth 1), config, upload
    first = r.server.heartbeats[0]
    assert first.state is AttentionState.DROWSY
    assert first.confidence == 0.7
    assert first.disk_free_mb == 900
    assert first.queue_depth == 1
    assert first.applied_config_version is None
    r.run(4)
    assert r.server.heartbeats[-1].applied_config_version == 7
    assert r.server.heartbeats[-1].queue_depth == 0
    seqs = [h.seq for h in r.server.heartbeats]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_a_failing_status_source_still_heartbeats_unknown(rig: Rig) -> None:
    class Broken:
        def station_status(self) -> StationStatus:
            raise RuntimeError("ipc down")

    rig.client._status = Broken()
    rig.client.tick()
    assert rig.server.heartbeats[0].state is AttentionState.UNKNOWN


def test_heartbeat_interval_is_two_seconds_while_online(rig: Rig) -> None:
    rig.run(20)
    assert 9 <= len(rig.server.heartbeats) <= 11


# -- result-status table (4.3) ---------------------------------------------------


class ScriptedBatch:
    """Answers `/events:batch` with chosen per-item statuses; delegates the rest."""

    def __init__(self, server: FakeDeviceServer, statuses: dict[UUID, str | None]) -> None:
        self.server = server
        self.statuses = statuses
        self.batches: list[Json] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/events:batch"):
            return self.server(request)
        body = json.loads(request.content)
        self.batches.append(body)
        results = []
        for item in body["items"]:
            status = self.statuses.get(UUID(item["id"]), "accepted")
            if status is not None:
                results.append({"id": item["id"], "status": status, "reason": "r"})
        return httpx.Response(
            200,
            json={"batch_id": body["batch_id"], "server_time": T0.isoformat(), "results": results},
        )


@pytest.mark.parametrize(
    ("status", "deleted", "dead"),
    [
        ("accepted", True, False),
        ("duplicate", True, False),
        ("rejected", False, True),
        ("retry_later", False, False),
        ("some_new_status", False, False),  # unknown: keep and retry, never lose data
        (None, False, False),  # no result for the item: retry
    ],
)
def test_per_item_results_follow_the_status_table(
    tmp_path: Path, status: str | None, deleted: bool, dead: bool
) -> None:
    holder: list[ScriptedBatch] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return holder[0](request)

    r = make_rig(tmp_path, handler=handler)
    target = new_uuid7()
    holder.append(ScriptedBatch(r.server, {target: status}))
    other = r.scan("other")
    r.scan("target", scan_id=target)
    r.client.tick()
    assert r.queue.next_attempt_at(other) is None  # always accepted
    if deleted:
        assert r.queue.next_attempt_at(target) is None
        assert not r.queue.dead_letters()
    elif dead:
        assert r.queue.next_attempt_at(target) is None
        letters = r.queue.dead_letters()
        assert [d.id for d in letters] == [target]
        assert letters[0].reason is DeadLetterReason.REJECTED
    else:
        due = r.queue.next_attempt_at(target)
        assert due is not None and due > r.clock.now()
        r.until(lambda: len(holder[0].batches) >= 2, seconds=30)
        assert UUID(holder[0].batches[1]["items"][0]["id"]) == target


# -- error table (7) on the batch ----------------------------------------------------


class BatchStatus:
    """`/events:batch` answers `status` while `active`; everything else is the server."""

    def __init__(self, server: FakeDeviceServer, status: int, code: str | None = None) -> None:
        self.server = server
        self.status = status
        self.code = code
        self.active = True
        self.batch_ids: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.active and request.url.path.endswith("/events:batch"):
            self.batch_ids.append(json.loads(request.content)["batch_id"])
            body: Json = {"status": self.status, "title": "x"}
            if self.code:
                body["code"] = self.code
            return httpx.Response(
                self.status, json=body, headers={"content-type": "application/problem+json"}
            )
        return self.server(request)


@pytest.mark.parametrize(
    ("status", "flag"),
    [
        (403, SyncFlag.FORBIDDEN),
        (404, None),
        (409, None),
        (429, SyncFlag.SERVER_ERROR),
        (500, SyncFlag.SERVER_ERROR),
        (503, SyncFlag.SERVER_ERROR),
    ],
)
def test_transient_batch_errors_keep_records_and_resend_the_same_batch(
    tmp_path: Path, status: int, flag: SyncFlag | None
) -> None:
    holder: list[BatchStatus] = []
    r = make_rig(tmp_path, handler=lambda req: holder[0](req))
    holder.append(BatchStatus(r.server, status))
    ids = [r.scan(f"B{i}") for i in range(3)]
    r.run(30)
    answers = holder[0]
    assert len(answers.batch_ids) >= 3
    assert len(set(answers.batch_ids)) == 1, "a failed batch is resent with the same batch_id"
    gaps = r.pending()
    assert gaps == 3 and not r.queue.dead_letters()
    flags = r.client.health().flags
    assert (flag in flags) if flag else not (flags - {SyncFlag.SERVER_ERROR})
    answers.active = False
    r.until(lambda: r.pending() == 0, seconds=400)
    assert set(r.server.unique_items("scan")) == set(ids)
    assert r.client.health().flags == frozenset()


def test_batch_backoff_grows_to_five_minutes(tmp_path: Path) -> None:
    holder: list[BatchStatus] = []
    r = make_rig(tmp_path, handler=lambda req: holder[0](req))
    holder.append(BatchStatus(r.server, 503))
    r.scan()
    times: list[float] = []
    count = 0
    for _ in range(int(1800 / 0.5)):
        r.client.tick()
        if len(holder[0].batch_ids) != count:
            count = len(holder[0].batch_ids)
            times.append(r.clock.monotonic())
        r.clock.advance(0.5)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert gaps[0] <= 2.0
    assert max(gaps) <= 300.5
    assert gaps[-1] >= 225  # equal jitter: at least half of the 5-minute step


@pytest.mark.parametrize("status", [400, 422, 413])
def test_bad_batch_is_split_until_the_bad_item_is_dead_lettered(
    tmp_path: Path, status: int
) -> None:
    bad = new_uuid7()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events:batch"):
            ids = {UUID(i["id"]) for i in json.loads(request.content)["items"]}
            if bad in ids:
                return httpx.Response(status, json={"status": status, "title": "bad"})
        return r.server(request)

    r = make_rig(tmp_path, handler=handler)
    good = [r.scan(f"G{i}") for i in range(3)]
    r.scan("bad", scan_id=bad)
    good += [r.scan(f"G{i}") for i in range(3, 6)]
    r.run(20)
    assert set(r.server.unique_items("scan")) == set(good)
    assert [d.id for d in r.queue.dead_letters()] == [bad]
    assert r.pending() == 0


def test_payload_too_large_splits_and_delivers_everything(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events:batch") and len(json.loads(request.content)["items"]) > 2:
            return httpx.Response(413, json={"status": 413, "title": "too big"})
        return r.server(request)

    r = make_rig(tmp_path, handler=handler)
    ids = [r.scan(f"B{i}") for i in range(7)]
    r.run(20)
    assert set(r.server.unique_items("scan")) == set(ids)
    assert not r.queue.dead_letters()


def test_unreadable_batch_result_resends_the_same_batch(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events:batch"):
            calls.append(json.loads(request.content)["batch_id"])
            if len(calls) == 1:
                return httpx.Response(200, content=b"<html>proxy</html>")
        return r.server(request)

    r = make_rig(tmp_path, handler=handler)
    r.scan()
    r.until(lambda: r.pending() == 0, seconds=30)
    assert len(calls) == 2 and calls[0] == calls[1]


# -- loss, duplication, reordering ---------------------------------------------------


def test_lost_request_resends_the_same_batch(rig: Rig) -> None:
    ids = [rig.scan(f"B{i}") for i in range(3)]
    rig.server.inject("/events:batch", Fault.DROP_REQUEST)
    rig.client.tick()
    assert rig.pending() == 3
    assert SyncFlag.OFFLINE in rig.client.health().flags
    rig.until(lambda: rig.pending() == 0, seconds=30)
    assert all(rig.server.item_arrivals[i] == 1 for i in ids)
    assert SyncFlag.OFFLINE not in rig.client.health().flags


def test_lost_response_is_replayed_by_batch_id_without_reprocessing(rig: Rig) -> None:
    ids = [rig.scan(f"B{i}") for i in range(3)]
    rig.server.inject("/events:batch", Fault.DROP_RESPONSE)
    rig.client.tick()
    assert set(rig.server.unique_items("scan")) == set(ids)  # the server has them
    assert rig.pending() == 3  # the client does not know yet
    rig.until(lambda: rig.pending() == 0, seconds=30)
    assert len(rig.server.batches) == 1
    assert all(rig.server.item_arrivals[i] == 1 for i in ids)


def test_duplicated_request_gives_the_same_end_state(rig: Rig) -> None:
    ids = [rig.scan(f"B{i}") for i in range(3)]
    rig.server.inject("/events:batch", Fault.DUPLICATE)
    rig.until(lambda: rig.pending() == 0, seconds=30)
    assert sorted(rig.server.unique_items()) == sorted(ids)


def test_duplicate_items_across_batches_are_deleted(rig: Rig) -> None:
    """The server already has an item (e.g. a batch sent before a crash)."""
    sid = rig.scan()
    rig.server.items[sid] = {"type": "scan"}
    rig.run(4)
    assert rig.pending() == 0
    assert rig.server.item_arrivals[sid] == 1


class OneAtATime:
    """Claims one record per batch, so parents and children travel separately."""

    def __init__(self, queue: EventQueue) -> None:
        self.queue = queue

    def claim_batch(self) -> list[ClaimedRecord]:
        return self.queue.claim_batch(max_items=1)

    def apply_results(
        self, claimed: Sequence[ClaimedRecord], results: Sequence[ItemResult]
    ) -> ApplySummary:
        return self.queue.apply_results(claimed, results)

    def dead_letter(
        self, record_id: UUID, reason: DeadLetterReason, detail: str | None = None
    ) -> bool:
        return self.queue.dead_letter(record_id, reason, detail)

    def stats(self) -> QueueStats:
        return self.queue.stats()

    def enqueue(self, item_type: str, payload: BaseModel, occurred_at: datetime) -> object:
        return self.queue.enqueue(item_type, payload, occurred_at)


def test_child_before_parent_is_retried_later_until_the_parent_arrives(rig: Rig) -> None:
    rig.client._queue = OneAtATime(rig.queue)
    scan_id = new_uuid7()
    event_id = rig.event(scan_id)  # queued first: arrives before its scan
    rig.scan(scan_id=scan_id)
    rig.client.tick()
    assert event_id not in rig.server.items  # retry_later
    assert rig.queue.next_attempt_at(event_id) is not None
    rig.until(lambda: rig.pending() == 0, seconds=60)
    assert event_id in rig.server.items and scan_id in rig.server.items
    assert rig.server.item_arrivals[event_id] == 2


def test_reordered_delivery_of_an_old_batch_changes_nothing(rig: Rig) -> None:
    rig.client._queue = OneAtATime(rig.queue)
    first = rig.scan("first")
    rig.server.inject("/events:batch", Fault.DEFER)
    rig.client.tick()  # held by the network
    second_ids: list[UUID] = []
    rig.until(lambda: rig.pending() == 0, seconds=60)  # resent and accepted
    second_ids.append(rig.scan("second"))
    rig.until(lambda: rig.pending() == 0, seconds=30)
    assert rig.server.release_deferred() == 1  # the old copy finally arrives
    assert sorted(rig.server.unique_items()) == sorted([first, *second_ids])
    assert rig.server.item_arrivals[first] == 1  # replayed by batch_id, not reprocessed


def test_reordered_parent_and_child_across_lost_batches(rig: Rig) -> None:
    rig.client._queue = OneAtATime(rig.queue)
    scan_id = new_uuid7()
    rig.scan(scan_id=scan_id)
    event_id = rig.event(scan_id)
    rig.server.inject("/events:batch", Fault.DEFER)  # the scan's batch is held
    rig.client.tick()
    rig.until(lambda: rig.pending() == 0, seconds=120)
    rig.server.release_deferred()
    assert {scan_id, event_id} <= set(rig.server.items)
    assert len(rig.server.items) == 2


# -- 426 ------------------------------------------------------------------------------


def test_update_required_keeps_buffering_and_resumes_after_update(rig: Rig) -> None:
    rig.server.min_schema = SCHEMA_REVISION + 1
    ids = [rig.scan(f"B{i}") for i in range(3)]
    rig.run(60)
    assert SyncFlag.UPDATE_REQUIRED in rig.client.health().flags
    assert rig.pending() == 3 and not rig.queue.dead_letters()
    ids.append(rig.scan("while outdated"))  # scanning and buffering go on
    attempts = len(rig.server.requests)
    rig.run(60)
    assert len(rig.server.requests) - attempts <= 3, "426 is probed slowly, not hammered"
    rig.server.min_schema = SCHEMA_REVISION  # the server (or the app) was updated
    rig.until(lambda: rig.pending() == 0, seconds=400)
    assert set(rig.server.unique_items("scan")) == set(ids)
    assert SyncFlag.UPDATE_REQUIRED not in rig.client.health().flags


# -- tokens (3, 7: 401) ----------------------------------------------------------------


def test_expired_token_is_refreshed_once_and_the_request_resent(rig: Rig) -> None:
    rig.run(4)
    tokens = rig.paths("POST /token")
    rig.server.expire_tokens()
    sid = rig.scan()
    rig.run(2)
    assert rig.paths("POST /token") == tokens + 1
    assert sid in rig.server.items
    assert SyncFlag.AUTH_FAILED not in rig.client.health().flags


def test_token_is_renewed_before_it_expires(rig: Rig) -> None:
    rig.run(4)
    assert rig.paths("POST /token") == 1
    rig.run(850, step=2.0)
    assert rig.paths("POST /token") >= 2
    statuses = [r for r in rig.server.requests if r.startswith("POST /heartbeat")]
    assert statuses  # no 401 was needed: the server never answered one
    assert SyncFlag.AUTH_FAILED not in rig.client.health().flags


def test_persistent_401_refreshes_once_then_backs_off(tmp_path: Path) -> None:
    tokens: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events:batch"):
            return httpx.Response(401, json={"status": 401, "title": "no"})
        if request.url.path.endswith("/token"):
            tokens.append(1)
        return r.server(request)

    r = make_rig(tmp_path, handler=handler)
    r.scan()
    r.client._heartbeat.due = 10**9  # isolate the upload channel
    r.client._config_wanted = False
    r.client.tick()
    assert len(tokens) == 2  # the first token, then exactly one refresh
    assert SyncFlag.AUTH_FAILED in r.client.health().flags
    assert r.pending() == 1
    r.client.tick()
    assert len(tokens) == 2, "backing off: no retry inside the backoff window"


def test_revoked_device_keeps_working_offline_and_warns(rig: Rig) -> None:
    rig.run(4)
    rig.server.revoked = True
    sid = rig.scan()
    rig.run(30)
    flags = rig.client.health().flags
    assert SyncFlag.DEVICE_REVOKED in flags
    assert rig.pending() == 1
    before = len(rig.server.requests)
    rig.run(120)
    assert len(rig.server.requests) - before <= 3  # long backoff
    rig.server.revoked = False
    rig.until(lambda: rig.pending() == 0, seconds=400)
    assert sid in rig.server.items


def test_wrong_secret_reports_auth_failed(rig: Rig) -> None:
    rig.creds.save(DeviceCredentials(STATION, SecretStr("wrong")))
    rig.run(4)
    assert SyncFlag.AUTH_FAILED in rig.client.health().flags
    assert not rig.server.heartbeats


def test_not_enrolled_makes_no_requests(tmp_path: Path) -> None:
    r = make_rig(tmp_path, enrolled=False)
    r.scan()
    assert r.client.tick() == 5.0
    assert SyncFlag.NOT_ENROLLED in r.client.health().flags
    assert not r.server.requests


def test_enrollment_stores_credentials_and_sync_starts(tmp_path: Path) -> None:
    r = make_rig(tmp_path, enrolled=False)
    r.server.enrollment_token = "one-time"  # noqa: S105 - test fixture
    with pytest.raises(ApiError) as bad:
        r.api.enroll(
            EnrollRequest(
                enrollment_token=SecretStr("nope"), hostname="ws3", hardware_fingerprint="fp"
            )
        )
    assert bad.value.status == 401
    reply = r.api.enroll(
        EnrollRequest(
            enrollment_token=SecretStr("one-time"), hostname="ws3", hardware_fingerprint="fp"
        )
    )
    assert reply.workstation_id == STATION
    creds = r.creds.load()
    assert creds is not None and creds.device_secret.get_secret_value() == SECRET
    sid = r.scan()
    r.run(4)
    assert sid in r.server.items
    assert SyncFlag.NOT_ENROLLED not in r.client.health().flags


def test_token_without_credentials_raises(tmp_path: Path) -> None:
    r = make_rig(tmp_path, enrolled=False)
    with pytest.raises(NotEnrolledError):
        r.api.token()


def test_unreadable_token_reply_is_a_transport_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, content=b"{}")
        return r.server(request)

    r = make_rig(tmp_path, handler=handler)
    with pytest.raises(TransportError):
        r.api.token()


# -- evidence (4.4) --------------------------------------------------------------------


def _warned_scan_with_images(r: Rig, frames: int = 2) -> tuple[UUID, list[UUID]]:
    scan_id = r.scan()
    event_id = new_uuid7()
    saved = save(r.evidence, burst(event_id, sizes=(5000,) * frames, at=T0))
    r.event(scan_id, event_id=event_id)
    return event_id, list(saved.image_ids)


def test_images_are_uploaded_after_their_event(rig: Rig) -> None:
    _, images = _warned_scan_with_images(rig)
    rig.until(lambda: not rig.evidence.pending_uploads(), seconds=30)
    assert set(rig.server.evidence) == set(images)
    first_put = next(i for i, p in enumerate(rig.server.requests) if p.startswith("PUT"))
    first_batch = next(i for i, p in enumerate(rig.server.requests) if "events:batch" in p)
    assert first_batch < first_put, "metadata before images"
    stored = rig.evidence.image(images[0])
    assert stored is not None and stored.uploaded and stored.present


def test_lost_image_response_is_idempotent_200(rig: Rig) -> None:
    _, images = _warned_scan_with_images(rig, frames=1)
    rig.server.inject("/evidence/", Fault.DROP_RESPONSE)
    rig.until(lambda: not rig.evidence.pending_uploads(), seconds=60)
    assert set(rig.server.evidence) == set(images)
    assert rig.paths(f"PUT /evidence/{images[0]}") == 2


def test_consent_missing_deletes_the_local_images(rig: Rig) -> None:
    event_id, _ = _warned_scan_with_images(rig)
    rig.server.consent_missing_events.add(event_id)
    rig.run(10)
    assert not rig.server.evidence
    rows = rig.evidence.event_images(event_id)
    assert rows and all(r.removed_reason is RemovalReason.CONSENT_MISSING for r in rows)
    assert not rig.evidence.pending_uploads()
    put_count = sum(1 for p in rig.server.requests if p.startswith("PUT"))
    rig.run(20)
    assert sum(1 for p in rig.server.requests if p.startswith("PUT")) == put_count


def test_image_of_an_unknown_event_is_retried_later(rig: Rig) -> None:
    event_id = new_uuid7()
    images = save(rig.evidence, burst(event_id, sizes=(5000,), at=T0)).image_ids
    rig.run(4)  # no event yet: 404 event_unknown
    assert not rig.server.evidence
    assert rig.evidence.pending_uploads()
    rig.event(rig.scan(), event_id=event_id)
    rig.until(lambda: not rig.evidence.pending_uploads(), seconds=60)
    assert set(rig.server.evidence) == set(images)


def test_hash_mismatch_is_not_retried(rig: Rig) -> None:
    _, images = _warned_scan_with_images(rig, frames=1)
    rig.server.evidence[images[0]] = ("0" * 64, b"other")
    rig.run(20)
    assert rig.paths(f"PUT /evidence/{images[0]}") == 1
    assert rig.client.health().evidence_rejected == 1
    assert rig.evidence.pending_uploads()  # kept locally until the age purge


@pytest.mark.parametrize("status", [503, 426])
def test_image_channel_errors_back_off(tmp_path: Path, status: int) -> None:
    holder: list[Rig] = []
    puts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            if len(puts) <= 2:
                return httpx.Response(status, json={"status": status, "title": "x"})
        return holder[0].server(request)

    r = make_rig(tmp_path, handler=handler)
    holder.append(r)
    _, images = _warned_scan_with_images(r, frames=2)
    r.client.tick()
    r.client.tick()
    r.until(lambda: not r.evidence.pending_uploads(), seconds=700)
    assert set(r.server.evidence) == set(images)


def test_too_large_image_is_skipped(rig: Rig) -> None:
    event_id = new_uuid7()
    save(rig.evidence, burst(event_id, sizes=(2 * 1024 * 1024 + 10,), at=T0))
    rig.event(rig.scan(), event_id=event_id)
    rig.run(10)
    assert not rig.server.evidence
    assert rig.client.health().evidence_rejected == 1


# -- config (4.2) ----------------------------------------------------------------------


def test_config_is_pulled_once_and_then_answered_304(rig: Rig) -> None:
    rig.run(20)
    assert rig.config.applied_version == 7
    assert rig.paths("GET /config") == 1
    bundle = load_bundle(bundle_path(rig.data_dir), InMemoryKeyProvider(KEY))
    assert bundle is not None and bundle.version == 7  # the child and UI can load it


def test_new_version_in_heartbeat_pulls_and_applies(rig: Rig) -> None:
    applied: list[int] = []
    rig.client._on_config_applied = lambda b: applied.append(b.version)
    rig.run(4)
    rig.server.set_config(json.dumps(bundle_json(8)).encode(), 8)
    rig.run(4)
    assert rig.config.applied_version == 8
    assert applied == [7, 8]


def test_invalid_config_is_rejected_previous_stays_and_is_reported(rig: Rig) -> None:
    rig.run(4)
    bad = bundle_json(8)
    bad["cameras"] = [{"slot": 1, "role": "primary", "kind": "laser", "uri": "0"}]
    rig.server.set_config(json.dumps(bad).encode(), 8)
    rig.run(10)
    assert rig.config.applied_version == 7
    stored = load_bundle(bundle_path(rig.data_dir), InMemoryKeyProvider(KEY))
    assert stored is not None and stored.version == 7
    health = rig.client.health()
    assert SyncFlag.CONFIG_REJECTED in health.flags
    assert health.rejected_config is not None and health.rejected_config.version == 8
    last = rig.server.heartbeats[-1]
    assert last.rejected_config is not None and last.rejected_config.version == 8
    assert "kind" in last.rejected_config.reason and "laser" not in last.rejected_config.reason
    assert last.applied_config_version == 7
    assert rig.paths("GET /config") == 2, "a refused version is not pulled again"
    rig.server.set_config(json.dumps(bundle_json(9)).encode(), 9)
    rig.run(4)
    assert rig.config.applied_version == 9
    assert rig.server.heartbeats[-1].rejected_config is None
    assert SyncFlag.CONFIG_REJECTED not in rig.client.health().flags


def test_garbage_config_is_rejected_with_the_advertised_version(rig: Rig) -> None:
    rig.run(4)
    rig.server.set_config(b"not json at all", 8)
    rig.run(4)
    rejected = rig.client.health().rejected_config
    assert rejected is not None and rejected.version == 8
    assert rig.config.applied_version == 7


def test_bundle_for_another_workstation_is_rejected(tmp_path: Path) -> None:
    other = bundle_json(7)
    other["workstation"]["id"] = str(new_uuid7())
    r = make_rig(tmp_path, bundle=other)
    r.run(4)
    assert r.config.applied_version is None
    rejected = r.client.health().rejected_config
    assert rejected is not None and "another workstation" in rejected.reason


def test_config_errors_back_off_and_recover(rig: Rig) -> None:
    rig.server.inject("/config", Fault.STATUS, times=2, status=500)
    rig.run(10)
    assert rig.config.applied_version == 7


# -- commands (4.1) ----------------------------------------------------------------------


class Sessions:
    def __init__(self) -> None:
        self.assigned: list[UUID] = []
        self.ended: list[UUID] = []

    def handle_assign_worker(self, command: Any) -> str:  # Any: duck-typed command
        self.assigned.append(command.id)
        return "started"

    def handle_end_session(self, command: Any) -> str:  # Any: duck-typed command
        self.ended.append(command.id)
        return "ended"


def _assign(cid: UUID) -> Json:
    return {
        "id": str(cid),
        "type": "assign_worker",
        "assignment_id": str(new_uuid7()),
        "user_id": str(new_uuid7()),
        "valid_until": (T0 + timedelta(hours=8)).isoformat(),
    }


def test_session_command_is_acked_only_after_the_ui_handled_it(rig: Rig) -> None:
    from ag_workstation.sync import CommandPump

    cid = new_uuid7()
    rig.server.add_command(_assign(cid))
    rig.run(10)  # repeated every heartbeat, stored once, not acked
    assert len(rig.inbox.pending()) == 1
    assert cid not in rig.server.acked
    sessions = Sessions()
    assert CommandPump(rig.inbox, sessions).poll() == 1
    assert sessions.assigned == [cid]
    rig.run(4)
    assert cid in rig.server.acked
    assert not rig.server.commands
    assert rig.inbox.handled_unacked() == []
    assert CommandPump(rig.inbox, sessions).poll() == 0
    assert sessions.assigned == [cid]


def test_end_session_goes_through_the_inbox(rig: Rig) -> None:
    from ag_workstation.sync import CommandPump

    cid = new_uuid7()
    rig.server.add_command({"id": str(cid), "type": "end_session", "session_id": str(new_uuid7())})
    rig.run(4)
    sessions = Sessions()
    CommandPump(rig.inbox, sessions).poll()
    rig.run(4)
    assert sessions.ended == [cid] and cid in rig.server.acked


def test_pull_config_command_fetches_and_is_acked(rig: Rig) -> None:
    rig.run(4)
    rig.server.config_version = 7
    cid = new_uuid7()
    rig.server.add_command({"id": str(cid), "type": "pull_config", "version": 7})
    rig.run(6)
    assert rig.paths("GET /config") == 2  # pulled again (304), then acked
    assert cid in rig.server.acked


def test_rotate_secret_rotates_once_and_the_new_secret_works(rig: Rig) -> None:
    cid = new_uuid7()
    rig.server.add_command({"id": str(cid), "type": "rotate_secret"})
    rig.run(10)
    assert rig.server.rotations == 1
    assert cid in rig.server.acked
    creds = rig.creds.load()
    assert creds is not None and creds.device_secret.get_secret_value() == rig.server.secret
    rig.server.expire_tokens()
    rig.clock.advance(700)  # the old secret's 10-minute grace is over
    sid = rig.scan()
    rig.run(4)
    assert sid in rig.server.items


def test_rotated_secret_survives_a_failing_credential_store(rig: Rig) -> None:
    rig.creds.fail_saves = 3
    rig.server.add_command({"id": str(new_uuid7()), "type": "rotate_secret"})
    rig.run(1)
    assert SyncFlag.CREDENTIAL_STORE_FAILED in rig.client.health().flags
    rig.server.expire_tokens()
    rig.clock.advance(700)
    sid = rig.scan()
    rig.run(4)
    assert sid in rig.server.items  # the in-memory secret is used
    creds = rig.creds.load()
    assert creds is not None and creds.device_secret.get_secret_value() == rig.server.secret
    assert SyncFlag.CREDENTIAL_STORE_FAILED not in rig.client.health().flags


def test_rotation_failure_is_retried_on_the_repeat(rig: Rig) -> None:
    cid = new_uuid7()
    rig.server.add_command({"id": str(cid), "type": "rotate_secret"})
    rig.server.inject("/secret/rotate", Fault.STATUS, status=503)
    rig.run(10)
    assert rig.server.rotations == 1 and cid in rig.server.acked


@pytest.mark.parametrize(
    "command",
    [
        {"type": "live_still", "request_id": None, "camera_slot": 1, "expires_at": None},
        {"type": "live_video", "session_id": None, "camera_slot": 1, "expires_at": None},
        {"type": "self_destruct"},
    ],
)
def test_unsupported_and_unknown_commands_are_acked_without_action(rig: Rig, command: Json) -> None:
    cid = new_uuid7()
    body = {
        k: (str(new_uuid7()) if v is None and k.endswith("id") else v) for k, v in command.items()
    }
    if "expires_at" in body:
        body["expires_at"] = (T0 + timedelta(minutes=1)).isoformat()
    rig.server.add_command({"id": str(cid), **body})
    rig.run(6)
    assert cid in rig.server.acked
    assert not rig.inbox.pending()


def test_lost_ack_heartbeat_does_not_execute_twice(rig: Rig) -> None:
    cid = new_uuid7()
    rig.server.add_command({"id": str(cid), "type": "rotate_secret"})
    rig.client.tick()  # heartbeat delivers the command; rotation done
    rig.server.inject("/heartbeat", Fault.DROP_REQUEST, times=2)
    rig.run(20)
    assert rig.server.rotations == 1
    assert cid in rig.server.acked


# -- clock offset (6) --------------------------------------------------------------------


def test_clock_offset_is_estimated_reported_and_drift_alerted_once(rig: Rig) -> None:
    rig.server.skew = timedelta(seconds=5)
    rig.run(20)
    health = rig.client.health()
    assert abs(health.clock_offset_ms - 5000) <= 50
    assert abs(rig.server.heartbeats[-1].clock_offset_ms - 5000) <= 50
    assert SyncFlag.CLOCK_DRIFT in health.flags
    alerts = [
        i
        for i, d in rig.server.items.items()
        if d["type"] == "local_alert" and d["payload"]["kind"] == LocalAlertKind.CLOCK_DRIFT  # type: ignore[index,call-overload]  # JSON
    ]
    assert len(alerts) == 1
    assert abs(rig.client.utc.now() - (rig.clock.now() + timedelta(seconds=5))) < timedelta(
        milliseconds=50
    )
    rig.server.skew = timedelta(milliseconds=300)
    rig.run(10)
    assert SyncFlag.CLOCK_DRIFT not in rig.client.health().flags


def test_small_offset_raises_no_drift(rig: Rig) -> None:
    rig.server.skew = timedelta(milliseconds=800)
    rig.run(10)
    assert SyncFlag.CLOCK_DRIFT not in rig.client.health().flags
    assert not [d for d in rig.server.items.values() if d["type"] == "local_alert"]


# -- heartbeat backoff and the boundary --------------------------------------------------


def test_heartbeat_backs_off_to_thirty_seconds(rig: Rig) -> None:
    rig.server.inject("/heartbeat", Fault.STATUS, times=100, status=503)
    times: list[float] = []
    count = 0
    for _ in range(400):
        rig.client.tick()
        n = rig.paths("POST /heartbeat")
        if n != count:
            count = n
            times.append(rig.clock.monotonic())
        rig.clock.advance(0.5)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert gaps[0] <= 2.5
    assert max(gaps) <= 30.5
    assert gaps[-1] >= 29.5


def test_reconnect_wakes_channels_waiting_on_a_network_backoff(rig: Rig) -> None:
    rig.server.inject("/", Fault.DROP_REQUEST, times=40)
    sid = rig.scan()
    rig.run(120)
    assert rig.client._upload.due - rig.clock.monotonic() > 60  # deep in its backoff
    rig.server.rules.clear()
    rig.run(32)  # the next heartbeat (at most 30 s) succeeds and wakes the upload
    assert sid in rig.server.items


def test_tick_never_raises(rig: Rig) -> None:
    class Broken:
        def claim_batch(self) -> list[ClaimedRecord]:
            raise RuntimeError("disk on fire")

        def stats(self) -> QueueStats:
            raise RuntimeError("disk on fire")

    rig.client._queue = Broken()  # type: ignore[assignment]  # partial fake on purpose
    for _ in range(5):
        assert isinstance(rig.client.tick(), float)
        rig.clock.advance(1)
    assert SyncFlag.SYNC_ERROR in rig.client.health().flags
    assert rig.server.heartbeats  # heartbeat still went out


def test_unreadable_credentials_flag_and_sleep(rig: Rig) -> None:
    class Broken:
        def load(self) -> DeviceCredentials:
            raise OSError("gone")

        def save(self, credentials: DeviceCredentials) -> None:
            raise OSError("gone")

    rig.api._store = Broken()
    assert rig.client.tick() == 5.0
    assert SyncFlag.CREDENTIAL_STORE_FAILED in rig.client.health().flags


def test_http_response_ok() -> None:
    assert HttpResponse(204).ok and not HttpResponse(304).ok
