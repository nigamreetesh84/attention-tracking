"""What runs in the engine child next to the real engine (task 2.9-pre).

Process placement (recorded in PROGRESS.md, 2026-11-21, 2.9-pre):

- UI process: scan flow, scan journal, login and sessions. A scan is always
  recorded there, even with the service or the engine child dead (rule 1).
- Engine child: the engine, the evidence rings and burst assembly, and the
  capture gate in front of the evidence store, so the gate is enforced in the
  process that holds and writes the frames (rule 5). It also turns engine
  episodes and buckets into queue records (queue opened enqueue-only).
- Service parent: supervisor, housekeeping, and (task 2.9) the sync client,
  the queue's single sender.

The child learns the open session only from the UI (`set_session`, pushed on
login/logout and every second). A pushed session expires after its TTL, so
a dead or silent UI closes the gate within seconds, and a restarted child
starts with no session: gate closed until the next push. The push carries
session id, user id, start time and the consent flag, never names, employee
IDs or PINs.
"""

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol, runtime_checkable
from uuid import UUID

from ag_shared.api import (
    AttentionEventKind,
    AttentionEventPayload,
    BucketPayload,
    EventType,
    ImagesWithheld,
)
from ag_shared.config import EngineConfig
from ag_shared.states import AttentionState

from ag_workstation.clock import Clock
from ag_workstation.consent.sink import CaptureOutcome
from ag_workstation.engine.adapter import KEYFRAME_STATES, EngineAdapter
from ag_workstation.engine_api import (
    BurstKind,
    ConfigApplyResult,
    EngineEventListener,
    EngineHealth,
    StateBucket,
    StateEpisode,
    StationState,
)
from ag_workstation.scan.journal import ItemQueue

logger = logging.getLogger(__name__)

MAX_SESSION_TTL_S: Final = 60.0
DEFAULT_SESSION_TTL_S: Final = 5.0
OUTCOME_CAPACITY: Final = 512
OUTCOME_TTL_S: Final = 300.0
CAPTURE_FAILED: Final = ImagesWithheld.DISK_FULL
"""As in the scan flow: images expected but not stored."""
KEYFRAME_WAIT_S: Final = 5.0
"""How long a closed Sleeping/Absent episode waits for its key frame outcome."""


@dataclass(frozen=True, slots=True)
class PushedSession:
    """The open session as the UI pushes it (`consent.GateSession`)."""

    session_id: UUID
    user_id: UUID
    started_at: datetime
    has_consent: bool


