"""Multi-camera fusion: exhaustive two-camera table and 1-4 camera cases."""

import itertools

import pytest
from ag_shared.states import SEVERITY_ORDER, AttentionState
from ag_workstation.engine import CameraCandidate
from ag_workstation.engine.fusion import DegradedTracker, fuse

A = AttentionState
ALL = (*SEVERITY_ORDER, A.UNKNOWN)


def cand(
    slot: int,
    state: AttentionState,
    *,
    confidence: float = 0.8,
    quality: float = 0.9,
    face: bool | None = None,
) -> CameraCandidate:
    usable = state is not A.UNKNOWN
    face_seen = (state not in (A.ABSENT, A.UNKNOWN)) if face is None else face
    return CameraCandidate(
        slot=slot,
        ts=1.0,
        state=state,
        confidence=confidence,
        quality=quality if usable else 0.1,
        usable=usable,
        face_seen=face_seen,
    )


def _expected(primary: AttentionState, secondary: AttentionState) -> tuple[AttentionState, bool]:
    """Independent statement of rules 1-6 for two cameras."""
    usable = [s for s in (primary, secondary) if s is not A.UNKNOWN]
    if not usable:
        return A.UNKNOWN, False
    if len(usable) == 1:
        return usable[0], False
    if primary == secondary:
        return primary, False
    if A.ABSENT in (primary, secondary):  # the other camera sees a face
        other = secondary if primary is A.ABSENT else primary
        return other, False
    rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    return min((primary, secondary), key=rank.__getitem__), True


@pytest.mark.parametrize(("primary", "secondary"), list(itertools.product(ALL, ALL)))
def test_two_camera_table(primary: AttentionState, secondary: AttentionState) -> None:
    fused = fuse([cand(1, primary), cand(2, secondary)], primary_slot=1, ts=1.0)
    state, low = _expected(primary, secondary)
    assert fused.state is state
    assert fused.low_confidence is low
    assert not fused.monitoring_unavailable


def test_rule1_primary_only() -> None:
    fused = fuse([cand(1, A.DROWSY, confidence=0.7)], primary_slot=1, ts=2.0)
    assert (fused.state, fused.confidence, fused.source_slots) == (A.DROWSY, 0.7, (1,))
    assert fused.ts == 2.0


def test_rule2_best_quality_secondary_when_primary_unknown() -> None:
    fused = fuse(
        [
            cand(1, A.UNKNOWN),
            cand(2, A.ATTENTIVE, quality=0.6),
            cand(3, A.DISTRACTED, quality=0.95, confidence=0.5),
        ],
        primary_slot=1,
        ts=1.0,
    )
    assert fused.state is A.DISTRACTED
    assert fused.source_slots == (3,)
    assert fused.confidence == 0.5


def test_rule2_quality_tie_prefers_lower_slot() -> None:
    fused = fuse(
        [cand(1, A.UNKNOWN), cand(3, A.DROWSY), cand(2, A.ATTENTIVE)], primary_slot=1, ts=1.0
    )
    assert fused.source_slots == (2,)


def test_rule3_agreement_takes_the_higher_confidence() -> None:
    fused = fuse(
        [cand(1, A.SLEEPING, confidence=0.6), cand(2, A.SLEEPING, confidence=0.9)],
        primary_slot=1,
        ts=1.0,
    )
    assert (fused.state, fused.confidence, fused.low_confidence) == (A.SLEEPING, 0.9, False)
    assert fused.source_slots == (1, 2)


def test_rule4_disagreement_takes_the_more_severe_state() -> None:
    fused = fuse(
        [cand(1, A.ATTENTIVE, confidence=0.9), cand(2, A.DROWSY, confidence=0.4)],
        primary_slot=1,
        ts=1.0,
    )
    assert (fused.state, fused.confidence, fused.low_confidence) == (A.DROWSY, 0.4, True)
    assert fused.source_slots == (2,)


def test_rule5_face_on_any_camera_is_not_absent() -> None:
    fused = fuse([cand(1, A.ABSENT), cand(2, A.ATTENTIVE)], primary_slot=1, ts=1.0)
    assert fused.state is A.ATTENTIVE
    assert not fused.low_confidence
    # Also when the absent camera is the secondary.
    fused = fuse([cand(1, A.SLEEPING), cand(2, A.ABSENT)], primary_slot=1, ts=1.0)
    assert fused.state is A.SLEEPING


