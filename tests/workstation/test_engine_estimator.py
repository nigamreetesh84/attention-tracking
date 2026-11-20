"""Per-camera estimator: every candidate rule and every PPE profile row."""

from collections.abc import Iterable

import pytest
from ag_shared.ppe import PpeProfile, PpeProfileKind, SignalSetting, builtin_profile
from ag_shared.signals import SignalFrame, SignalName
from ag_shared.states import AttentionState
from ag_shared.thresholds import Thresholds
from ag_workstation.engine import CameraCandidate, CameraEstimator, EstimatorParams
from ag_workstation.engine.synthetic import (
    ATTENTIVE,
    DARK,
    EYES_CLOSED,
    HEAD_DOWN_STILL,
    LOOKING_AWAY,
    NO_FACE,
    Look,
    make_frame,
    trace,
)

A = AttentionState
K = PpeProfileKind


def _run(
    segments: list[tuple[Look, float]],
    kind: PpeProfileKind = K.OPEN_FACE,
    *,
    profile: PpeProfile | None = None,
    thresholds: Thresholds | None = None,
    params: EstimatorParams | None = None,
) -> list[CameraCandidate]:
    prof = profile or builtin_profile(kind)
    est = CameraEstimator(1, prof, thresholds or Thresholds(), params)
    return [est.update(f) for f in trace(segments, profile=prof)]


def _state_at(candidates: Iterable[CameraCandidate], ts: float) -> AttentionState:
    for c in candidates:
        if abs(c.ts - ts) < 1e-6:
            return c.state
    raise AssertionError(f"no candidate at {ts}")


def _states(candidates: Iterable[CameraCandidate]) -> set[AttentionState]:
    return {c.state for c in candidates}


# --- Attentive, Unknown, Absent ---


def test_attentive_by_default() -> None:
    out = _run([(ATTENTIVE, 3.0)])
    assert _states(out) == {A.ATTENTIVE}
    assert all(c.usable and c.face_seen for c in out)
    assert out[-1].confidence == pytest.approx(0.9)


def test_first_frame_unusable_is_unknown() -> None:
    out = _run([(DARK, 0.5)])
    assert _states(out) == {A.UNKNOWN}
    assert not any(c.usable for c in out)


def test_unknown_after_unknown_seconds_of_bad_quality() -> None:
    out = _run([(ATTENTIVE, 1.0), (DARK, 3.0)])
    # Held for the first 2 s of bad quality, then Unknown (strictly after 2 s).
    assert _state_at(out, 3.0) is A.ATTENTIVE
    assert _state_at(out, 3.1) is A.UNKNOWN
    assert not out[-1].usable


def test_short_bad_quality_burst_holds_previous_state() -> None:
    out = _run([(ATTENTIVE, 1.0), (DARK, 1.0), (ATTENTIVE, 1.0)])
    assert _states(out) == {A.ATTENTIVE}
    held = [c for c in out if 1.0 <= c.ts < 2.0]
    assert all(c.usable and not c.quality_ok for c in held)


def test_absent_after_absent_seconds_without_a_face() -> None:
    out = _run([(ATTENTIVE, 1.0), (NO_FACE, 12.0)])
    assert _state_at(out, 11.0) is A.ATTENTIVE  # 10.0 s: not yet more than 10 s
    assert _state_at(out, 11.1) is A.ABSENT
    assert out[-1].usable and not out[-1].face_seen


def test_absent_ends_when_the_face_returns() -> None:
    out = _run([(NO_FACE, 11.0), (ATTENTIVE, 1.0)])
    assert _state_at(out, 10.5) is A.ABSENT
    assert out[-1].state is A.ATTENTIVE


def test_visible_slumped_body_is_not_absent_for_body_profiles() -> None:
    slumped = NO_FACE.with_(posture=0.95, motion=0.0)
    out = _run([(ATTENTIVE, 1.0), (slumped, 12.0)], K.SUNGLASSES)
    assert A.ABSENT not in _states(out)
    # Head dropped (by posture) and still for more than 8 s: Sleeping.
    assert _state_at(out, 9.2) is A.SLEEPING


