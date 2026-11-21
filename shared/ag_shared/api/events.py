"""Event batch upload (`api-contract.md` sections 4.3 and 5).

Two views of the same JSON:

- `EventBatch` is fully typed; the client builds it.
- `RawEventBatch` keeps items as raw JSON objects; the server validates the
  envelope with it and each item separately with `parse_event_item`, so one
  bad item is `rejected` without failing the batch (contract 5.3).

An item `type` this side does not know parses as `UnknownEventItem`.
"""

from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import (
    BeforeValidator,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    TypeAdapter,
    model_validator,
)

from ag_shared.api.common import (
    ApiModel,
    Confidence,
    NonNegativeInt,
    UtcDatetime,
    drop_unknown_enum_keys,
    lenient_enum,
    logger,
)
from ag_shared.api.heartbeat import CameraHeartbeat, WireCameraStatus, WireEngineStatus, WireState
from ag_shared.states import AttentionState, ScanOutcome

MAX_BATCH_ITEMS: Final = 200
MAX_BATCH_BYTES: Final = 1_000_000


class EventType(StrEnum):
    """Known batch item types."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    SCAN = "scan"
    ATTENTION_EVENT = "attention_event"
    BUCKET = "bucket"
    DISPUTE = "dispute"
    HEALTH = "health"
    LOCAL_ALERT = "local_alert"
    LOGIN_FAILURE = "login_failure"
    """Added in schema revision 2 (owner decision 2026-11-21)."""


INGEST_ORDER: Final[tuple[EventType, ...]] = (
    EventType.SESSION_START,
    EventType.SCAN,
    EventType.ATTENTION_EVENT,
    EventType.SESSION_END,
    EventType.BUCKET,
    EventType.DISPUTE,
    EventType.HEALTH,
    EventType.LOCAL_ALERT,
    EventType.LOGIN_FAILURE,
)
"""Dependency order for processing a batch (contract 5.3: session, scan,
event, the rest). Session end follows the scans and events of its session."""


class SessionMethod(StrEnum):
    """How a worker session started (`worker_session.method`)."""

    EMPLOYEE_ID_PIN = "employee_id_pin"
    SUPERVISOR_ASSIGNED = "supervisor_assigned"


class SessionEndReason(StrEnum):
    """Why a session ended (`worker_session.end_reason`)."""

    LOGOUT = "logout"
    TIMEOUT = "timeout"
    SHIFT_END = "shift_end"
    REPLACED = "replaced"
    ASSIGNMENT_EXPIRED = "assignment_expired"
    """A supervisor assignment reached its `valid_until` (revision 2)."""
    SUPERVISOR_ENDED = "supervisor_ended"
    """A supervisor ended the session from the console (`end_session` command; revision 2)."""


class AttentionEventKind(StrEnum):
    """`attention_event.kind`."""

    SCAN_WARNING = "scan_warning"
    STATE_EPISODE = "state_episode"


class ImagesWithheld(StrEnum):
    """Why an event has no (or fewer) images (`attention_event.images_withheld`)."""

    NONE = "none"
    NO_CONSENT = "no_consent"
    NO_WORKER = "no_worker"
    ON_BREAK = "on_break"
    """The frames fall in a scheduled break or the engine reports On Break (revision 2)."""
    DISK_FULL = "disk_full"


class LocalAlertKind(StrEnum):
    """Alerts the workstation raises itself; the server maps them to `alert.kind`."""

    CAMERA_OFFLINE = "camera_offline"
    MONITORING_UNAVAILABLE = "monitoring_unavailable"
    ENGINE_RESTART = "engine_restart"
    DISK_FULL = "disk_full"
    CLOCK_DRIFT = "clock_drift"
    OTHER = "other"
    """Default for kinds this side does not know."""


class DisputeReasonCode(StrEnum):
    """Optional reason code on a dispute (`dispute.reason`, revision 2).

    The values are the fixed choices of the workstation's "My record" screen,
    so false-positive statistics can be counted by reason (scope 6)."""

    WAS_ATTENTIVE = "was_attentive"
    """The worker says they were paying attention."""
    CAMERA_VIEW = "camera_view"
    """The camera could not see the worker properly."""
    ON_BREAK = "on_break"
    """The worker was on a break."""
    OTHER_TASK = "other_task"
    """The worker was doing another part of the job."""
    OTHER = "other"
    """Default for codes this side does not know."""


# Safe defaults for unknown enum values (contract section 2). Session method
# and end reason only label a record, so any member is harmless; an unknown
# scan outcome becomes `unmonitored` (no warning is implied); an unknown
# withheld reason becomes `none` because `image_ids` stays authoritative; an
# unknown dispute reason becomes `other` (the dispute itself is never lost).
WireSessionMethod = Annotated[
    SessionMethod, BeforeValidator(lenient_enum(SessionMethod, SessionMethod.EMPLOYEE_ID_PIN))
]
WireEndReason = Annotated[
    SessionEndReason, BeforeValidator(lenient_enum(SessionEndReason, SessionEndReason.LOGOUT))
]
WireOutcome = Annotated[
    ScanOutcome, BeforeValidator(lenient_enum(ScanOutcome, ScanOutcome.UNMONITORED))
]
WireEventKind = Annotated[
    AttentionEventKind,
    BeforeValidator(lenient_enum(AttentionEventKind, AttentionEventKind.STATE_EPISODE)),
]
WireImagesWithheld = Annotated[
    ImagesWithheld, BeforeValidator(lenient_enum(ImagesWithheld, ImagesWithheld.NONE))
]
WireLocalAlertKind = Annotated[
    LocalAlertKind, BeforeValidator(lenient_enum(LocalAlertKind, LocalAlertKind.OTHER))
]
WireDisputeReason = Annotated[
    DisputeReasonCode, BeforeValidator(lenient_enum(DisputeReasonCode, DisputeReasonCode.OTHER))
]

SignalSnapshot = dict[Annotated[str, Field(max_length=64)], float | None]
"""Signal name -> value at the event (None = invalid). Keys are strings so a
newer engine's signals pass through."""


