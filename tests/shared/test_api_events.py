"""Event batch items, per-item validation and batch results."""

from uuid import UUID

import pytest
from ag_shared.api import (
    INGEST_ORDER,
    MAX_BATCH_ITEMS,
    AttentionEventItem,
    BatchResult,
    BucketItem,
    DisputeItem,
    EventBatch,
    EventType,
    HealthItem,
    ImagesWithheld,
    ItemStatus,
    LocalAlertItem,
    LocalAlertKind,
    RawEventBatch,
    ScanItem,
    SessionEndItem,
    SessionEndReason,
    SessionMethod,
    SessionStartItem,
    UnknownEventItem,
    parse_event_item,
)
from ag_shared.health import EngineStatus
from ag_shared.ids import Uuid7Generator
from ag_shared.states import AttentionState, ScanOutcome
from pydantic import ValidationError

ids = Uuid7Generator()
U = UUID("0193a0b0-0000-7000-8000-0000000000aa")
T = "2026-11-20T12:00:00.000Z"
T2 = "2026-11-20T12:00:09.500Z"

PAYLOADS: dict[str, dict[str, object]] = {
    "session_start": {"user_id": str(U), "method": "employee_id_pin"},
    "session_end": {"session_id": str(U), "end_reason": "logout"},
    "scan": {
        "session_id": str(U),
        "user_id": str(U),
        "barcode": "4006381333931",
        "state_at_scan": "drowsy",
        "confidence": 0.7,
        "outcome": "warned",
    },
    "attention_event": {
        "kind": "scan_warning",
        "state": "drowsy",
        "confidence": 0.7,
        "started_at": T,
        "scan_id": str(U),
        "signals": {"perclos": 0.41, "ear": None},
        "camera_states": [{"slot": 1, "status": "online", "state": "drowsy", "quality": 0.8}],
        "engine_version": "1.0.0",
        "config_version": 3,
        "image_ids": [str(U)],
    },
    "bucket": {"bucket_start": "2026-11-20T12:01:00Z", "seconds": {"attentive": 50, "drowsy": 10}},
    "dispute": {"attention_event_id": str(U), "user_id": str(U), "message": "I was reading"},
    "health": {"engine_status": "running", "cpu_pct": 30, "dead_letter_count": 2},
    "local_alert": {"kind": "disk_full", "detail": {"free_mb": 10}},
}

ITEM_CLASSES = {
    "session_start": SessionStartItem,
    "session_end": SessionEndItem,
    "scan": ScanItem,
    "attention_event": AttentionEventItem,
    "bucket": BucketItem,
    "dispute": DisputeItem,
    "health": HealthItem,
    "local_alert": LocalAlertItem,
}


def _item(kind: str, payload: dict[str, object] | None = None, seq: int = 1) -> dict[str, object]:
    return {
        "type": kind,
        "id": str(ids()),
        "seq": seq,
        "occurred_at": T,
        "payload": PAYLOADS[kind] if payload is None else payload,
    }


@pytest.mark.parametrize("kind", list(PAYLOADS))
def test_every_item_type_parses_and_round_trips(kind: str) -> None:
    item = parse_event_item(_item(kind))
    assert type(item) is ITEM_CLASSES[kind]
    assert item.type == kind
    assert parse_event_item(item.model_dump(mode="json")) == item


def test_known_types_cover_the_contract_and_the_ingest_order() -> None:
    assert set(PAYLOADS) == set(EventType)
    assert set(INGEST_ORDER) == set(EventType)
    assert INGEST_ORDER[:3] == (EventType.SESSION_START, EventType.SCAN, EventType.ATTENTION_EVENT)


def test_unknown_item_type_parses_as_unknown() -> None:
    raw = {"type": "biometrics", "id": str(U), "seq": 1, "occurred_at": T, "payload": {"a": 1}}
    item = parse_event_item(raw)
    assert isinstance(item, UnknownEventItem)
    assert item.payload == {"a": 1}