def test_no_frames_recently_is_unknown() -> None:
    est = CameraEstimator(1, builtin_profile(K.OPEN_FACE), Thresholds())
    assert est.candidate_at(0.0).state is A.UNKNOWN
    est.update(make_frame(ATTENTIVE, 1.0))
    assert est.candidate_at(2.5).state is A.ATTENTIVE
    assert est.candidate_at(3.1).state is A.UNKNOWN


# --- Sleeping per profile ---


def test_open_face_sleeping_boundary_4_0_vs_4_1_seconds() -> None:
    out = _run([(ATTENTIVE, 1.0), (EYES_CLOSED, 5.0)])
    assert _state_at(out, 5.0) is not A.SLEEPING  # closed 4.0 s
    assert _state_at(out, 5.1) is A.SLEEPING  # closed 4.1 s
    assert out[-1].confidence == pytest.approx(0.9)


def test_short_open_eye_gap_does_not_reset_the_sleeping_timer() -> None:
    out = _run([(EYES_CLOSED, 2.0), (ATTENTIVE, 0.3), (EYES_CLOSED, 2.2)])
    assert out[-1].state is A.SLEEPING


def test_long_open_eye_gap_resets_the_sleeping_timer() -> None:
    out = _run([(EYES_CLOSED, 2.0), (ATTENTIVE, 1.0), (EYES_CLOSED, 2.2)])
    assert A.SLEEPING not in _states(out)


def test_clear_glasses_sleeping_has_reduced_eye_weight() -> None:
    out = _run([(EYES_CLOSED, 4.5)], K.CLEAR_GLASSES)
    assert out[-1].state is A.SLEEPING
    assert out[-1].confidence == pytest.approx(0.9 * 0.8)


def test_glare_falls_back_to_head_drop_with_lower_confidence() -> None:
    glare_head_down = HEAD_DOWN_STILL.with_(eyes_closed=None, ear=None)
    out = _run([(ATTENTIVE, 1.0), (glare_head_down, 9.0)], K.CLEAR_GLASSES)
    last = out[-1]
    assert last.state is A.SLEEPING
    assert last.fallback
    assert last.confidence == pytest.approx(0.9 * 0.6)
    assert _state_at(out, 9.0) is not A.SLEEPING  # dropped exactly 8.0 s


def test_readable_eyes_do_not_use_the_fallback() -> None:
    head_down_eyes_open = HEAD_DOWN_STILL
    out = _run([(head_down_eyes_open, 10.0)], K.OPEN_FACE)
    assert A.SLEEPING not in _states(out)
    # Pitch -35 is off-axis: looking down for more than 5 s is Distracted.
    assert out[-1].state is A.DISTRACTED


def test_sunglasses_ignores_eyes_and_uses_head_drop_and_stillness() -> None:
    out = _run([(EYES_CLOSED, 6.0)], K.SUNGLASSES)
    assert A.SLEEPING not in _states(out)
    out = _run([(ATTENTIVE, 1.0), (HEAD_DOWN_STILL, 9.0)], K.SUNGLASSES)
    assert _state_at(out, 9.0) is not A.SLEEPING
    assert _state_at(out, 9.1) is A.SLEEPING
    assert out[-1].confidence == pytest.approx(0.9 * 0.8)


def test_sunglasses_head_drop_with_movement_is_not_sleeping() -> None:
    moving = HEAD_DOWN_STILL.with_(motion=0.5)
    out = _run([(moving, 10.0)], K.SUNGLASSES)
    assert A.SLEEPING not in _states(out)


def test_mask_sleeping_by_eyes_and_no_yawn_drowsiness() -> None:
    out = _run([(EYES_CLOSED, 4.5)], K.MASK)
    assert out[-1].state is A.SLEEPING
    yawning = [(ATTENTIVE.with_(yawn=True), 2.0), (ATTENTIVE, 2.0)] * 4
    out = _run(yawning, K.MASK)
    assert A.DROWSY not in _states(out)


