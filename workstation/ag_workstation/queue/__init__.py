"""Encrypted local event queue (PLAN 2.1, `api-contract.md` section 6)."""

from ag_workstation.queue.backoff import MAX_BACKOFF_S, MIN_BACKOFF_S, backoff_delay
from ag_workstation.queue.crypto import NONCE_BYTES, PayloadCipher, PayloadIntegrityError
from ag_workstation.queue.keys import (
    DATA_KEY_BYTES,
    DpapiKeyProvider,
    InMemoryKeyProvider,
    KeyProvider,
    KeyUnavailableError,
)
from ag_workstation.queue.store import (
    DEAD_LETTER_RETENTION,
    ApplySummary,
    ClaimedRecord,
    DeadLetter,
    DeadLetterReason,
    EventQueue,
    QueuedRecord,
    QueueStats,
    record_associated_data,
)

__all__ = [
    "DATA_KEY_BYTES",
    "DEAD_LETTER_RETENTION",
    "MAX_BACKOFF_S",
    "MIN_BACKOFF_S",
    "NONCE_BYTES",
    "ApplySummary",
    "ClaimedRecord",
    "DeadLetter",
    "DeadLetterReason",
    "DpapiKeyProvider",
    "EventQueue",
    "InMemoryKeyProvider",
    "KeyProvider",
    "KeyUnavailableError",
    "PayloadCipher",
    "PayloadIntegrityError",
    "QueueStats",
    "QueuedRecord",
    "backoff_delay",
    "record_associated_data",
]
