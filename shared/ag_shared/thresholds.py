"""Detection thresholds, partial overrides and their resolution order.

Source: `docs/detection-engine.md` section 4 (keys and defaults, taken from
the trigger table in `docs/scope-v1.md` section 2) and `docs/schema.md`
(`threshold_profile`: resolution order).
"""

from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# One alias per kind of key, so `Thresholds` and `ThresholdOverrides` cannot
# drift apart in their ranges. `strict` refuses strings and booleans.
_AngleDeg = Annotated[float, Field(strict=True, gt=0, le=90, allow_inf_nan=False)]
_Seconds = Annotated[float, Field(strict=True, gt=0, le=3600, allow_inf_nan=False)]
_Share = Annotated[float, Field(strict=True, gt=0, lt=1, allow_inf_nan=False)]
_Quality = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
_Count = Annotated[int, Field(strict=True, ge=1, le=100)]
_GapSeconds = Annotated[float, Field(strict=True, ge=0, le=5, allow_inf_nan=False)]
_ScanWindow = Annotated[float, Field(strict=True, gt=0, le=60, allow_inf_nan=False)]

# Timers that `gap_tolerance_s` bridges or that end a state.
_TIMER_KEYS = (
    "distracted_seconds",
    "sleeping_eyes_closed_s",
    "sleeping_head_drop_s",
    "absent_seconds",
    "unknown_seconds",
    "recovery_seconds",
)


class Thresholds(BaseModel):
    """A complete, validated set of detection thresholds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distracted_yaw_deg: _AngleDeg = 30.0
    distracted_pitch_deg: _AngleDeg = 25.0
    distracted_seconds: _Seconds = 5.0
    perclos_threshold: _Share = 0.30
    perclos_window_s: _Seconds = 30.0
    yawns_for_drowsy: _Count = 3
    yawn_window_s: _Seconds = 300.0
    sleeping_eyes_closed_s: _Seconds = 4.0
    sleeping_head_drop_s: _Seconds = 8.0
    absent_seconds: _Seconds = 10.0
    quality_min: _Quality = 0.5
    unknown_seconds: _Seconds = 2.0
    recovery_seconds: _Seconds = 2.0
    gap_tolerance_s: _GapSeconds = 0.5
    scan_window_s: _ScanWindow = 5.0

    @model_validator(mode="after")
    def _gap_shorter_than_timers(self) -> Self:
        # A tolerated gap as long as a timer would mean the timer never resets.
        for key in _TIMER_KEYS:
            if self.gap_tolerance_s >= getattr(self, key):
                raise ValueError(f"gap_tolerance_s must be shorter than {key}")
        return self


class ThresholdOverrides(BaseModel):
    """A partial `Thresholds`: only the keys one layer overrides.

    This is the `values` of a `threshold_profile`. Each key is range-checked
    here; cross-key rules are checked when the layers are resolved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    distracted_yaw_deg: _AngleDeg | None = None
    distracted_pitch_deg: _AngleDeg | None = None
    distracted_seconds: _Seconds | None = None
    perclos_threshold: _Share | None = None
    perclos_window_s: _Seconds | None = None
    yawns_for_drowsy: _Count | None = None
    yawn_window_s: _Seconds | None = None
    sleeping_eyes_closed_s: _Seconds | None = None
    sleeping_head_drop_s: _Seconds | None = None
    absent_seconds: _Seconds | None = None
    quality_min: _Quality | None = None
    unknown_seconds: _Seconds | None = None
    recovery_seconds: _Seconds | None = None
    gap_tolerance_s: _GapSeconds | None = None
    scan_window_s: _ScanWindow | None = None

    def overridden(self) -> dict[str, float]:
        """The keys this layer sets, with their values."""
        return self.model_dump(exclude_none=True)


def resolve_thresholds(
    *,
    site: ThresholdOverrides | None = None,
    ppe: ThresholdOverrides | None = None,
    shift: ThresholdOverrides | None = None,
    station: ThresholdOverrides | None = None,
    defaults: Thresholds | None = None,
) -> Thresholds:
    """Merge the override layers into one complete `Thresholds`.

    Order, later wins per key: built-in defaults -> site default -> PPE
    profile -> shift -> workstation. The merged result is validated as a
    whole and a `pydantic.ValidationError` is raised if the combination is
    invalid, so a bad combination is refused instead of reaching the engine.
    """
    # Any: values are float or int depending on the key; validated below.
    merged: dict[str, Any] = (defaults or Thresholds()).model_dump()
    for layer in (site, ppe, shift, station):
        if layer is not None:
            merged.update(layer.overridden())
    return Thresholds.model_validate(merged)