def test_helmet_eyes_closed_alone_is_not_sleeping() -> None:
    out = _run([(EYES_CLOSED, 6.0)], K.HELMET_SHIELD)
    assert A.SLEEPING not in _states(out)


def test_helmet_eyes_closed_with_head_drop() -> None:
    closed_dropped = HEAD_DOWN_STILL.with_(eyes_closed=True, ear=0.3, motion=0.4)
    out = _run([(closed_dropped, 4.5)], K.HELMET_SHIELD)
    assert out[-1].state is A.SLEEPING
    assert out[-1].confidence == pytest.approx(0.9 * 1.0)


def test_helmet_head_dropped_and_still() -> None:
    out = _run([(HEAD_DOWN_STILL, 8.5)], K.HELMET_SHIELD)
    assert out[-1].state is A.SLEEPING


def test_custom_profile_without_any_sleeping_rule_never_sleeps() -> None:
    base = builtin_profile(K.CUSTOM)
    signals = dict(base.signals)
    for name in (
        SignalName.EAR,
        SignalName.EYES_CLOSED,
        SignalName.PERCLOS,
        SignalName.BLINK_RATE,
    ):
        signals[name] = SignalSetting(enabled=False)
    profile = PpeProfile(kind=K.CUSTOM, signals=signals)
    out = _run([(EYES_CLOSED, 6.0), (HEAD_DOWN_STILL, 10.0)], profile=profile)
    assert A.SLEEPING not in _states(out)


# --- Drowsy ---


def test_perclos_above_threshold_is_drowsy() -> None:
    out = _run([(ATTENTIVE.with_(perclos=0.31), 1.0)])
    assert _states(out) == {A.DROWSY}
    out = _run([(ATTENTIVE.with_(perclos=0.30), 1.0)])
    assert A.DROWSY not in _states(out)


def test_three_yawns_in_the_window_is_drowsy() -> None:
    yawn = ATTENTIVE.with_(yawn=True)
    out = _run([(yawn, 2.0), (ATTENTIVE, 5.0), (yawn, 2.0), (ATTENTIVE, 5.0)])
    assert A.DROWSY not in _states(out)
    out = _run([(yawn, 2.0), (ATTENTIVE, 5.0)] * 3)
    assert out[-1].state is A.DROWSY


def test_yawns_outside_the_window_do_not_count() -> None:
    yawn = ATTENTIVE.with_(yawn=True)
    thresholds = Thresholds(yawn_window_s=10.0)
    out = _run([(yawn, 2.0), (ATTENTIVE, 9.0)] * 3, thresholds=thresholds)
    assert A.DROWSY not in _states(out)


def test_repeated_head_nods_are_drowsy() -> None:
    nod = ATTENTIVE.with_(nod=True)
    out = _run([(nod, 0.1), (ATTENTIVE, 3.0)] * 3)
    assert out[-1].state is A.DROWSY
    out = _run([(nod, 0.1), (ATTENTIVE, 3.0)] * 2)
    assert A.DROWSY not in _states(out)


def test_disabled_signals_are_ignored() -> None:
    # PERCLOS high but the sunglasses profile has no eye signals.
    frames = [make_frame(ATTENTIVE.with_(perclos=0.9), i / 10) for i in range(10)]
    est = CameraEstimator(1, builtin_profile(K.SUNGLASSES), Thresholds())
    assert {est.update(f).state for f in frames} == {A.ATTENTIVE}


# --- Distracted ---


def test_distracted_after_distracted_seconds_off_axis() -> None:
    out = _run([(ATTENTIVE, 1.0), (LOOKING_AWAY, 6.0)])
    assert _state_at(out, 6.0) is A.ATTENTIVE  # off-axis exactly 5.0 s
    assert _state_at(out, 6.1) is A.DISTRACTED


@pytest.mark.parametrize(
    ("look", "distracted"),
    [
        (ATTENTIVE.with_(yaw=-31.0), True),
        (ATTENTIVE.with_(yaw=30.0), False),
        (ATTENTIVE.with_(pitch=26.0), True),
        (ATTENTIVE.with_(pitch=-26.0), True),
        (ATTENTIVE.with_(pitch=25.0), False),
    ],
)
def test_distracted_angles(look: Look, distracted: bool) -> None:
    out = _run([(look, 6.0)])
    assert (out[-1].state is A.DISTRACTED) is distracted


