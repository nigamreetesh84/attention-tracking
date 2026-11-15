"""Thresholds: defaults, range validation and resolution order."""

import pytest
from ag_shared.thresholds import ThresholdOverrides, Thresholds, resolve_thresholds
from pydantic import ValidationError

# --- defaults ---------------------------------------------------------------


def test_defaults_equal_the_scope_trigger_table() -> None:
    """scope-v1.md section 2 ("Default trigger") and 4.1 (rolling window)."""
    t = Thresholds()
    assert t.distracted_seconds == 5  # head off-axis for more than 5 s
    assert t.perclos_threshold == 0.30  # PERCLOS above 30% ...
    assert t.perclos_window_s == 30  # ... over 30 s
    assert t.sleeping_eyes_closed_s == 4  # eyes closed more than 4 s
    assert t.absent_seconds == 10  # no face for more than 10 s
    assert t.scan_window_s == 5  # rolling window state (last 5 s)


def test_defaults_equal_the_design_doc_table() -> None:
    """detection-engine.md section 4, every key."""
    assert Thresholds().model_dump() == {
        "distracted_yaw_deg": 30,
        "distracted_pitch_deg": 25,
        "distracted_seconds": 5,
        "perclos_threshold": 0.30,
        "perclos_window_s": 30,
        "yawns_for_drowsy": 3,
        "yawn_window_s": 300,
        "sleeping_eyes_closed_s": 4,
        "sleeping_head_drop_s": 8,
        "absent_seconds": 10,
        "quality_min": 0.5,
        "unknown_seconds": 2,
        "recovery_seconds": 2,
        "gap_tolerance_s": 0.5,
        "scan_window_s": 5,
    }


def test_overrides_cover_exactly_the_threshold_keys() -> None:
    assert set(ThresholdOverrides.model_fields) == set(Thresholds.model_fields)
    assert ThresholdOverrides().overridden() == {}


# --- validation -------------------------------------------------------------

_OUT_OF_RANGE: list[tuple[str, object]] = [
    ("distracted_yaw_deg", 0),
    ("distracted_yaw_deg", -10),
    ("distracted_yaw_deg", 90.5),
    ("distracted_pitch_deg", 0),
    ("distracted_pitch_deg", 91),
    ("distracted_seconds", 0),
    ("distracted_seconds", -1),
    ("distracted_seconds", 3601),
    ("perclos_threshold", 0),
    ("perclos_threshold", 1),
    ("perclos_threshold", 1.5),
    ("perclos_window_s", 0),
    ("perclos_window_s", 3601),
    ("yawns_for_drowsy", 0),
    ("yawns_for_drowsy", 101),
    ("yawns_for_drowsy", 2.5),
    ("yawn_window_s", 0),
    ("sleeping_eyes_closed_s", 0),
    ("sleeping_head_drop_s", -8),
    ("absent_seconds", 0),
    ("quality_min", -0.1),
    ("quality_min", 1.1),
    ("unknown_seconds", 0),
    ("recovery_seconds", 0),
    ("gap_tolerance_s", -0.1),
    ("gap_tolerance_s", 5.1),
    ("scan_window_s", 0),
    ("scan_window_s", 61),
]

_WRONG_TYPE: list[tuple[str, object]] = [
    ("distracted_seconds", "5"),
    ("distracted_seconds", None),
    ("distracted_seconds", True),
    ("distracted_seconds", float("nan")),
    ("distracted_seconds", float("inf")),
    ("perclos_threshold", "high"),
    ("yawns_for_drowsy", "3"),
    ("yawns_for_drowsy", True),
    ("quality_min", [0.5]),
]


@pytest.mark.parametrize(("key", "value"), _OUT_OF_RANGE)
def test_out_of_range_values_are_rejected(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Thresholds.model_validate({key: value})
    with pytest.raises(ValidationError):
        ThresholdOverrides.model_validate({key: value})


@pytest.mark.parametrize(("key", "value"), _WRONG_TYPE)
def test_wrong_types_are_rejected(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Thresholds.model_validate({key: value})
    if value is not None:  # None means "not overridden" in an override layer
        with pytest.raises(ValidationError):
            ThresholdOverrides.model_validate({key: value})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("distracted_yaw_deg", 90),
        ("distracted_yaw_deg", 0.1),
        ("perclos_threshold", 0.01),
        ("perclos_threshold", 0.99),
        ("yawns_for_drowsy", 1),
        ("yawns_for_drowsy", 100),
        ("quality_min", 0),
        ("quality_min", 1),
        ("gap_tolerance_s", 0),
        ("scan_window_s", 60),
        ("distracted_seconds", 3600),
    ],
)
def test_boundary_values_are_accepted(key: str, value: float) -> None:
    assert getattr(Thresholds.model_validate({key: value}), key) == value


def test_integers_are_accepted_for_float_keys() -> None:
    assert Thresholds.model_validate({"absent_seconds": 12}).absent_seconds == 12.0