class CameraStateSnapshot(ApiModel):
    """One camera's view at an event (`attention_event.camera_states`)."""

    slot: Annotated[int, Field(ge=1, le=4)]
    status: WireCameraStatus
    state: WireState
    quality: Confidence | None = None
    usable: bool = False


class SessionStartPayload(ApiModel):
    """The item `id` is the session id; `occurred_at` is `started_at`."""

    user_id: UUID
    method: WireSessionMethod
    station_assignment_id: UUID | None = None
    ppe_profile_id: UUID | None = None
    """The worker's PPE override in force, if any (additive, informational)."""

    @model_validator(mode="after")
    def _assignment_matches_method(self) -> Self:
        if self.method is SessionMethod.SUPERVISOR_ASSIGNED and self.station_assignment_id is None:
            raise ValueError("a supervisor_assigned session needs station_assignment_id")
        return self


class SessionEndPayload(ApiModel):
    """`occurred_at` is `ended_at`."""

    session_id: UUID
    end_reason: WireEndReason


class ScanPayload(ApiModel):
    """A scan. `occurred_at` is `scanned_at`. No session: an unattributed scan."""

    session_id: UUID | None = None
    user_id: UUID | None = None
    barcode: Annotated[str, Field(max_length=4096)]
    """Generous limit: a scan must never fail validation (2D codes run to ~3 KB)."""
    state_at_scan: WireState
    confidence: Confidence | None = None
    outcome: WireOutcome


class AttentionEventPayload(ApiModel):
    """A scan warning or a state episode."""

    kind: WireEventKind
    state: WireState
    confidence: Confidence | None = None
    low_confidence: bool = False
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    scan_id: UUID | None = None
    session_id: UUID | None = None
    user_id: UUID | None = None
    signals: SignalSnapshot = Field(default_factory=dict)
    camera_states: tuple[CameraStateSnapshot, ...] = ()
    engine_version: Annotated[str, Field(max_length=64)] = ""
    config_version: NonNegativeInt = 0
    images_withheld: WireImagesWithheld = ImagesWithheld.NONE
    image_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind is AttentionEventKind.SCAN_WARNING:
            if self.scan_id is None:
                raise ValueError("a scan_warning needs scan_id")
            if self.ended_at is not None:
                raise ValueError("a scan_warning has no ended_at")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        return self


class BucketPayload(ApiModel):
    """Seconds per state in one closed minute."""

    bucket_start: UtcDatetime
    user_id: UUID | None = None
    seconds: Annotated[
        dict[AttentionState, Annotated[float, Field(ge=0, le=60, allow_inf_nan=False)]],
        BeforeValidator(drop_unknown_enum_keys(AttentionState)),
    ]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.bucket_start.second or self.bucket_start.microsecond:
            raise ValueError("bucket_start must be on a whole minute")
        # Small slack for float rounding of per-frame accumulation.
        if sum(self.seconds.values()) > 60.001:
            raise ValueError("a bucket holds at most 60 seconds")
        return self


class DisputePayload(ApiModel):
    """A worker's dispute of one of their events."""

    attention_event_id: UUID
    user_id: UUID
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    reason: WireDisputeReason | None = None
    """Optional reason code (revision 2); `message` stays the text reviewers read."""


class HealthPayload(ApiModel):
    """One-minute health sample (`device_health`)."""

    cpu_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None
    mem_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None
    disk_free_mb: NonNegativeInt | None = None
    cameras: tuple[CameraHeartbeat, ...] = ()
    engine_status: WireEngineStatus
    engine_restarts: NonNegativeInt = 0
    queue_depth: NonNegativeInt = 0
    dead_letter_count: NonNegativeInt = 0
    clock_offset_ms: int = 0
    app_version: Annotated[str, Field(max_length=64)] = ""


class LocalAlertPayload(ApiModel):
    """A locally detected problem."""

    kind: WireLocalAlertKind
    detail: dict[str, JsonValue] = Field(default_factory=dict)