class PushedSessions:
    """`SessionSource` for the engine child: the last push, until its TTL runs out."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._session: PushedSession | None = None
        self._expires = 0.0

    def set(self, session: PushedSession | None, ttl_s: float) -> bool:
        """Store a push; True if the session id changed (a new worker or logout)."""
        ttl = min(max(ttl_s, 0.0), MAX_SESSION_TTL_S)
        with self._lock:
            previous = self._live()
            self._session = session
            self._expires = self._clock.monotonic() + ttl
            before = previous.session_id if previous is not None else None
            after = session.session_id if session is not None else None
            return before != after

    def _live(self) -> PushedSession | None:
        if self._session is None or self._clock.monotonic() > self._expires:
            return None
        return self._session

    def current(self) -> PushedSession | None:
        """The pushed session if it has not expired."""
        with self._lock:
            return self._live()


BurstStatusKind = Literal["pending", "done", "unknown"]


@dataclass(frozen=True, slots=True)
class BurstStatus:
    """The answer to `burst_outcome`."""

    status: BurstStatusKind
    outcome: CaptureOutcome | None = None


class OutcomeRegistry:
    """Capture outcomes by event id, bounded in size and age. Thread-safe."""

    def __init__(self, clock: Clock, *, capacity: int = OUTCOME_CAPACITY) -> None:
        self._clock = clock
        self._capacity = capacity
        self._lock = threading.Lock()
        self._requested: OrderedDict[UUID, float] = OrderedDict()
        self._done: OrderedDict[UUID, tuple[float, CaptureOutcome]] = OrderedDict()

    def _trim(self) -> None:
        horizon = self._clock.monotonic() - OUTCOME_TTL_S
        while self._requested and (
            len(self._requested) > self._capacity or next(iter(self._requested.values())) < horizon
        ):
            self._requested.popitem(last=False)
        while self._done and (
            len(self._done) > self._capacity or next(iter(self._done.values()))[0] < horizon
        ):
            self._done.popitem(last=False)

    def requested(self, event_id: UUID) -> None:
        """A burst was asked for (status `pending` until its outcome)."""
        with self._lock:
            self._requested[event_id] = self._clock.monotonic()
            self._trim()

    def record(self, outcome: CaptureOutcome) -> None:
        """`GatedEvidenceSink(on_outcome=...)`."""
        with self._lock:
            self._requested.pop(outcome.event_id, None)
            self._done[outcome.event_id] = (self._clock.monotonic(), outcome)
            self._trim()

    def status(self, event_id: UUID) -> BurstStatus:
        """Done (with the outcome), pending, or unknown."""
        with self._lock:
            done = self._done.get(event_id)
            if done is not None:
                return BurstStatus("done", done[1])
            if event_id in self._requested:
                return BurstStatus("pending")
            return BurstStatus("unknown")


class FanOut:
    """An `EngineEventListener` calling several listeners; one failing does not stop the rest."""

    def __init__(self, *listeners: EngineEventListener) -> None:
        self._listeners = listeners

    def _each(self, call: Callable[[EngineEventListener], None]) -> None:
        for listener in self._listeners:
            try:
                call(listener)
            except Exception as exc:
                logger.error("engine event listener error (%s)", type(exc).__name__)

    def episode_opened(self, episode: StateEpisode) -> None:
        """Fan out."""
        self._each(lambda listener: listener.episode_opened(episode))

    def episode_closed(self, episode: StateEpisode) -> None:
        """Fan out."""
        self._each(lambda listener: listener.episode_closed(episode))

    def bucket_closed(self, bucket: StateBucket) -> None:
        """Fan out."""
        self._each(lambda listener: listener.bucket_closed(bucket))


class EngineEventRecorder:
    """Turns engine episodes and buckets into queue records (`EngineEventListener`).

    - An episode is attributed to the session open when it started; episodes
      that start with no session (an empty station) are not recorded.
    - It is queued once, when it closes, as a `state_episode` attention event
      (queue records cannot be updated). Sleeping and Absent carry the key
      frame the adapter requested at open, or the reason it was withheld; if
      that outcome is not in yet, the record waits for it (`outcome_arrived`)
      up to `KEYFRAME_WAIT_S`, checked on every later event and on `flush`.
    - A bucket is queued when its minute closes, with the user of the session
      open at that time (None when nobody is logged in). Splitting a minute
      at a session change is not done (1.7 note).

    A queue failure loses that record only and is counted.
    """

    def __init__(
        self,
        queue: ItemQueue | None,
        sessions: PushedSessions,
        outcomes: OutcomeRegistry,
        clock: Clock,
    ) -> None:
        self._queue = queue
        self._sessions = sessions
        self._outcomes = outcomes
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[UUID, PushedSession] = {}
        self._waiting: dict[UUID, tuple[float, StateEpisode, PushedSession]] = {}
        self.recorded = 0
        self.lost = 0
        self.unattributed_episodes = 0

    def episode_opened(self, episode: StateEpisode) -> None:
        """Remember the session the episode belongs to."""
        self._expire()
        session = self._sessions.current()
        with self._lock:
            if session is None:
                self.unattributed_episodes += 1
                return
            self._open[episode.id] = session
            while len(self._open) > OUTCOME_CAPACITY:
                self._open.pop(next(iter(self._open)))

    def episode_closed(self, episode: StateEpisode) -> None:
        """Queue the finished episode (or wait briefly for its key frame outcome)."""
        self._expire()
        with self._lock:
            session = self._open.pop(episode.id, None)
        if session is None:
            return
        if episode.state not in KEYFRAME_STATES:
            self._emit(episode, session, (), ImagesWithheld.NONE)
            return
        outcome = self._outcomes.status(episode.id).outcome
        if outcome is not None:
            self._emit(episode, session, outcome.image_ids, outcome.images_withheld)
            return
        with self._lock:
            deadline = self._clock.monotonic() + KEYFRAME_WAIT_S
            self._waiting[episode.id] = (deadline, episode, session)

    def outcome_arrived(self, outcome: CaptureOutcome) -> None:
        """A capture outcome: queue the closed episode that waited for it."""
        with self._lock:
            waiting = self._waiting.pop(outcome.event_id, None)
        if waiting is not None:
            _, episode, session = waiting
            self._emit(episode, session, outcome.image_ids, outcome.images_withheld)

    def _expire(self) -> None:
        now = self._clock.monotonic()
        with self._lock:
            due = [k for k, (deadline, _, _) in self._waiting.items() if deadline <= now]
            expired = [self._waiting.pop(k) for k in due]
        for _, episode, session in expired:
            self._emit(episode, session, (), CAPTURE_FAILED)

    def flush(self) -> None:
        """Queue every waiting episode now (shutdown); missing key frames count as failed."""
        with self._lock:
            waiting, self._waiting = list(self._waiting.values()), {}
        for _, episode, session in waiting:
            self._emit(episode, session, (), CAPTURE_FAILED)

    def _emit(
        self,
        episode: StateEpisode,
        session: PushedSession,
        image_ids: tuple[UUID, ...],
        withheld: ImagesWithheld,
    ) -> None:
        payload = AttentionEventPayload(
            kind=AttentionEventKind.STATE_EPISODE,
            state=episode.state,
            confidence=episode.confidence,
            low_confidence=episode.low_confidence,
            started_at=episode.started_at,
            ended_at=episode.ended_at,
            session_id=session.session_id,
            user_id=session.user_id,
            signals=dict(episode.signals),
            camera_states=tuple(c.to_snapshot() for c in episode.camera_states),
            engine_version=episode.engine_version[:64],
            config_version=episode.config_version,
            images_withheld=withheld,
            image_ids=image_ids,
        )
        self._enqueue(EventType.ATTENTION_EVENT, payload, episode.started_at, episode.id)

    def bucket_closed(self, bucket: StateBucket) -> None:
        """Queue the closed minute."""
        self._expire()
        session = self._sessions.current()
        payload = BucketPayload(
            bucket_start=bucket.bucket_start,
            user_id=session.user_id if session is not None else None,
            seconds={AttentionState(k): v for k, v in bucket.seconds.items()},
        )
        self._enqueue(EventType.BUCKET, payload, bucket.bucket_start + timedelta(minutes=1), None)

    def _enqueue(
        self,
        item_type: EventType,
        payload: AttentionEventPayload | BucketPayload,
        occurred_at: datetime,
        record_id: UUID | None,
    ) -> None:
        if self._queue is None:
            self.lost += 1
            logger.warning("%s not recorded: no local queue in the engine child", item_type.value)
            return
        try:
            self._queue.enqueue(item_type, payload, occurred_at, record_id=record_id)
            self.recorded += 1
        except Exception as exc:
            self.lost += 1
            logger.error("%s not queued (%s)", item_type.value, type(exc).__name__)


@runtime_checkable
class StationControl(Protocol):
    """The control operations the engine child serves over IPC."""

    def set_session(self, session: PushedSession | None, ttl_s: float) -> None:
        """Replace the pushed session."""
        ...

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        """Apply an engine config; never raises."""
        ...

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        """Start a burst; never raises."""
        ...

    def burst_outcome(self, event_id: UUID) -> BurstStatus:
        """What happened to a requested burst."""
        ...


class StationChild:
    """The engine child's handle: state, health and control. Never raises."""

    def __init__(
        self,
        adapter: EngineAdapter,
        sessions: PushedSessions,
        outcomes: OutcomeRegistry,
        *,
        recorder: EngineEventRecorder | None = None,
        closers: tuple[Callable[[], None], ...] = (),
    ) -> None:
        self.adapter = adapter
        self.sessions = sessions
        self.outcomes = outcomes
        self.recorder = recorder
        self._closers = closers

    def scan_state(self) -> StationState:
        """The scan decision."""
        return self.adapter.scan_state()

    def current_state(self) -> StationState:
        """The current state."""
        return self.adapter.current_state()

    def health(self) -> EngineHealth:
        """Engine health."""
        return self.adapter.health()

    def set_session(self, session: PushedSession | None, ttl_s: float) -> None:
        """Replace the pushed session; a new session resets per-person baselines."""
        if self.sessions.set(session, ttl_s) and session is not None:
            self.adapter.reset_session()

    def apply_config(self, config: EngineConfig) -> ConfigApplyResult:
        """Apply an engine config (station profile or a worker's PPE override)."""
        return self.adapter.apply_config(config)

    def request_burst(self, event_id: UUID, kind: BurstKind, trigger_ts: float) -> None:
        """Start a burst; its outcome is then available from `burst_outcome`."""
        self.outcomes.requested(event_id)
        self.adapter.request_burst(event_id, kind, trigger_ts)

    def burst_outcome(self, event_id: UUID) -> BurstStatus:
        """Pending, done (with image ids or the withheld reason), or unknown."""
        return self.outcomes.status(event_id)

    def close(self) -> None:
        """Stop the engine (flushing bursts and events), then close storage.

        Closers run first-registered-last; the evidence sink (registered
        last) drains its bursts before the recorder's waiting episodes are
        queued and the queue closes."""
        self.adapter.stop()
        for close in reversed(self._closers):
            try:
                close()
            except Exception as exc:
                logger.warning("close failed (%s)", type(exc).__name__)
