"""Where the evidence store reports a `disk_full` episode.

The store never holds a queue handle: its only link to the event queue is
`QueueDiskFullAlert`, which can do nothing but enqueue a `local_alert`. That
is how "metadata is never dropped to free space" is kept by construction.
"""

from typing import Protocol

from ag_shared.api import EventType, LocalAlertKind, LocalAlertPayload
from pydantic import JsonValue

from ag_workstation.clock import Clock
from ag_workstation.queue.store import EventQueue


class DiskFullAlertSink(Protocol):
    """Receives one call per disk-full episode."""

    def disk_full(self, detail: dict[str, JsonValue]) -> None:
        """Raise the alert. May raise; the store logs it and retries on the next loss."""
        ...


class QueueDiskFullAlert:
    """Queues a `local_alert` of kind `disk_full`; nothing else touches the queue."""

    def __init__(self, queue: EventQueue, clock: Clock) -> None:
        self._enqueue = queue.enqueue
        self._clock = clock

    def disk_full(self, detail: dict[str, JsonValue]) -> None:
        """Enqueue the alert record."""
        self._enqueue(
            EventType.LOCAL_ALERT,
            LocalAlertPayload(kind=LocalAlertKind.DISK_FULL, detail=detail),
            self._clock.now(),
        )
