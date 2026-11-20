"""Multi-camera fusion (`docs/detection-engine.md` section 6).

`fuse` is a pure function of the per-camera candidates, so it is tested
exhaustively over state combinations. Rules:

1. Primary usable and no secondary usable: the primary decides.
2. Primary Unknown or low quality: the best-quality usable secondary decides.
3. Primary and secondaries usable and all agree: that state, with the highest
   confidence among them.
4. They disagree: the most severe state, with `low_confidence` set.
5. Presence exception: if any usable camera sees a good-quality face the
   station is not Absent; the state comes from the cameras that see the face.
6. No usable camera: Unknown. All cameras offline: also "monitoring
   unavailable".
7. A camera offline or below quality for more than `degraded_after_s` is
   reported as degraded (`DegradedTracker`); fusion continues without it.

"Usable" here means: not Unknown, not offline, and the latest frame of good
quality. A camera in a short bad-quality burst (its estimator still holds
the last state, `quality_ok` false) is used only when no camera has a good
frame, so the station does not drop to Unknown before `unknown_seconds`.
"""

from collections.abc import Collection, Iterable, Sequence

from ag_shared.states import AttentionState, most_severe

from ag_workstation.engine.types import CameraCandidate, FusedState

A = AttentionState

DEGRADED_AFTER_S = 30.0
"""Rule 7: how long a camera may be offline or unusable before it is degraded."""


def _best_quality(candidates: Iterable[CameraCandidate]) -> CameraCandidate:
    # Highest quality first; the lower slot wins a tie so the result is stable.
    return max(candidates, key=lambda c: (c.quality or 0.0, -c.slot))


def _combine(
    group: Sequence[CameraCandidate],
) -> tuple[AttentionState, float, bool, tuple[int, ...]]:
    """State, confidence, disagreement and deciding slots for usable candidates."""
    states = {c.state for c in group}
    if len(states) == 1:
        state = group[0].state
        low = False
    else:
        severe = most_severe(states)
        assert severe is not None  # noqa: S101 - usable candidates are ranked states
        state = severe
        low = True
    deciding = [c for c in group if c.state is state]
    confidence = max(c.confidence for c in deciding)
    return state, confidence, low, tuple(sorted(c.slot for c in deciding))


def fuse(
    candidates: Sequence[CameraCandidate],
    *,
    primary_slot: int,
    ts: float,
    offline_slots: Collection[int] = (),
    degraded_slots: Sequence[int] = (),
) -> FusedState:
    """Fuse one candidate per camera into the station state at `ts`."""
    by_slot = {c.slot: c for c in candidates}
    if primary_slot not in by_slot:
        raise ValueError("the primary camera has no candidate")
    all_offline = bool(candidates) and all(c.slot in offline_slots for c in candidates)
    trusted = [c for c in candidates if c.usable and c.slot not in offline_slots]
    good = [c for c in trusted if c.quality_ok]
    usable = good or trusted
    fused_candidates = tuple(sorted(candidates, key=lambda c: c.slot))

    def result(
        state: AttentionState,
        confidence: float,
        low: bool,
        slots: tuple[int, ...],
    ) -> FusedState:
        return FusedState(
            ts=ts,
            state=state,
            confidence=confidence,
            low_confidence=low,
            source_slots=slots,
            candidates=fused_candidates,
            degraded_slots=tuple(sorted(degraded_slots)),
            monitoring_unavailable=all_offline,
        )

    # Rule 6.
    if not usable:
        return result(A.UNKNOWN, 0.0, False, ())

    # Rule 5: someone is visibly at the station.
    face_cams = [c for c in usable if c.face_seen]
    if face_cams and any(c.state is A.ABSENT for c in usable):
        state, confidence, low, slots = _combine(face_cams)
        return result(state, confidence, low, slots)

    primary = by_slot[primary_slot]
    primary_usable = primary in usable
    secondaries = [c for c in usable if c.slot != primary_slot]

    # Rule 1.
    if primary_usable and not secondaries:
        return result(primary.state, primary.confidence, False, (primary.slot,))
    # Rule 2.
    if not primary_usable:
        best = _best_quality(secondaries)
        return result(best.state, best.confidence, False, (best.slot,))
    # Rules 3 and 4.
    state, confidence, low, slots = _combine([primary, *secondaries])
    return result(state, confidence, low, slots)


class DegradedTracker:
    """Rule 7: which cameras have been offline or unusable for too long."""

    def __init__(self, degraded_after_s: float = DEGRADED_AFTER_S) -> None:
        self._after = degraded_after_s
        self._bad_since: dict[int, float] = {}

    def reset(self) -> None:
        """Forget all history."""
        self._bad_since.clear()

    def update(
        self,
        ts: float,
        candidates: Iterable[CameraCandidate],
        offline_slots: Collection[int] = (),
    ) -> tuple[int, ...]:
        """Record the cameras' condition at `ts`; return the degraded slots."""
        degraded: list[int] = []
        for c in candidates:
            bad = c.slot in offline_slots or not (c.usable and c.quality_ok)
            if not bad:
                self._bad_since.pop(c.slot, None)
                continue
            since = self._bad_since.setdefault(c.slot, ts)
            if ts - since > self._after:
                degraded.append(c.slot)
        return tuple(sorted(degraded))