def test_rule5_face_cameras_that_disagree_still_flag_low_confidence() -> None:
    fused = fuse(
        [cand(1, A.ABSENT), cand(2, A.ATTENTIVE), cand(3, A.DISTRACTED)],
        primary_slot=1,
        ts=1.0,
    )
    assert (fused.state, fused.low_confidence) == (A.DISTRACTED, True)


def test_absent_on_every_camera_is_absent() -> None:
    fused = fuse([cand(1, A.ABSENT), cand(2, A.ABSENT)], primary_slot=1, ts=1.0)
    assert fused.state is A.ABSENT


def test_rule6_no_usable_camera_is_unknown() -> None:
    fused = fuse([cand(1, A.UNKNOWN), cand(2, A.UNKNOWN)], primary_slot=1, ts=1.0)
    assert (fused.state, fused.confidence) == (A.UNKNOWN, 0.0)
    assert not fused.monitoring_unavailable


def test_rule6_all_offline_is_monitoring_unavailable() -> None:
    fused = fuse(
        [cand(1, A.UNKNOWN), cand(2, A.UNKNOWN)],
        primary_slot=1,
        ts=1.0,
        offline_slots={1, 2},
    )
    assert fused.state is A.UNKNOWN
    assert fused.monitoring_unavailable


def test_offline_camera_is_ignored_even_with_a_stale_candidate() -> None:
    fused = fuse(
        [cand(1, A.SLEEPING), cand(2, A.ATTENTIVE)], primary_slot=1, ts=1.0, offline_slots={1}
    )
    assert fused.state is A.ATTENTIVE


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_one_to_four_cameras(count: int) -> None:
    cams = [cand(slot, A.ATTENTIVE) for slot in range(1, count + 1)]
    assert fuse(cams, primary_slot=1, ts=1.0).state is A.ATTENTIVE
    cams[-1] = cand(count, A.SLEEPING)
    fused = fuse(cams, primary_slot=1, ts=1.0)
    assert fused.state is A.SLEEPING
    assert fused.low_confidence is (count > 1)


def test_four_cameras_with_primary_unknown_and_one_offline() -> None:
    fused = fuse(
        [
            cand(1, A.UNKNOWN),
            cand(2, A.DROWSY, quality=0.99),
            cand(3, A.ATTENTIVE, quality=0.7),
            cand(4, A.SLEEPING, quality=0.8),
        ],
        primary_slot=1,
        ts=1.0,
        offline_slots={2},
    )
    assert fused.state is A.SLEEPING
    assert fused.source_slots == (4,)


def test_primary_slot_need_not_be_slot_one() -> None:
    fused = fuse([cand(1, A.UNKNOWN), cand(2, A.DROWSY)], primary_slot=2, ts=1.0)
    assert fused.state is A.DROWSY


def test_missing_primary_candidate_is_an_error() -> None:
    with pytest.raises(ValueError, match="primary"):
        fuse([cand(2, A.ATTENTIVE)], primary_slot=1, ts=1.0)


def test_candidates_are_reported_by_slot_and_degraded_slots_pass_through() -> None:
    fused = fuse(
        [cand(2, A.ATTENTIVE), cand(1, A.ATTENTIVE)],
        primary_slot=1,
        ts=1.0,
        degraded_slots=(2,),
    )
    assert [c.slot for c in fused.candidates] == [1, 2]
    assert fused.degraded_slots == (2,)


def test_rule7_degraded_after_30_seconds() -> None:
    tracker = DegradedTracker()
    good, bad = cand(1, A.ATTENTIVE), cand(2, A.UNKNOWN)
    assert tracker.update(0.0, [good, bad]) == ()
    assert tracker.update(30.0, [good, bad]) == ()
    assert tracker.update(30.1, [good, bad]) == (2,)
    # Recovery clears it; going bad again starts a new 30 s.
    assert tracker.update(31.0, [good, cand(2, A.ATTENTIVE)]) == ()
    assert tracker.update(32.0, [good, bad]) == ()
    assert tracker.update(70.0, [good, cand(2, A.ATTENTIVE)], offline_slots={1}) == ()
    assert tracker.update(100.1, [good, cand(2, A.ATTENTIVE)], offline_slots={1}) == (1,)
    tracker.reset()
    assert tracker.update(200.0, [good, bad]) == ()