def test_severity_sleeping_beats_distracted() -> None:
    closed_away = EYES_CLOSED.with_(yaw=45.0)
    out = _run([(closed_away, 6.0)])
    assert out[-1].state is A.SLEEPING


# --- lifecycle ---


def test_reset_and_replay_restart() -> None:
    profile = builtin_profile(K.OPEN_FACE)
    est = CameraEstimator(1, profile, Thresholds())
    for f in trace([(EYES_CLOSED, 4.5)]):
        est.update(f)
    assert est.last is not None and est.last.state is A.SLEEPING
    # A timestamp earlier than the last one restarts everything.
    assert est.update(make_frame(ATTENTIVE, 0.0)).state is A.ATTENTIVE
    est.reset()
    assert est.last is None


def test_configure_swaps_thresholds_between_frames() -> None:
    est = CameraEstimator(1, builtin_profile(K.OPEN_FACE), Thresholds())
    for f in trace([(EYES_CLOSED, 3.0)]):
        est.update(f)
    est.configure(thresholds=Thresholds(sleeping_eyes_closed_s=2.0))
    assert est.update(make_frame(EYES_CLOSED, 3.0)).state is A.SLEEPING
    est.configure(profile=builtin_profile(K.SUNGLASSES))
    assert est.slot == 1


def test_frame_from_another_camera_is_refused() -> None:
    est = CameraEstimator(1, builtin_profile(K.OPEN_FACE), Thresholds())
    with pytest.raises(ValueError, match="another camera"):
        est.update(make_frame(ATTENTIVE, 0.0, slot=2))


def test_candidate_carries_the_signal_snapshot() -> None:
    est = CameraEstimator(1, builtin_profile(K.OPEN_FACE), Thresholds())
    frame = make_frame(ATTENTIVE, 0.0)
    candidate = est.update(frame)
    assert isinstance(candidate.frame, SignalFrame)
    assert candidate.frame.ts == 0.0


def test_absent_ends_on_the_first_frame_that_shows_the_face() -> None:
    out = _run([(NO_FACE, 12.0), (ATTENTIVE, 0.3)])
    assert _state_at(out, 11.9) is A.ABSENT
    assert _state_at(out, 12.0) is not A.ABSENT


@pytest.mark.parametrize("fps", [1.5, 1.9, 5.0])
def test_states_are_detected_at_low_frame_rates(fps: float) -> None:
    profile = builtin_profile(K.OPEN_FACE)
    for segments, expected in (
        ([(ATTENTIVE, 2.0), (EYES_CLOSED, 30.0)], A.SLEEPING),
        ([(ATTENTIVE, 2.0), (NO_FACE, 30.0)], A.ABSENT),
        ([(ATTENTIVE, 2.0), (DARK, 30.0)], A.UNKNOWN),
        ([(ATTENTIVE, 2.0), (LOOKING_AWAY, 30.0)], A.DISTRACTED),
    ):
        est = CameraEstimator(1, profile, Thresholds())
        out = [est.update(f) for f in trace(segments, fps=fps, profile=profile)]
        assert out[-1].state is expected, (fps, expected)


def test_states_are_detected_with_processing_stalls() -> None:
    # 10 fps with a 0.6 s stall every 3 s (frames dropped, latest-frame-wins).
    profile = builtin_profile(K.OPEN_FACE)
    est = CameraEstimator(1, profile, Thresholds())
    frames = [f for f in trace([(EYES_CLOSED, 60.0)], profile=profile) if (f.ts % 3.0) < 2.4]
    assert [est.update(f) for f in frames][-1].state is A.SLEEPING


def test_empty_station_at_start_is_unknown_until_absent() -> None:
    out = _run([(NO_FACE, 12.0)])
    assert _state_at(out, 5.0) is A.UNKNOWN
    assert not out[0].usable
    assert out[-1].state is A.ABSENT
