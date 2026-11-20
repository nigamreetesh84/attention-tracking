"""Station state machine (`docs/detection-engine.md` section 5).

Takes the fused state of every frame cycle and produces the station state,
`state_episode` events, per-minute buckets and the scan-time decision.

- Severity order (most to least): Sleeping, Absent, Drowsy, Distracted,
  Attentive. Entry timers live in the per-camera estimator, so a fused state
  is already "timer met" when it arrives here.
- A more severe state replaces a less severe one immediately. A less severe
  state replaces a more severe one only after `recovery_seconds` of
  uninterrupted less severe candidates, so the state does not flicker.
- Unknown is taken immediately (its timer already ran). Leaving Unknown is
  immediate too: there is nothing to recover from.
- On Break comes from the shift calendar and overrides everything; during a
  break no episodes are produced.
- Each entry into Distracted, Drowsy, Sleeping or Absent opens an episode;
  leaving it closes the episode.

`ts` is the engine's monotonic seconds (frame time in replay); `wall` is the
matching UTC time, used for breaks, episode times and buckets.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from uuid import UUID

from ag_shared.ids import uuid7
from ag_shared.states import (
    WARNED_STATES,
    AttentionState,
    ScanOutcome,
    is_more_severe,
    scan_outcome,
)
from ag_shared.thresholds import Thresholds

from ag_workstation.engine.timers import ConditionTimer
from ag_workstation.engine.types import CameraCandidate, FusedState

A = AttentionState

SCAN_WINDOW_MIN_SHARE = 0.40
"""Scan rule 2: a warned state must hold this share of the scan window."""

MAX_ATTRIBUTED_GAP_S = 2.0
"""Bucket time between two updates is credited to the previous state only up
to this long; the rest of a longer gap (engine stalled) counts as Unknown."""

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Episode:
    """A `state_episode` attention event (open while `ended_at` is None)."""

    id: UUID
    state: AttentionState
    started_at: datetime
    started_ts: float
    confidence: float
    low_confidence: bool
    candidates: tuple[CameraCandidate, ...] = field(compare=False)
    """Per-camera candidates when the episode opened (signals and camera states)."""
    ended_at: datetime | None = None
    ended_ts: float | None = None


@dataclass(frozen=True, slots=True)
class Bucket:
    """Seconds per state in one closed UTC minute."""

    bucket_start: datetime
    seconds: dict[AttentionState, float]


@dataclass(frozen=True, slots=True)
class ScanDecision:
    """The state a scan is recorded with and its outcome."""

    state: AttentionState
    confidence: float
    low_confidence: bool
    outcome: ScanOutcome


@dataclass(frozen=True, slots=True)
class StationStatus:
    """The current station state for the live wall and the scan screen."""

    state: AttentionState
    since_ts: float
    since: datetime | None
    confidence: float
    low_confidence: bool
    monitoring_unavailable: bool
    degraded_slots: tuple[int, ...]


@dataclass(slots=True)
class MachineOutput:
    """What one update produced."""

    status: StationStatus
    opened: list[Episode] = field(default_factory=list)
    closed: list[Episode] = field(default_factory=list)
    buckets: list[Bucket] = field(default_factory=list)


def _minute(at: datetime) -> datetime:
    return at.replace(second=0, microsecond=0)


class BucketAccumulator:
    """Splits state time into UTC minutes and emits each minute once it closes."""

    def __init__(self) -> None:
        self._minute: datetime | None = None
        self._seconds: dict[AttentionState, float] = {}

    def reset(self) -> None:
        """Drop the open minute without emitting it."""
        self._minute = None
        self._seconds = {}

    def add(self, start: datetime, end: datetime, state: AttentionState) -> list[Bucket]:
        """Credit [start, end) to `state`; return minutes that closed."""
        closed: list[Bucket] = []
        cursor = start
        while cursor < end:
            minute = _minute(cursor)
            if self._minute is not None and minute != self._minute:
                closed.extend(self.flush())
            self._minute = minute
            boundary = minute + timedelta(minutes=1)
            stop = min(end, boundary)
            seconds = (stop - cursor).total_seconds()
            self._seconds[state] = self._seconds.get(state, 0.0) + seconds
            cursor = stop
            if cursor == boundary:
                closed.extend(self.flush())
        return closed

    def flush(self) -> list[Bucket]:
        """Emit the open minute (even if incomplete) and start empty."""
        if self._minute is None or not self._seconds:
            self._minute = None
            self._seconds = {}
            return []
        bucket = Bucket(self._minute, {s: round(v, 3) for s, v in self._seconds.items()})
        self._minute = None
        self._seconds = {}
        return [bucket]


class StationStateMachine:
    """The station state over time. Call `update` once per fused state, in order."""

    def __init__(
        self,
        thresholds: Thresholds,
        *,
        id_factory: Callable[[], UUID] = uuid7,
    ) -> None:
        self._t = thresholds
        self._new_id = id_factory
        self._state = A.UNKNOWN
        self._since_ts = 0.0
        self._since: datetime | None = None
        self._confidence = 0.0
        self._low = False
        self._unavailable = False
        self._degraded: tuple[int, ...] = ()
        self._recovery = ConditionTimer()
        self._episode: Episode | None = None
        self._buckets = BucketAccumulator()
        self._last_ts: float | None = None
        self._last_wall: datetime | None = None
        # (ts, fused state, confidence, low_confidence) of each update in the scan window.
        self._history: deque[tuple[float, AttentionState, float, bool]] = deque()

    # --- configuration ---

    def configure(self, thresholds: Thresholds) -> None:
        """Use new thresholds from the next update on."""
        self._t = thresholds

    @property
    def open_episode(self) -> Episode | None:
        """The episode in progress, if any."""
        return self._episode

    @property
    def state(self) -> AttentionState:
        """The current station state."""
        return self._state

    # --- updates ---

    def update(self, fused: FusedState, wall: datetime, *, on_break: bool = False) -> MachineOutput:
        """Advance to `fused.ts` (UTC `wall`) and return what changed."""
        ts = fused.ts
        if self._last_ts is not None and ts < self._last_ts - _EPS:
            raise ValueError("updates must be in timestamp order")
        out_buckets = self._account(ts, wall)
        self._unavailable = fused.monitoring_unavailable
        self._degraded = fused.degraded_slots

        target = self._next_state(fused, on_break)
        opened: list[Episode] = []
        closed: list[Episode] = []
        if target is not self._state or self._since is None:
            closed.extend(self._close_episode(ts, wall))
            self._state = target
            self._since_ts = ts
            self._since = wall
            self._recovery.reset()
            self._confidence = fused.confidence if target is fused.state else 0.0
            self._low = fused.low_confidence if target is fused.state else False
            if target in WARNED_STATES:
                self._episode = Episode(
                    id=self._new_id(),
                    state=target,
                    started_at=wall,
                    started_ts=ts,
                    confidence=self._confidence,
                    low_confidence=self._low,
                    candidates=fused.candidates,
                )
                opened.append(self._episode)
        elif fused.state is self._state:
            self._confidence = fused.confidence
            self._low = fused.low_confidence
            if self._episode is not None:
                self._episode = replace(
                    self._episode,
                    confidence=max(self._episode.confidence, fused.confidence),
                    low_confidence=self._episode.low_confidence or fused.low_confidence,
                )

        window_state = A.ON_BREAK if on_break else fused.state
        self._remember(ts, window_state, fused.confidence, fused.low_confidence)
        return MachineOutput(
            status=self.status(), opened=opened, closed=closed, buckets=out_buckets
        )

    def _next_state(self, fused: FusedState, on_break: bool) -> AttentionState:
        t = self._t
        if on_break:
            return A.ON_BREAK
        candidate = fused.state
        current = self._state
        if candidate is A.UNKNOWN or current in (A.UNKNOWN, A.ON_BREAK) or self._since is None:
            return candidate
        if candidate is current:
            # A relapse restarts recovery: no gap tolerance here, or a brief
            # Attentive spell could end an episode only to reopen it at once.
            self._recovery.reset()
            return current
        if is_more_severe(candidate, current):
            return candidate
        # Less severe: only after recovery.
        self._recovery.update(fused.ts, True, t.gap_tolerance_s)
        if self._recovery.duration + _EPS >= t.recovery_seconds:
            return candidate
        return current

    def _close_episode(self, ts: float, wall: datetime) -> list[Episode]:
        if self._episode is None:
            return []
        done = replace(self._episode, ended_at=wall, ended_ts=ts)
        self._episode = None
        return [done]

    def _account(self, ts: float, wall: datetime) -> list[Bucket]:
        """Credit the time since the last update to the state held during it."""
        buckets: list[Bucket] = []
        if self._last_wall is not None and self._since is not None and wall > self._last_wall:
            gap = (wall - self._last_wall).total_seconds()
            held = min(gap, MAX_ATTRIBUTED_GAP_S)
            split = self._last_wall + timedelta(seconds=held)
            buckets.extend(self._buckets.add(self._last_wall, split, self._state))
            if split < wall:
                buckets.extend(self._buckets.add(split, wall, A.UNKNOWN))
        self._last_ts = ts
        self._last_wall = wall
        return buckets

    def _remember(self, ts: float, state: AttentionState, confidence: float, low: bool) -> None:
        self._history.append((ts, state, confidence, low))
        horizon = max(self._t.scan_window_s, 1.0) + MAX_ATTRIBUTED_GAP_S
        while len(self._history) > 1 and ts - self._history[1][0] > horizon:
            self._history.popleft()

    def flush(self) -> MachineOutput:
        """Close the open episode and emit the open minute (engine stop)."""
        closed: list[Episode] = []
        if self._last_ts is not None and self._last_wall is not None:
            closed = self._close_episode(self._last_ts, self._last_wall)
        return MachineOutput(status=self.status(), closed=closed, buckets=self._buckets.flush())

    # --- queries ---

    def status(self) -> StationStatus:
        """The current station state."""
        return StationStatus(
            state=self._state,
            since_ts=self._since_ts,
            since=self._since,
            confidence=self._confidence,
            low_confidence=self._low,
            monitoring_unavailable=self._unavailable,
            degraded_slots=self._degraded,
        )

    def scan_decision(self, now_ts: float | None = None) -> ScanDecision:
        """The state for a scan at `now_ts` (default: the latest update).

        1. The current state, if it is Sleeping, Drowsy, Distracted or Absent,
           or On Break (a break never warns, whatever came before it).
        2. Otherwise the warned state that held the largest share of the last
           `scan_window_s` in the fused (pre-hysteresis) states, if that share
           is at least 40%.
        3. Otherwise the current state (Attentive or Unknown).
        Answered from memory; no inference happens here.
        """
        current = self._state
        if current in WARNED_STATES or current is A.ON_BREAK:
            return self._decision(current, self._confidence, self._low)
        now = now_ts if now_ts is not None else (self._last_ts or 0.0)
        window = self._t.scan_window_s
        start = now - window
        shares: dict[AttentionState, float] = {}
        best_conf: dict[AttentionState, tuple[float, bool]] = {}
        entries = list(self._history)
        for i, (ts, state, confidence, low) in enumerate(entries):
            seg_end = entries[i + 1][0] if i + 1 < len(entries) else now
            seg_start = max(ts, start)
            seg_end = min(seg_end, now)
            if seg_end <= seg_start:
                continue
            shares[state] = shares.get(state, 0.0) + (seg_end - seg_start)
            prev = best_conf.get(state, (0.0, False))
            best_conf[state] = (max(prev[0], confidence), prev[1] or low)
        warned = {s: v for s, v in shares.items() if s in WARNED_STATES}
        if warned:
            state, held = max(warned.items(), key=lambda kv: kv[1])
            if held + _EPS >= SCAN_WINDOW_MIN_SHARE * window:
                confidence, low = best_conf[state]
                return self._decision(state, confidence, low)
        return self._decision(current, self._confidence, self._low)

    @staticmethod
    def _decision(state: AttentionState, confidence: float, low: bool) -> ScanDecision:
        return ScanDecision(state, confidence, low, scan_outcome(state))
