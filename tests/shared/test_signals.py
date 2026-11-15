"""Signal names and per-frame signal containers (detection-engine.md 2)."""

import pytest
from ag_shared.signals import (
    ALWAYS_ON_SIGNALS,
    BODY_SIGNALS,
    BOOLEAN_SIGNALS,
    EYE_SIGNALS,
    HEAD_POSE_SIGNALS,
    SIGNAL_DEPENDENCIES,
    YAWN_SIGNALS,
    SignalFrame,
    SignalName,
    SignalReading,
)
from pydantic import ValidationError

N = SignalName


def test_signal_names_match_the_design_doc() -> None:
    assert {s.value for s in SignalName} == {
        "face_present",
        "quality",
        "ear",
        "eyes_closed",
        "perclos",
        "blink_rate",
        "mar",
        "yawn",
        "head_yaw",
        "head_pitch",
        "head_roll",
        "head_nod",
        "head_dropped",
        "posture",
        "motion",
    }


def test_groups_partition_all_signals() -> None:
    groups = [ALWAYS_ON_SIGNALS, EYE_SIGNALS, YAWN_SIGNALS, HEAD_POSE_SIGNALS, BODY_SIGNALS]
    union: set[SignalName] = set()
    for group in groups:
        assert not union & group
        union |= group
    assert union == set(SignalName)


def test_group_members() -> None:
    assert frozenset({N.FACE_PRESENT, N.QUALITY}) == ALWAYS_ON_SIGNALS
    assert frozenset({N.EAR, N.EYES_CLOSED, N.PERCLOS, N.BLINK_RATE}) == EYE_SIGNALS
    assert frozenset({N.MAR, N.YAWN}) == YAWN_SIGNALS
    assert frozenset({N.POSTURE, N.MOTION}) == BODY_SIGNALS
    assert (
        frozenset({N.FACE_PRESENT, N.EYES_CLOSED, N.YAWN, N.HEAD_NOD, N.HEAD_DROPPED})
        == BOOLEAN_SIGNALS
    )


def test_dependencies_point_at_real_signals_without_cycles() -> None:
    for name in SignalName:
        seen = {name}
        current = name
        while current in SIGNAL_DEPENDENCIES:
            current = SIGNAL_DEPENDENCIES[current]
            assert current not in seen
            seen.add(current)
    assert SIGNAL_DEPENDENCIES[N.PERCLOS] is N.EYES_CLOSED
    assert SIGNAL_DEPENDENCIES[N.EYES_CLOSED] is N.EAR
    assert SIGNAL_DEPENDENCIES[N.YAWN] is N.MAR
    assert SIGNAL_DEPENDENCIES[N.HEAD_DROPPED] is N.HEAD_PITCH


def test_reading_has_value_and_validity() -> None:
    reading = SignalReading(value=0.27)
    assert reading.value == 0.27
    assert reading.valid is True
    assert SignalReading(value=0.0, valid=False).valid is False


def test_reading_flag_helpers() -> None:
    assert SignalReading.flag(True).is_true
    assert not SignalReading.flag(False).is_true
    assert not SignalReading.flag(True, valid=False).is_true
    assert SignalReading.invalid() == SignalReading(value=0.0, valid=False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "0.5", None])
def test_reading_rejects_non_finite_or_non_numeric(value: object) -> None:
    with pytest.raises(ValidationError):
        SignalReading.model_validate({"value": value})


def test_reading_is_immutable() -> None:
    reading = SignalReading(value=1.0)
    with pytest.raises(ValidationError):
        reading.value = 2.0  # frozen model: the assignment must fail


def _frame() -> SignalFrame:
    return SignalFrame(
        camera_slot=1,
        ts=12.5,
        signals={
            N.FACE_PRESENT: SignalReading.flag(True),
            N.QUALITY: SignalReading(value=0.8),
            N.EAR: SignalReading.invalid(),
            N.EYES_CLOSED: SignalReading.flag(True, valid=False),
        },
    )


def test_frame_accessors() -> None:
    frame = _frame()
    assert frame.value(N.QUALITY) == 0.8
    assert frame.flag(N.FACE_PRESENT) is True
    # Invalid readings and signals the profile disabled both read as "no data".
    assert frame.value(N.EAR) is None
    assert frame.flag(N.EYES_CLOSED) is None
    assert frame.value(N.PERCLOS) is None
    assert frame.flag(N.YAWN) is None
    assert frame.reading(N.EAR) == SignalReading.invalid()
    assert frame.reading(N.PERCLOS) is None


@pytest.mark.parametrize("slot", [0, 5, -1])
def test_frame_rejects_bad_slot(slot: int) -> None:
    with pytest.raises(ValidationError):
        SignalFrame.model_validate({"camera_slot": slot, "ts": 0.0, "signals": {}})


def test_frame_rejects_unknown_signal_name() -> None:
    with pytest.raises(ValidationError):
        SignalFrame.model_validate(
            {"camera_slot": 1, "ts": 0.0, "signals": {"face_embedding": {"value": 1.0}}}
        )


def test_frame_json_round_trip() -> None:
    frame = _frame()
    assert SignalFrame.model_validate_json(frame.model_dump_json()) == frame
