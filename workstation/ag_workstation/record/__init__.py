"""The worker's own record on the workstation and disputes (PLAN 2.10; scope 7; D8).

- `PersonalRecordStore`: local, sealed, per-session counters and events
  (no images, no barcodes), kept for a short window, read per worker.
- `RecordMirror`: copies queued items into it without touching the scan path.
- `DisputeService`: queues one `dispute` item per event, exactly once.
"""

from ag_workstation.record.disputes import (
    MAX_MESSAGE_CHARS,
    DisputeOutcome,
    DisputeResult,
    DisputeService,
)
from ag_workstation.record.mirror import RecordMirror, RecordWriter, apply_copy
from ag_workstation.record.store import (
    DEFAULT_RETENTION,
    RECORD_FILE,
    DisputeRecord,
    DisputeStatus,
    PersonalRecordStore,
    RecordEvent,
    RecordSnapshot,
    SessionSummary,
)

__all__ = [
    "DEFAULT_RETENTION",
    "MAX_MESSAGE_CHARS",
    "RECORD_FILE",
    "DisputeOutcome",
    "DisputeRecord",
    "DisputeResult",
    "DisputeService",
    "DisputeStatus",
    "PersonalRecordStore",
    "RecordEvent",
    "RecordMirror",
    "RecordSnapshot",
    "RecordWriter",
    "SessionSummary",
    "apply_copy",
]
