"""Attention states, their severity order and the scan outcome.

Source: `docs/scope-v1.md` section 2 and `docs/detection-engine.md` section 5.
The string values are the wire and database values (`docs/schema.md`).
"""

from collections.abc import Iterable
from enum import StrEnum


class AttentionState(StrEnum):
    """State of one camera candidate or of the whole station."""

    ATTENTIVE = "attentive"
    DISTRACTED = "distracted"
    DROWSY = "drowsy"
    SLEEPING = "sleeping"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    ON_BREAK = "on_break"


SEVERITY_ORDER: tuple[AttentionState, ...] = (
    AttentionState.SLEEPING,
    AttentionState.ABSENT,
    AttentionState.DROWSY,
    AttentionState.DISTRACTED,
    AttentionState.ATTENTIVE,
)
"""Most to least severe. Unknown and On Break are outside the order."""

WARNED_STATES: frozenset[AttentionState] = frozenset(
    {
        AttentionState.DISTRACTED,
        AttentionState.DROWSY,
        AttentionState.SLEEPING,
        AttentionState.ABSENT,
    }
)
"""States that warn the worker on a scan and open a `state_episode` event."""

_RANK: dict[AttentionState, int] = {
    state: rank for rank, state in enumerate(reversed(SEVERITY_ORDER))
}


def severity_rank(state: AttentionState) -> int | None:
    """Return the severity of `state` (Attentive 0 ... Sleeping 4).

    Unknown and On Break have no severity and return None, so callers must
    decide explicitly what to do with them instead of comparing by accident.
    """
    return _RANK.get(state)


def is_more_severe(a: AttentionState, b: AttentionState) -> bool:
    """True when `a` is strictly more severe than `b`.

    False when either state is outside the severity order.
    """
    rank_a, rank_b = _RANK.get(a), _RANK.get(b)
    return rank_a is not None and rank_b is not None and rank_a > rank_b


def most_severe(states: Iterable[AttentionState]) -> AttentionState | None:
    """Return the most severe ranked state, ignoring Unknown and On Break.

    None when no ranked state is given.
    """
    ranked = [s for s in states if s in _RANK]
    return max(ranked, key=_RANK.__getitem__) if ranked else None


class ScanOutcome(StrEnum):
    """What a scan was recorded as. The scan is accepted in every case."""

    ATTENTIVE = "attentive"
    WARNED = "warned"
    UNMONITORED = "unmonitored"


def scan_outcome(state: AttentionState) -> ScanOutcome:
    """Map the scan state to its outcome (warn-and-log; never a rejection)."""
    if state is AttentionState.ATTENTIVE:
        return ScanOutcome.ATTENTIVE
    if state in WARNED_STATES:
        return ScanOutcome.WARNED
    return ScanOutcome.UNMONITORED
