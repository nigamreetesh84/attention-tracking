"""Attention states, severity order and scan outcome (detection-engine.md 5)."""

import pytest
from ag_shared.states import (
    SEVERITY_ORDER,
    WARNED_STATES,
    AttentionState,
    ScanOutcome,
    is_more_severe,
    most_severe,
    scan_outcome,
    severity_rank,
)

S = AttentionState


def test_seven_states_with_wire_values() -> None:
    assert {s.value for s in AttentionState} == {
        "attentive",
        "distracted",
        "drowsy",
        "sleeping",
        "absent",
        "unknown",
        "on_break",
    }


def test_severity_order_most_to_least() -> None:
    assert SEVERITY_ORDER == (S.SLEEPING, S.ABSENT, S.DROWSY, S.DISTRACTED, S.ATTENTIVE)


def test_severity_rank_follows_the_order() -> None:
    ranks = [severity_rank(s) for s in SEVERITY_ORDER]
    assert all(r is not None for r in ranks)
    assert ranks == sorted(ranks, key=lambda r: -(r or 0))
    assert severity_rank(S.ATTENTIVE) == 0


@pytest.mark.parametrize("state", [S.UNKNOWN, S.ON_BREAK])
def test_unknown_and_on_break_are_outside_the_order(state: AttentionState) -> None:
    assert severity_rank(state) is None
    assert not is_more_severe(state, S.ATTENTIVE)
    assert not is_more_severe(S.SLEEPING, state)


def test_is_more_severe() -> None:
    assert is_more_severe(S.SLEEPING, S.ABSENT)
    assert is_more_severe(S.ABSENT, S.DROWSY)
    assert is_more_severe(S.DROWSY, S.DISTRACTED)
    assert is_more_severe(S.DISTRACTED, S.ATTENTIVE)
    assert not is_more_severe(S.DROWSY, S.DROWSY)
    assert not is_more_severe(S.ATTENTIVE, S.DISTRACTED)


def test_most_severe_ignores_unranked_states() -> None:
    assert most_severe([S.ATTENTIVE, S.UNKNOWN, S.DROWSY, S.DISTRACTED]) is S.DROWSY
    assert most_severe([S.UNKNOWN, S.ON_BREAK]) is None
    assert most_severe([]) is None


def test_warned_states() -> None:
    assert frozenset({S.DISTRACTED, S.DROWSY, S.SLEEPING, S.ABSENT}) == WARNED_STATES


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (S.ATTENTIVE, ScanOutcome.ATTENTIVE),
        (S.DISTRACTED, ScanOutcome.WARNED),
        (S.DROWSY, ScanOutcome.WARNED),
        (S.SLEEPING, ScanOutcome.WARNED),
        (S.ABSENT, ScanOutcome.WARNED),
        (S.UNKNOWN, ScanOutcome.UNMONITORED),
        (S.ON_BREAK, ScanOutcome.UNMONITORED),
    ],
)
def test_scan_outcome(state: AttentionState, outcome: ScanOutcome) -> None:
    assert scan_outcome(state) is outcome


def test_scan_outcome_wire_values() -> None:
    assert {o.value for o in ScanOutcome} == {"attentive", "warned", "unmonitored"}