def test_unknown_enum_values_inside_payloads_get_safe_defaults() -> None:
    scan = parse_event_item(
        _item("scan", {**PAYLOADS["scan"], "state_at_scan": "zoned_out", "outcome": "blocked"})
    )
    assert isinstance(scan, ScanItem)
    assert scan.payload.state_at_scan is AttentionState.UNKNOWN
    assert scan.payload.outcome is ScanOutcome.UNMONITORED
    end = parse_event_item(_item("session_end", {"session_id": str(U), "end_reason": "fire"}))
    assert isinstance(end, SessionEndItem)
    assert end.payload.end_reason is SessionEndReason.LOGOUT
    start = parse_event_item(_item("session_start", {"user_id": str(U), "method": "face"}))
    assert isinstance(start, SessionStartItem)
    assert start.payload.method is SessionMethod.EMPLOYEE_ID_PIN
    alert = parse_event_item(_item("local_alert", {"kind": "gpu_hot"}))
    assert isinstance(alert, LocalAlertItem)
    assert alert.payload.kind is LocalAlertKind.OTHER
    event = parse_event_item(
        _item("attention_event", {**PAYLOADS["attention_event"], "images_withheld": "solar"})
    )
    assert isinstance(event, AttentionEventItem)
    assert event.payload.images_withheld is ImagesWithheld.NONE
    health = parse_event_item(_item("health", {"engine_status": "zombie"}))
    assert isinstance(health, HealthItem)
    assert health.payload.engine_status is EngineStatus.UNKNOWN
    bucket = parse_event_item(
        _item("bucket", {"bucket_start": T, "seconds": {"attentive": 30, "meditating": 30}})
    )
    assert isinstance(bucket, BucketItem)
    assert bucket.payload.seconds == {AttentionState.ATTENTIVE: 30}


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("attention_event", {**PAYLOADS["attention_event"], "scan_id": None}),
        ("attention_event", {**PAYLOADS["attention_event"], "ended_at": T2}),
        (
            "attention_event",
            {
                **PAYLOADS["attention_event"],
                "kind": "state_episode",
                "ended_at": "2026-11-20T11:00:00Z",
            },
        ),
        ("attention_event", {**PAYLOADS["attention_event"], "confidence": 2}),
        ("bucket", {"bucket_start": "2026-11-20T12:01:30Z", "seconds": {"attentive": 1}}),
        ("bucket", {"bucket_start": T, "seconds": {"attentive": 40, "absent": 40}}),
        ("session_start", {"user_id": str(U), "method": "supervisor_assigned"}),
        ("dispute", {"attention_event_id": str(U), "user_id": str(U), "message": ""}),
        ("scan", {**PAYLOADS["scan"], "barcode": "x" * 5000}),
    ],
)
def test_invalid_items_are_refused(kind: str, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_event_item(_item(kind, payload))


def test_state_episode_may_be_open_or_closed() -> None:
    base = {**PAYLOADS["attention_event"], "kind": "state_episode", "scan_id": None}
    assert isinstance(parse_event_item(_item("attention_event", base)), AttentionEventItem)
    closed = parse_event_item(_item("attention_event", {**base, "ended_at": T2}))
    assert isinstance(closed, AttentionEventItem)
    assert closed.payload.ended_at is not None


def test_one_bad_item_does_not_fail_the_raw_batch() -> None:
    good, bad = _item("scan"), _item("bucket", {"bucket_start": T, "seconds": {"x": "y"}})
    bad["payload"] = {"bucket_start": "not a time", "seconds": {}}
    raw = RawEventBatch.model_validate(
        {"batch_id": str(U), "sent_at": T, "items": [good, bad], "client_note": "x"}
    )
    outcomes = []
    for item in raw.items:
        try:
            parse_event_item(item)
            outcomes.append("ok")
        except ValidationError:
            outcomes.append("rejected")
    assert outcomes == ["ok", "rejected"]


def test_typed_batch_round_trips_and_enforces_limits() -> None:
    batch = EventBatch.model_validate(
        {
            "batch_id": str(U),
            "sent_at": T,
            "items": [_item(k, seq=i) for i, k in enumerate(PAYLOADS)],
        }
    )
    assert EventBatch.model_validate_json(batch.model_dump_json()) == batch
    assert RawEventBatch.model_validate_json(batch.model_dump_json()).batch_id == U
    too_many = [_item("health", seq=i) for i in range(MAX_BATCH_ITEMS + 1)]
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"batch_id": str(U), "sent_at": T, "items": too_many})
    dup = _item("scan")
    with pytest.raises(ValidationError, match="unique"):
        EventBatch.model_validate({"batch_id": str(U), "sent_at": T, "items": [dup, dup]})


def test_batch_result_statuses() -> None:
    result = BatchResult.model_validate(
        {
            "batch_id": str(U),
            "server_time": T,
            "results": [
                {"id": str(U), "status": "accepted"},
                {"id": str(U), "status": "duplicate"},
                {"id": str(U), "status": "rejected", "reason": "invalid payload"},
                {"id": str(U), "status": "retry_later", "reason": "scan missing"},
                {"id": str(U), "status": "quarantined"},
            ],
        }
    )
    statuses = [r.status for r in result.results]
    assert statuses == [
        ItemStatus.ACCEPTED,
        ItemStatus.DUPLICATE,
        ItemStatus.REJECTED,
        ItemStatus.RETRY_LATER,
        ItemStatus.RETRY_LATER,  # unknown: keep the record and retry
    ]
    assert [s.deletes_record for s in statuses] == [True, True, False, False, False]