class LoginFailurePayload(ApiModel):
    """A failed worker login on the station (revision 2), for the server audit log.

    Never carries what was typed: an unknown employee ID is often a PIN typed
    into the ID field. `occurred_at` is the time of the attempt."""

    method: Annotated[str, Field(min_length=1, max_length=64)]
    """The login method's name (`employee_id_pin`); free text because login
    methods are plug-ins and a failure with a newer method must still be audited."""
    user_id: UUID | None = None
    """The roster user the typed ID matched (active or not), else null."""
    known_worker: bool
    """Whether the typed ID matched a roster user."""
    locked: bool = False
    """This failure locked the ID on this station (security-privacy 3 audits
    lockouts). Additive field beyond the contract's main fields."""


class _ItemBase(ApiModel):
    id: UUID
    """Client-generated UUIDv7; the server-side primary key."""
    seq: NonNegativeInt
    occurred_at: UtcDatetime


class SessionStartItem(_ItemBase):
    """`session_start` item."""

    type: Literal["session_start"] = "session_start"
    payload: SessionStartPayload


class SessionEndItem(_ItemBase):
    """`session_end` item."""

    type: Literal["session_end"] = "session_end"
    payload: SessionEndPayload


class ScanItem(_ItemBase):
    """`scan` item."""

    type: Literal["scan"] = "scan"
    payload: ScanPayload


class AttentionEventItem(_ItemBase):
    """`attention_event` item."""

    type: Literal["attention_event"] = "attention_event"
    payload: AttentionEventPayload


class BucketItem(_ItemBase):
    """`bucket` item."""

    type: Literal["bucket"] = "bucket"
    payload: BucketPayload


class DisputeItem(_ItemBase):
    """`dispute` item."""

    type: Literal["dispute"] = "dispute"
    payload: DisputePayload


class HealthItem(_ItemBase):
    """`health` item."""

    type: Literal["health"] = "health"
    payload: HealthPayload


class LocalAlertItem(_ItemBase):
    """`local_alert` item."""

    type: Literal["local_alert"] = "local_alert"
    payload: LocalAlertPayload


class LoginFailureItem(_ItemBase):
    """`login_failure` item."""

    type: Literal["login_failure"] = "login_failure"
    payload: LoginFailurePayload


class UnknownEventItem(_ItemBase):
    """An item type this side does not know; the server answers `rejected`."""

    type: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _log(self) -> Self:
        logger.warning("unknown event item type %r (id %s)", self.type[:40], self.id)
        return self


_KNOWN_TYPES = frozenset(EventType)


def _item_tag(value: object) -> str:
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    if isinstance(kind, str) and kind in _KNOWN_TYPES and not isinstance(value, UnknownEventItem):
        return kind
    return "unknown"


EventItem = Annotated[
    Annotated[SessionStartItem, Tag("session_start")]
    | Annotated[SessionEndItem, Tag("session_end")]
    | Annotated[ScanItem, Tag("scan")]
    | Annotated[AttentionEventItem, Tag("attention_event")]
    | Annotated[BucketItem, Tag("bucket")]
    | Annotated[DisputeItem, Tag("dispute")]
    | Annotated[HealthItem, Tag("health")]
    | Annotated[LocalAlertItem, Tag("local_alert")]
    | Annotated[LoginFailureItem, Tag("login_failure")]
    | Annotated[UnknownEventItem, Tag("unknown")],
    Discriminator(_item_tag),
]

EVENT_ITEM_ADAPTER: TypeAdapter[EventItem] = TypeAdapter(EventItem)


def parse_event_item(raw: object) -> EventItem:
    """Validate one item; raises `pydantic.ValidationError` if it is invalid."""
    return EVENT_ITEM_ADAPTER.validate_python(raw)


class EventBatch(ApiModel):
    """`POST /events:batch` body, typed. A retry resends the same `batch_id`."""

    batch_id: UUID
    sent_at: UtcDatetime
    items: Annotated[tuple[EventItem, ...], Field(max_length=MAX_BATCH_ITEMS)]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("item ids must be unique within a batch")
        return self


class RawEventBatch(ApiModel):
    """The batch envelope with unvalidated items (server side)."""

    batch_id: UUID
    sent_at: UtcDatetime
    items: Annotated[tuple[dict[str, JsonValue], ...], Field(max_length=MAX_BATCH_ITEMS)]


class ItemStatus(StrEnum):
    """Per-item ingest result."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    RETRY_LATER = "retry_later"

    @property
    def deletes_record(self) -> bool:
        """Whether the client may delete the queue record."""
        return self in (ItemStatus.ACCEPTED, ItemStatus.DUPLICATE)


# Unknown status: keep the record and retry (never lose data on a guess).
WireItemStatus = Annotated[
    ItemStatus, BeforeValidator(lenient_enum(ItemStatus, ItemStatus.RETRY_LATER))
]


class ItemResult(ApiModel):
    """The result for one item."""

    id: UUID
    status: WireItemStatus
    reason: Annotated[str, Field(max_length=500)] | None = None


class BatchResult(ApiModel):
    """`200` response of `POST /events:batch`."""

    batch_id: UUID
    server_time: UtcDatetime
    results: tuple[ItemResult, ...] = ()
