"""PPE profiles: the six built-ins, signal sets, weights and sleeping rules."""

import pytest
from ag_shared.ppe import (
    BUILTIN_PROFILES,
    PpeProfile,
    PpeProfileKind,
    SignalSetting,
    SleepingRule,
    builtin_profile,
)
from ag_shared.signals import (
    ALWAYS_ON_SIGNALS,
    BODY_SIGNALS,
    EYE_SIGNALS,
    HEAD_POSE_SIGNALS,
    YAWN_SIGNALS,
    SignalName,
)
from ag_shared.thresholds import ThresholdOverrides
from pydantic import ValidationError

K = PpeProfileKind
N = SignalName

# detection-engine.md section 3, with the sunglasses yawn column taken from
# scope-v1.md 4.3 ("Head pose, posture and motion only").
_EXPECTED_ENABLED: dict[PpeProfileKind, frozenset[SignalName]] = {
    K.OPEN_FACE: ALWAYS_ON_SIGNALS | EYE_SIGNALS | YAWN_SIGNALS | HEAD_POSE_SIGNALS,
    K.CLEAR_GLASSES: ALWAYS_ON_SIGNALS | EYE_SIGNALS | YAWN_SIGNALS | HEAD_POSE_SIGNALS,
    K.SUNGLASSES: ALWAYS_ON_SIGNALS | HEAD_POSE_SIGNALS | BODY_SIGNALS,
    K.MASK: ALWAYS_ON_SIGNALS | EYE_SIGNALS | HEAD_POSE_SIGNALS,
    K.HELMET_SHIELD: ALWAYS_ON_SIGNALS | EYE_SIGNALS | HEAD_POSE_SIGNALS | BODY_SIGNALS,
    K.CUSTOM: ALWAYS_ON_SIGNALS | EYE_SIGNALS | YAWN_SIGNALS | HEAD_POSE_SIGNALS,
}

_EXPECTED_EYE_WEIGHT: dict[PpeProfileKind, float] = {
    K.OPEN_FACE: 1.0,
    K.CLEAR_GLASSES: 0.8,
    K.SUNGLASSES: 0.0,
    K.MASK: 1.0,
    K.HELMET_SHIELD: 0.5,
    K.CUSTOM: 1.0,
}

_EXPECTED_SLEEPING: dict[PpeProfileKind, tuple[SleepingRule, ...]] = {
    K.OPEN_FACE: (SleepingRule.EYES_CLOSED,),
    K.CLEAR_GLASSES: (SleepingRule.EYES_CLOSED,),
    K.SUNGLASSES: (SleepingRule.HEAD_DROPPED_STILL,),
    K.MASK: (SleepingRule.EYES_CLOSED,),
    K.HELMET_SHIELD: (SleepingRule.EYES_CLOSED_WITH_HEAD_DROP, SleepingRule.HEAD_DROPPED_STILL),
    K.CUSTOM: (SleepingRule.EYES_CLOSED,),
}


def test_kind_wire_values_match_the_schema() -> None:
    assert {k.value for k in PpeProfileKind} == {
        "open_face",
        "clear_glasses",
        "sunglasses",
        "mask",
        "helmet_shield",
        "custom",
    }


def test_there_are_six_builtin_profiles() -> None:
    assert set(BUILTIN_PROFILES) == set(PpeProfileKind)
    for kind, profile in BUILTIN_PROFILES.items():
        assert profile.kind is kind
        assert builtin_profile(kind) == profile


@pytest.mark.parametrize("kind", list(PpeProfileKind))
def test_builtin_signal_sets(kind: PpeProfileKind) -> None:
    profile = builtin_profile(kind)
    assert profile.enabled_signals == _EXPECTED_ENABLED[kind]
    # Every signal has an explicit entry, so a consumer never guesses.
    assert set(profile.signals) == set(SignalName)
    for name in SignalName:
        assert profile.is_enabled(name) is (name in _EXPECTED_ENABLED[kind])


@pytest.mark.parametrize("kind", list(PpeProfileKind))
def test_builtin_eye_weights(kind: PpeProfileKind) -> None:
    profile = builtin_profile(kind)
    for name in EYE_SIGNALS:
        assert profile.weight(name) == _EXPECTED_EYE_WEIGHT[kind]
    for name in HEAD_POSE_SIGNALS:
        assert profile.weight(name) == 1.0


def test_disabled_signal_has_zero_weight() -> None:
    assert builtin_profile(K.MASK).weight(N.YAWN) == 0.0
    assert builtin_profile(K.OPEN_FACE).weight(N.MOTION) == 0.0


@pytest.mark.parametrize("kind", list(PpeProfileKind))
def test_only_clear_glasses_has_glare_tolerance(kind: PpeProfileKind) -> None:
    assert builtin_profile(kind).glare_tolerance is (kind is K.CLEAR_GLASSES)


@pytest.mark.parametrize("kind", list(PpeProfileKind))
def test_builtin_sleeping_rules(kind: PpeProfileKind) -> None:
    assert builtin_profile(kind).sleeping_rules == _EXPECTED_SLEEPING[kind]


def test_builtins_carry_no_threshold_overrides() -> None:
    assert all(p.thresholds is None for p in BUILTIN_PROFILES.values())


# --- custom profiles --------------------------------------------------------


def _signals(*enabled: SignalName, weight: float = 1.0) -> dict[str, dict[str, object]]:
    names = ALWAYS_ON_SIGNALS | set(enabled)
    return {n.value: {"enabled": True, "weight": weight} for n in names}


