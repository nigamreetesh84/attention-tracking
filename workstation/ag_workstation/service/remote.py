"""UI-process side of the engine child's control operations (task 2.9-pre).

The scan flow and the session manager run in the UI process; the engine,
the frames and the capture gate run in the engine child. These adapters
let the UI-side code keep its interfaces:

- `RemoteEvidence` is the scan flow's `EvidenceRequester`. `request_burst`
  only queues the request (it is called on the scan path after the scan is
  durable) and returns at once; a worker thread sends it and polls
  `burst_outcome` until the outcome arrives or `wait_s` runs out, then
  hands the `CaptureOutcome` to the scan flow. A lost request or outcome is
  covered by the scan flow's own capture wait (5 s, images withheld).
- `SessionPusher` pushes the open session (ids, start, consent) to the child
  on demand and every `interval_s`; the child forgets it after `ttl_s`.
- The `IpcClient` itself is the `EngineConfigTarget` (`apply_config`).

Use a separate `IpcClient` for these than for the scan-state queries: the
client serializes calls, and a slow control call must not cost a scan query
its 150 ms.
"""

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from ag_workstation.consent.gate import GateSession, SessionSource
from ag_workstation.consent.sink import CaptureOutcome
from ag_workstation.engine_api import BurstKind
from ag_workstation.service.ipc_client import CallStatus
from ag_workstation.service.station import DEFAULT_SESSION_TTL_S, BurstStatus

logger = logging.getLogger(__name__)

POLL_S: Final = 0.2
WAIT_S: Final = 4.5
"""Below the scan flow's 5 s capture wait, so a late outcome is not wasted."""
MAX_QUEUED: Final = 32
PUSH_INTERVAL_S: Final = 1.0


class ControlClient(Protocol):
    """The `IpcClient` control calls these adapters use."""

    def send_burst_request(
        self, event_id: UUID, kind: BurstKind, trigger_ts: float, *, timeout_s: float | None = None
    ) -> CallStatus:
        """Start a burst in the child."""
        ...

    def burst_outcome(
        self, event_id: UUID, *, timeout_s: float | None = None
    ) -> BurstStatus | None:
        """Poll a burst."""
        ...


class SessionPushClient(Protocol):
    """The `IpcClient` session push."""

    def push_session(
        self, session: GateSession | None, ttl_s: float, *, timeout_s: float | None = None
    ) -> CallStatus:
        """Push the open session."""
        ...


@dataclass(slots=True)
class _InFlight:
    event_id: UUID
    deadline: float


OutcomeHandler = Callable[[CaptureOutcome], None]


class RemoteEvidence:
    """`EvidenceRequester` over IPC; outcomes go to `on_outcome`. Never raises."""

    def __init__(
        self,
        client: ControlClient,
        *,
        monotonic: Callable[[], float],
        on_outcome: OutcomeHandler | None = None,
        poll_s: float = POLL_S,
        wait_s: float = WAIT_S,
        max_queued: int = MAX_QUEUED,
    ) -> None:
        self._client = client
        self._mono = monotonic
        self._on_outcome = on_outcome
        self._poll_s = poll_s
        self._wait_s = wait_s
        self._requests: queue.Queue[tuple[UUID, BurstKind, float]] = queue.Queue(max_queued)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped = 0
        self.failed = 0
        self.expired = 0
        self.delivered = 0

    def bind(self, on_outcome: OutcomeHandler) -> None:
        """Set where outcomes go (the scan flow is built after its requester)."""
        self._on_outcome = on_outcome

    def start(self) -> None:
        """Start the worker thread."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="remote-evidence", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 1.0) -> None:
        """Stop the worker."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        """Queue the request; never blocks, never raises."""
        try:
            self._requests.put_nowait((event_id, kind, trigger_ts))
        except queue.Full:
            self.dropped += 1
            logger.warning("burst request dropped: too many waiting")

    def _run(self) -> None:
        in_flight: list[_InFlight] = []
        while not self._stop.is_set():
            try:
                self.step(in_flight, block_s=self._poll_s)
            except Exception as exc:  # the worker must outlive any bug
                logger.error("remote evidence worker error (%s)", type(exc).__name__)

    def step(self, in_flight: list[_InFlight], *, block_s: float = 0.0) -> None:
        """Send new requests, poll waiting ones (one round; tests call it directly)."""
        try:
            first = self._requests.get(timeout=block_s) if block_s > 0 else None
        except queue.Empty:
            first = None
        batch = [first] if first is not None else []
        while True:
            try:
                batch.append(self._requests.get_nowait())
            except queue.Empty:
                break
        for event_id, kind, trigger_ts in batch:
            status = self._client.send_burst_request(event_id, kind, trigger_ts)
            if status is CallStatus.OK:
                in_flight.append(_InFlight(event_id, self._mono() + self._wait_s))
            else:
                self.failed += 1
                logger.warning("burst request not delivered (%s)", status.value)
        for item in list(in_flight):
            answer = self._client.burst_outcome(item.event_id)
            if answer is not None and answer.outcome is not None:
                in_flight.remove(item)
                self._deliver(answer.outcome)
            elif (
                answer is not None and answer.status == "unknown"
            ) or self._mono() > item.deadline:
                in_flight.remove(item)
                self.expired += 1

    def _deliver(self, outcome: CaptureOutcome) -> None:
        handler = self._on_outcome
        if handler is None:
            return
        try:
            handler(outcome)
            self.delivered += 1
        except Exception as exc:
            logger.error("capture outcome handler failed (%s)", type(exc).__name__)


class SessionPusher:
    """Pushes the open session to the engine child, now and periodically."""

    def __init__(
        self,
        client: SessionPushClient,
        sessions: SessionSource,
        *,
        interval_s: float = PUSH_INTERVAL_S,
        ttl_s: float = DEFAULT_SESSION_TTL_S,
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._interval = interval_s
        self._ttl = ttl_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.pushes = 0
        self.failures = 0

    def start(self) -> None:
        """Start pushing."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="session-push", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 1.0) -> None:
        """Stop pushing (the child's copy then expires after its TTL)."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def push_now(self) -> None:
        """Push at once (after login or logout); returns without waiting."""
        self._wake.set()

    def push(self) -> CallStatus:
        """One push (tests call it directly). Never raises."""
        try:
            session = self._sessions.current()
            status = self._client.push_session(session, self._ttl)
        except Exception as exc:
            logger.error("session push failed (%s)", type(exc).__name__)
            status = CallStatus.ERROR
        if status is CallStatus.OK:
            self.pushes += 1
        else:
            self.failures += 1
        return status

    def _run(self) -> None:
        while not self._stop.is_set():
            self.push()
            self._wake.wait(self._interval)
            self._wake.clear()