@pytest.mark.parametrize("model", [Thresholds, ThresholdOverrides])
def test_unknown_keys_are_rejected(model: type[Thresholds] | type[ThresholdOverrides]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"sleeping_eyes_closed_seconds": 4})


@pytest.mark.parametrize(
    "timer",
    [
        "distracted_seconds",
        "sleeping_eyes_closed_s",
        "sleeping_head_drop_s",
        "absent_seconds",
        "unknown_seconds",
        "recovery_seconds",
    ],
)
def test_gap_tolerance_must_be_shorter_than_every_timer(timer: str) -> None:
    """A gap tolerance as long as a timer would let that timer never reset."""
    with pytest.raises(ValidationError):
        Thresholds.model_validate({"gap_tolerance_s": 1.0, timer: 1.0})
    assert Thresholds.model_validate({"gap_tolerance_s": 0.9, timer: 1.0})


def test_thresholds_are_immutable() -> None:
    t = Thresholds()
    with pytest.raises(ValidationError):
        t.absent_seconds = 1.0  # frozen model: the assignment must fail


# --- resolution order -------------------------------------------------------


def test_no_layers_gives_the_defaults() -> None:
    assert resolve_thresholds() == Thresholds()


def test_each_layer_overrides_only_its_own_keys() -> None:
    resolved = resolve_thresholds(
        site=ThresholdOverrides(absent_seconds=20),
        ppe=ThresholdOverrides(sleeping_head_drop_s=6),
        shift=ThresholdOverrides(perclos_threshold=0.25),
        station=ThresholdOverrides(distracted_yaw_deg=40),
    )
    assert resolved == Thresholds(
        absent_seconds=20,
        sleeping_head_drop_s=6,
        perclos_threshold=0.25,
        distracted_yaw_deg=40,
    )


def test_resolution_order_later_layer_wins_per_key() -> None:
    """defaults -> site -> PPE -> shift -> station (schema.md threshold_profile)."""
    site = ThresholdOverrides(absent_seconds=11)
    ppe = ThresholdOverrides(absent_seconds=12)
    shift = ThresholdOverrides(absent_seconds=13)
    station = ThresholdOverrides(absent_seconds=14)
    assert resolve_thresholds().absent_seconds == 10
    assert resolve_thresholds(site=site).absent_seconds == 11
    assert resolve_thresholds(site=site, ppe=ppe).absent_seconds == 12
    assert resolve_thresholds(site=site, ppe=ppe, shift=shift).absent_seconds == 13
    assert resolve_thresholds(site=site, ppe=ppe, shift=shift, station=station).absent_seconds == 14
    # The order is fixed by the layer, not by the order of the arguments.
    assert resolve_thresholds(station=station, shift=shift, ppe=ppe, site=site).absent_seconds == 14


_LAYERS = ("site", "ppe", "shift", "station")


@pytest.mark.parametrize(
    ("lower", "higher"),
    [(lo, hi) for i, lo in enumerate(_LAYERS) for hi in _LAYERS[i + 1 :]],
)
def test_pairwise_precedence(lower: str, higher: str) -> None:
    layers: dict[str, ThresholdOverrides | None] = dict.fromkeys(_LAYERS)
    layers[lower] = ThresholdOverrides(distracted_seconds=7, absent_seconds=15)
    layers[higher] = ThresholdOverrides(distracted_seconds=9)
    resolved = resolve_thresholds(
        site=layers["site"], ppe=layers["ppe"], shift=layers["shift"], station=layers["station"]
    )
    assert resolved.distracted_seconds == 9
    assert resolved.absent_seconds == 15  # untouched by the higher layer


def test_custom_defaults_are_the_lowest_layer() -> None:
    base = Thresholds(absent_seconds=30, recovery_seconds=3)
    resolved = resolve_thresholds(defaults=base, site=ThresholdOverrides(absent_seconds=25))
    assert resolved.absent_seconds == 25
    assert resolved.recovery_seconds == 3


def test_resolved_result_is_validated_as_a_whole() -> None:
    """Each layer is valid alone; the combination is not, so it is refused."""
    with pytest.raises(ValidationError):
        resolve_thresholds(
            site=ThresholdOverrides(gap_tolerance_s=2.0, unknown_seconds=3.0),
            station=ThresholdOverrides(unknown_seconds=1.0),
        )


# --- JSON -------------------------------------------------------------------


def test_thresholds_json_round_trip() -> None:
    t = Thresholds(absent_seconds=12.5, yawns_for_drowsy=4)
    assert Thresholds.model_validate_json(t.model_dump_json()) == t


def test_overrides_json_round_trip_keeps_only_overridden_keys() -> None:
    o = ThresholdOverrides(absent_seconds=12.5)
    assert o.overridden() == {"absent_seconds": 12.5}
    assert ThresholdOverrides.model_validate_json(o.model_dump_json()) == o
    assert ThresholdOverrides.model_validate(o.overridden()) == o