def test_custom_profile_missing_signals_default_to_disabled() -> None:
    profile = PpeProfile.model_validate(
        {"kind": "custom", "signals": _signals(N.HEAD_YAW, N.HEAD_PITCH)}
    )
    assert profile.enabled_signals == ALWAYS_ON_SIGNALS | {N.HEAD_YAW, N.HEAD_PITCH}
    assert set(profile.signals) == set(SignalName)
    assert profile.sleeping_rules == ()


def test_custom_sleeping_rules_are_derived_from_enabled_signals() -> None:
    eyes = (N.EAR, N.EYES_CLOSED)
    drop_still = (N.HEAD_PITCH, N.HEAD_DROPPED, N.MOTION)

    def rules(*names: SignalName) -> tuple[SleepingRule, ...]:
        return PpeProfile.model_validate(
            {"kind": "custom", "signals": _signals(*names)}
        ).sleeping_rules

    assert rules(*eyes) == (SleepingRule.EYES_CLOSED,)
    assert rules(*eyes, N.HEAD_PITCH, N.HEAD_DROPPED) == (SleepingRule.EYES_CLOSED,)
    assert rules(*drop_still) == (SleepingRule.HEAD_DROPPED_STILL,)
    assert rules(*eyes, *drop_still) == (
        SleepingRule.EYES_CLOSED_WITH_HEAD_DROP,
        SleepingRule.HEAD_DROPPED_STILL,
    )
    # Head drop without the motion signal cannot prove stillness.
    assert rules(N.HEAD_PITCH, N.HEAD_DROPPED) == ()


@pytest.mark.parametrize("always_on", sorted(ALWAYS_ON_SIGNALS))
def test_presence_and_quality_cannot_be_disabled(always_on: SignalName) -> None:
    signals = _signals(N.HEAD_YAW)
    signals[always_on.value] = {"enabled": False}
    with pytest.raises(ValidationError):
        PpeProfile.model_validate({"kind": "custom", "signals": signals})


@pytest.mark.parametrize(
    "enabled",
    [
        (N.EYES_CLOSED,),  # needs ear
        (N.EAR, N.PERCLOS),  # needs eyes_closed
        (N.EAR, N.BLINK_RATE),  # needs eyes_closed
        (N.YAWN,),  # needs mar
        (N.HEAD_NOD,),  # needs head_pitch
        (N.HEAD_DROPPED,),  # needs head_pitch
    ],
)
def test_derived_signal_needs_its_source(enabled: tuple[SignalName, ...]) -> None:
    with pytest.raises(ValidationError):
        PpeProfile.model_validate({"kind": "custom", "signals": _signals(*enabled)})


@pytest.mark.parametrize("weight", [-0.1, 1.1, "1", True, float("nan")])
def test_weight_out_of_range_is_rejected(weight: object) -> None:
    with pytest.raises(ValidationError):
        SignalSetting.model_validate({"enabled": True, "weight": weight})


def test_weight_bounds_are_accepted() -> None:
    assert SignalSetting(weight=0.0).weight == 0.0
    assert SignalSetting(weight=1.0).weight == 1.0
    assert SignalSetting() == SignalSetting(enabled=True, weight=1.0)


def test_unknown_signal_or_kind_or_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PpeProfile.model_validate(
            {"kind": "custom", "signals": {**_signals(), "face_embedding": {"enabled": True}}}
        )
    with pytest.raises(ValidationError):
        PpeProfile.model_validate({"kind": "balaclava", "signals": _signals()})
    with pytest.raises(ValidationError):
        PpeProfile.model_validate({"kind": "custom", "signals": _signals(), "template": "x"})
    with pytest.raises(ValidationError):
        PpeProfile.model_validate({"kind": "custom"})


def test_builtin_kind_must_keep_its_signal_set() -> None:
    """Only `custom` may change which signals are on; a `mask` profile with
    yawning enabled would silently stop meaning "mask"."""
    mask = builtin_profile(K.MASK).model_dump(mode="json")
    mask["signals"]["mar"] = {"enabled": True, "weight": 1.0}
    mask["signals"]["yawn"] = {"enabled": True, "weight": 1.0}
    with pytest.raises(ValidationError):
        PpeProfile.model_validate(mask)
    mask["kind"] = "custom"
    assert PpeProfile.model_validate(mask).is_enabled(N.YAWN)


def test_builtin_kind_may_tune_weights() -> None:
    data = builtin_profile(K.HELMET_SHIELD).model_dump(mode="json")
    data["signals"]["eyes_closed"]["weight"] = 0.4
    assert PpeProfile.model_validate(data).weight(N.EYES_CLOSED) == 0.4


def test_profile_may_carry_threshold_overrides() -> None:
    profile = builtin_profile(K.SUNGLASSES).model_copy(
        update={"thresholds": ThresholdOverrides(sleeping_head_drop_s=6)}
    )
    restored = PpeProfile.model_validate_json(profile.model_dump_json())
    assert restored == profile
    assert restored.thresholds is not None
    assert restored.thresholds.overridden() == {"sleeping_head_drop_s": 6}


def test_invalid_threshold_override_in_profile_is_rejected() -> None:
    data = builtin_profile(K.MASK).model_dump(mode="json")
    data["thresholds"] = {"absent_seconds": -1}
    with pytest.raises(ValidationError):
        PpeProfile.model_validate(data)


@pytest.mark.parametrize("kind", list(PpeProfileKind))
def test_builtin_json_round_trip(kind: PpeProfileKind) -> None:
    profile = builtin_profile(kind)
    assert PpeProfile.model_validate_json(profile.model_dump_json()) == profile
    assert PpeProfile.model_validate(profile.model_dump(mode="json")) == profile
