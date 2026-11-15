"""PPE (face-gear) profiles: which signals run and with what weight.

Source: `docs/scope-v1.md` section 4.3 and `docs/detection-engine.md`
section 3. A disabled signal is not computed.
"""

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from ag_shared.signals import (
    ALWAYS_ON_SIGNALS,
    BODY_SIGNALS,
    EYE_SIGNALS,
    SIGNAL_DEPENDENCIES,
    YAWN_SIGNALS,
    SignalName,
)
from ag_shared.thresholds import ThresholdOverrides


class PpeProfileKind(StrEnum):
    """Profile kinds; values match `ppe_profile.kind` in the schema."""

    OPEN_FACE = "open_face"
    CLEAR_GLASSES = "clear_glasses"
    SUNGLASSES = "sunglasses"
    MASK = "mask"
    HELMET_SHIELD = "helmet_shield"
    CUSTOM = "custom"


class SleepingRule(StrEnum):
    """Ways a profile can conclude Sleeping. Any one rule holding is enough."""

    EYES_CLOSED = "eyes_closed"
    """Eyes closed for `sleeping_eyes_closed_s`."""
    EYES_CLOSED_WITH_HEAD_DROP = "eyes_closed_with_head_drop"
    """Eyes closed for `sleeping_eyes_closed_s` while the head is dropped."""
    HEAD_DROPPED_STILL = "head_dropped_still"
    """Head dropped and no motion for `sleeping_head_drop_s`."""


class SignalSetting(BaseModel):
    """Whether one signal is computed and how much it counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: StrictBool = True
    weight: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] = 1.0


# (eye weight or None = off, yawn, body). Head pose is on in every built-in.
# Sunglasses yawn is off: scope 4.3 says "head pose, posture and motion only".
_BUILTIN_TABLE: dict[PpeProfileKind, tuple[float | None, bool, bool]] = {
    PpeProfileKind.OPEN_FACE: (1.0, True, False),
    PpeProfileKind.CLEAR_GLASSES: (0.8, True, False),
    PpeProfileKind.SUNGLASSES: (None, False, True),
    PpeProfileKind.MASK: (1.0, False, False),
    PpeProfileKind.HELMET_SHIELD: (0.5, False, True),
    # Starting point for an admin-defined profile: the full open-face method.
    PpeProfileKind.CUSTOM: (1.0, True, False),
}


def _builtin_signals(kind: PpeProfileKind) -> dict[SignalName, SignalSetting]:
    eye_weight, yawn, body = _BUILTIN_TABLE[kind]
    on, off = SignalSetting(), SignalSetting(enabled=False)
    signals: dict[SignalName, SignalSetting] = {}
    for name in SignalName:
        if name in EYE_SIGNALS:
            signals[name] = off if eye_weight is None else SignalSetting(weight=eye_weight)
        elif name in YAWN_SIGNALS:
            signals[name] = on if yawn else off
        elif name in BODY_SIGNALS:
            signals[name] = on if body else off
        else:  # always-on and head pose
            signals[name] = on
    return signals


_BUILTIN_ENABLED: dict[PpeProfileKind, frozenset[SignalName]] = {
    kind: frozenset(n for n, s in _builtin_signals(kind).items() if s.enabled)
    for kind in PpeProfileKind
}


class PpeProfile(BaseModel):
    """A PPE profile as the engine consumes it.

    `signals` always holds an entry for every `SignalName` after validation
    (missing ones are filled in as disabled). `thresholds` is the profile's
    layer in threshold resolution, if it has one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PpeProfileKind
    signals: dict[SignalName, SignalSetting]
    glare_tolerance: StrictBool = False
    """Eye frames with specular highlights count as invalid, not open/closed."""
    thresholds: ThresholdOverrides | None = None

    @field_validator("signals", mode="after")
    @classmethod
    def _complete_signals(
        cls, value: dict[SignalName, SignalSetting]
    ) -> dict[SignalName, SignalSetting]:
        return {
            name: value.get(name, SignalSetting(enabled=name in ALWAYS_ON_SIGNALS))
            for name in SignalName
        }

    @model_validator(mode="after")
    def _check_signal_set(self) -> Self:
        enabled = self.enabled_signals
        missing = ALWAYS_ON_SIGNALS - enabled
        if missing:
            raise ValueError(f"signals {sorted(missing)} cannot be disabled")
        for derived, source in SIGNAL_DEPENDENCIES.items():
            if derived in enabled and source not in enabled:
                raise ValueError(f"signal {derived.value} needs {source.value} enabled")
        if self.kind is not PpeProfileKind.CUSTOM and enabled != _BUILTIN_ENABLED[self.kind]:
            raise ValueError(
                f"a {self.kind.value} profile must keep its built-in signal set; "
                "use kind 'custom' to change which signals are enabled"
            )
        return self

    @property
    def enabled_signals(self) -> frozenset[SignalName]:
        """The signals this profile computes."""
        return frozenset(name for name, setting in self.signals.items() if setting.enabled)

    def is_enabled(self, name: SignalName) -> bool:
        """Whether the profile computes `name`."""
        return self.signals[name].enabled

    def weight(self, name: SignalName) -> float:
        """The weight of `name`; 0.0 when the signal is disabled."""
        setting = self.signals[name]
        return setting.weight if setting.enabled else 0.0

    @property
    def sleeping_rules(self) -> tuple[SleepingRule, ...]:
        """The profile's sleeping rule, derived from its enabled signals.

        Reproduces the "Sleeping rule" column for the built-in profiles. An
        empty tuple means the profile cannot detect Sleeping (a custom
        profile without eye closure and without head drop plus motion).
        """
        eyes = self.is_enabled(SignalName.EYES_CLOSED)
        drop_still = self.is_enabled(SignalName.HEAD_DROPPED) and self.is_enabled(SignalName.MOTION)
        if eyes and drop_still:
            return (SleepingRule.EYES_CLOSED_WITH_HEAD_DROP, SleepingRule.HEAD_DROPPED_STILL)
        if eyes:
            return (SleepingRule.EYES_CLOSED,)
        if drop_still:
            return (SleepingRule.HEAD_DROPPED_STILL,)
        return ()


BUILTIN_PROFILES: MappingProxyType[PpeProfileKind, PpeProfile] = MappingProxyType(
    {
        kind: PpeProfile(
            kind=kind,
            signals=_builtin_signals(kind),
            glare_tolerance=kind is PpeProfileKind.CLEAR_GLASSES,
        )
        for kind in PpeProfileKind
    }
)
"""The six built-in profiles (read-only). `custom` is the editing template."""


def builtin_profile(kind: PpeProfileKind) -> PpeProfile:
    """Return the built-in profile of `kind`."""
    return BUILTIN_PROFILES[kind]
