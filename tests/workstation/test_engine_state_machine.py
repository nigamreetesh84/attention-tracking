"""Station state machine: transitions, episodes, buckets and the scan decision."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import count
from uuid import UUID

import pytest
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_shared.states import AttentionState, ScanOutcome
from ag_shared.thresholds import Thresholds
from ag_workstation.engine import CameraEstimator, fuse
from ag_workstation.engine.state_machine import (
    MAX_ATTRIBUTED_GAP_S,
    BucketAccumulator,
    MachineOutput,
    StationStateMachine,
)
from ag_workstation.engine.synthetic import (
    ATTENTIVE,
    DARK,
    EYES_CLOSED,
    LOOKING_AWAY,
    NO_FACE,
    Look,
    trace,
)
from ag_workstation.engine.types import FusedState

A = AttentionState
T0 = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)
FPS = 10


def fused(
    ts: float, state: AttentionState, confidence: float = 0.8, low: bool = False
) -> FusedState:
    return FusedState(
        ts=ts,
        state=state,
        confidence=confidence,
        low_confidence=low,
        source_slots=(1,),
        candidates=(),
    )


def _ids() -> Iterator[UUID]:
    for i in count(1):
        yield UUID(int=i)


def machine(thresholds: Thresholds | None = None) -> StationStateMachine:
    ids = _ids()
    return StationStateMachine(thresholds or Thresholds(), id_factory=lambda: next(ids))


class Driver:
    """Feeds a machine with fused states at a fixed rate."""

    def __init__(self, sm: StationStateMachine) -> None:
        self.sm = sm
        self.frame = 0
        self.outputs: list[MachineOutput] = []

    @property
    def ts(self) -> float:
        return self.frame / FPS

    def run(
        self,
        state: AttentionState,
        seconds: float,
        *,
        on_break: bool = False,
        confidence: float = 0.8,
        low: bool = False,
    ) -> None:
        for _ in range(round(seconds * FPS)):
            ts = self.frame / FPS
            out = self.sm.update(
                fused(ts, state, confidence, low),
                T0 + timedelta(seconds=ts),
                on_break=on_break,
            )
            self.outputs.append(out)
            self.frame += 1

    def current(self) -> AttentionState:
        return self.sm.state

    def states(self) -> list[AttentionState]:
        return [o.status.state for o in self.outputs]

    def opened(self) -> list[AttentionState]:
        return [e.state for o in self.outputs for e in o.opened]

    def closed(self) -> list[AttentionState]:
        return [e.state for o in self.outputs for e in o.closed]


# --- transitions ---


def test_starts_in_the_first_fused_state() -> None:
    d = Driver(machine())
    d.run(A.ATTENTIVE, 1)
    assert set(d.states()) == {A.ATTENTIVE}
    assert d.opened() == []


def test_more_severe_state_replaces_immediately() -> None:
    d = Driver(machine())
    d.run(A.ATTENTIVE, 1)
    d.run(A.DISTRACTED, 0.1)
    assert d.current() is A.DISTRACTED
    d.run(A.SLEEPING, 0.1)
    assert d.current() is A.SLEEPING
    assert d.opened() == [A.DISTRACTED, A.SLEEPING]
    assert d.closed() == [A.DISTRACTED]


def test_recovery_needs_recovery_seconds() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    d.run(A.ATTENTIVE, 1.9)  # 19 frames: 1.8 s between first and last
    assert d.current() is A.SLEEPING
    d.run(A.ATTENTIVE, 0.2)
    assert d.current() is A.ATTENTIVE
    assert d.closed() == [A.SLEEPING]


def test_no_flicker_at_boundaries() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    for _ in range(10):
        d.run(A.ATTENTIVE, 0.3)
        d.run(A.SLEEPING, 0.1)
    assert set(d.states()) == {A.SLEEPING}
    assert d.opened() == [A.SLEEPING]


def test_long_relapse_restarts_recovery() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    d.run(A.ATTENTIVE, 1.5)
    d.run(A.SLEEPING, 1.0)  # longer than gap tolerance
    d.run(A.ATTENTIVE, 1.5)
    assert d.current() is A.SLEEPING


def test_recovery_to_a_less_severe_warned_state_opens_a_new_episode() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    d.run(A.DISTRACTED, 2.5)
    assert d.current() is A.DISTRACTED
    assert d.opened() == [A.SLEEPING, A.DISTRACTED]
    assert d.closed() == [A.SLEEPING]


def test_sleeping_to_absent_closes_and_opens() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    d.run(A.ABSENT, 2.5)  # Absent is less severe than Sleeping: after recovery
    assert d.current() is A.ABSENT
    assert d.closed() == [A.SLEEPING]
    assert d.opened() == [A.SLEEPING, A.ABSENT]


def test_unknown_is_immediate_both_ways() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    d.run(A.UNKNOWN, 0.1)
    assert d.current() is A.UNKNOWN
    assert d.closed() == [A.SLEEPING]
    d.run(A.ATTENTIVE, 0.1)
    assert d.current() is A.ATTENTIVE


def test_episode_times_and_confidence() -> None:
    d = Driver(machine())
    d.run(A.ATTENTIVE, 1)
    d.run(A.DROWSY, 1, confidence=0.5)
    d.run(A.DROWSY, 1, confidence=0.9, low=True)
    d.run(A.ATTENTIVE, 2.5)
    episodes = [e for o in d.outputs for e in o.closed]
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.started_at == T0 + timedelta(seconds=1)
    assert ep.ended_at is not None and ep.ended_ts is not None
    assert ep.ended_at - ep.started_at == timedelta(seconds=ep.ended_ts - ep.started_ts)
    assert ep.confidence == 0.9
    assert ep.low_confidence
    assert ep.id == UUID(int=1)


def test_timestamps_must_not_go_back() -> None:
    sm = machine()
    sm.update(fused(1.0, A.ATTENTIVE), T0)
    with pytest.raises(ValueError, match="order"):
        sm.update(fused(0.5, A.ATTENTIVE), T0)


# --- breaks ---


def test_break_overrides_everything_and_suppresses_episodes() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    d.run(A.SLEEPING, 5, on_break=True)
    assert set(d.states()[10:]) == {A.ON_BREAK}
    assert d.opened() == [A.SLEEPING]
    assert d.closed() == [A.SLEEPING]
    assert d.sm.open_episode is None
    d.run(A.ABSENT, 1, on_break=True)
    assert d.opened() == [A.SLEEPING]
    # After the break the state is taken as it is, at once.
    d.run(A.DROWSY, 0.1)
    assert d.current() is A.DROWSY


# --- scan decision ---


def test_scan_rule1_current_warned_state() -> None:
    d = Driver(machine())
    d.run(A.DISTRACTED, 0.5, confidence=0.7)
    decision = d.sm.scan_decision()
    assert decision.state is A.DISTRACTED
    assert decision.outcome is ScanOutcome.WARNED
    assert decision.confidence == 0.7


def test_scan_rule2_window_share_at_least_40_percent() -> None:
    d = Driver(machine())
    d.run(A.ATTENTIVE, 10)
    d.run(A.DROWSY, 2.0)  # 2.0 s of the 5 s window
    d.run(A.ATTENTIVE, 3.0)
    assert d.current() is A.ATTENTIVE
    decision = d.sm.scan_decision(d.ts)
    assert decision.state is A.DROWSY
    assert decision.outcome is ScanOutcome.WARNED


def test_scan_rule2_below_40_percent_is_attentive() -> None:
    d = Driver(machine())
    d.run(A.ATTENTIVE, 10)
    d.run(A.DROWSY, 1.9)
    d.run(A.ATTENTIVE, 3.1)
    decision = d.sm.scan_decision(d.ts)
    assert decision.state is A.ATTENTIVE
    assert decision.outcome is ScanOutcome.ATTENTIVE


def test_scan_rule2_largest_share_wins() -> None:
    d = Driver(machine())
    d.run(A.ATTENTIVE, 10)
    d.run(A.ABSENT, 0.5)  # Absent is less severe than Sleeping...
    d.run(A.SLEEPING, 0.5)
    d.run(A.SLEEPING, 2.5)  # ...recovery keeps Sleeping
    d.run(A.ATTENTIVE, 2.5)
    assert d.sm.scan_decision(d.ts).state is A.SLEEPING


def test_scan_rule3_unknown_is_unmonitored() -> None:
    d = Driver(machine())
    d.run(A.UNKNOWN, 6)
    decision = d.sm.scan_decision()
    assert decision.state is A.UNKNOWN
    assert decision.outcome is ScanOutcome.UNMONITORED


def test_scan_unknown_after_a_warned_state_still_warns() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 3)
    d.run(A.UNKNOWN, 1)
    assert d.sm.scan_decision().state is A.SLEEPING


def test_scan_on_break_never_warns() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 4)
    d.run(A.SLEEPING, 0.5, on_break=True)
    decision = d.sm.scan_decision()
    assert decision.state is A.ON_BREAK
    assert decision.outcome is ScanOutcome.UNMONITORED


def test_scan_before_any_update_is_unknown() -> None:
    assert machine().scan_decision().state is A.UNKNOWN


# --- buckets ---


def test_buckets_split_by_minute_and_sum_to_60() -> None:
    d = Driver(machine())
    d.run(A.ATTENTIVE, 30)
    d.run(A.DISTRACTED, 45)
    d.run(A.ATTENTIVE, 60)
    buckets = [b for o in d.outputs for b in o.buckets]
    assert [b.bucket_start for b in buckets] == [T0, T0 + timedelta(minutes=1)]
    first, second = buckets
    assert first.seconds == {A.ATTENTIVE: 30.0, A.DISTRACTED: 30.0}
    assert sum(second.seconds.values()) == pytest.approx(60.0)
    # 15 s of the minute plus the 2 s recovery before Attentive returns.
    assert second.seconds[A.DISTRACTED] == pytest.approx(17.0)


def test_bucket_gap_beyond_the_limit_counts_as_unknown() -> None:
    sm = machine()
    sm.update(fused(0.0, A.ATTENTIVE), T0)
    sm.update(fused(10.0, A.ATTENTIVE), T0 + timedelta(seconds=10))
    out = sm.update(fused(60.0, A.ATTENTIVE), T0 + timedelta(seconds=60))
    (bucket,) = out.buckets
    assert bucket.seconds[A.ATTENTIVE] == pytest.approx(2 * MAX_ATTRIBUTED_GAP_S)
    assert bucket.seconds[A.UNKNOWN] == pytest.approx(60 - 2 * MAX_ATTRIBUTED_GAP_S)


def test_flush_closes_episode_and_emits_partial_minute() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 5)
    out = d.sm.flush()
    assert [e.state for e in out.closed] == [A.SLEEPING]
    assert len(out.buckets) == 1
    assert out.buckets[0].seconds[A.SLEEPING] == pytest.approx(4.9)


def test_bucket_accumulator_flush_when_empty() -> None:
    acc = BucketAccumulator()
    assert acc.flush() == []
    acc.add(T0, T0 + timedelta(seconds=5), A.ATTENTIVE)
    acc.reset()
    assert acc.flush() == []


def test_configure_changes_recovery() -> None:
    d = Driver(machine())
    d.run(A.SLEEPING, 1)
    d.sm.configure(Thresholds(recovery_seconds=1.0))
    d.run(A.ATTENTIVE, 1.1)
    assert d.current() is A.ATTENTIVE


# --- the scope's state table, through estimator, fusion and state machine ---


def _pipeline(
    segments: list[tuple[Look, float]], kind: PpeProfileKind = PpeProfileKind.OPEN_FACE
) -> list[AttentionState]:
    profile = builtin_profile(kind)
    est = CameraEstimator(1, profile, Thresholds())
    sm = StationStateMachine(Thresholds())
    states = []
    for frame in trace(segments, profile=profile):
        candidate = est.update(frame)
        f = fuse([candidate], primary_slot=1, ts=frame.ts)
        states.append(sm.update(f, T0 + timedelta(seconds=frame.ts)).status.state)
    return states


@pytest.mark.parametrize(
    ("segments", "expected"),
    [
        ([(ATTENTIVE, 3)], A.ATTENTIVE),
        ([(ATTENTIVE, 1), (LOOKING_AWAY, 5.5)], A.DISTRACTED),
        ([(ATTENTIVE, 1), (ATTENTIVE.with_(perclos=0.4), 1)], A.DROWSY),
        ([(ATTENTIVE, 1), (EYES_CLOSED, 4.5)], A.SLEEPING),
        ([(ATTENTIVE, 1), (NO_FACE, 10.5)], A.ABSENT),
        ([(ATTENTIVE, 1), (DARK, 2.5)], A.UNKNOWN),
    ],
)
def test_scope_state_table(segments: list[tuple[Look, float]], expected: AttentionState) -> None:
    assert _pipeline(segments)[-1] is expected


def test_scope_state_table_just_below_each_timer() -> None:
    assert A.DISTRACTED not in _pipeline([(ATTENTIVE, 1), (LOOKING_AWAY, 5.0)])
    assert A.SLEEPING not in _pipeline([(ATTENTIVE, 1), (EYES_CLOSED, 4.0)])
    assert A.ABSENT not in _pipeline([(ATTENTIVE, 1), (NO_FACE, 10.0)])
    assert A.UNKNOWN not in _pipeline([(ATTENTIVE, 1), (DARK, 2.0)])
