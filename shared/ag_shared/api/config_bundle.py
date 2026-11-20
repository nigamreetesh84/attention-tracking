"""The workstation config bundle (`api-contract.md` section 4.2).

The bundle reuses the engine's domain models (`CameraConfig`, `PpeProfile`,
`Thresholds`, `ShiftThresholds`, `Shift`, `CaptureConfig`). Those forbid
unknown keys on purpose, so the bundle strips unknown keys (and unknown
enum-keyed map entries such as a newer signal name) before validating,
which keeps "both sides ignore unknown fields". Any other invalid content,
for example an unknown PPE profile kind or camera kind, makes the whole
bundle invalid: the client keeps its previous bundle (fail-open).

Per-profile thresholds (owner decision 2026-11-20): `profile_thresholds`
carries, for every PPE profile a roster entry may switch to, the profile
itself and its fully resolved thresholds, so a worker's PPE override can be
applied offline. See `ConfigBundle.resolve` for the fallback order.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import Field, model_validator

from ag_shared.api.common import (
    ApiModel,
    NonNegativeInt,
    UtcDatetime,
    logger,
    strip_unknown_fields,
)
from ag_shared.config import CameraConfig, CaptureConfig, EngineConfig, Shift, ShiftThresholds
from ag_shared.ppe import PpeProfile
from ag_shared.site import SiteSettings
from ag_shared.thresholds import Thresholds

EMPLOYEE_ID_PATTERN = r"^[A-Za-z0-9]{1,16}$"
"""Owner default for Q3: alphanumeric, up to 16 characters."""


class SiteInfo(ApiModel):
    """The site's timezone and settings."""

    timezone: Annotated[str, Field(min_length=1, max_length=64)]
    settings: SiteSettings = SiteSettings()


class WorkstationInfo(ApiModel):
    """The workstation this bundle is for."""

    id: UUID
    name: Annotated[str, Field(min_length=1, max_length=100)]
    line_id: UUID
    camera_count: Annotated[int, Field(ge=1, le=4)]


class RosterEntry(ApiModel):
    """One active worker of the site, for offline login."""

    user_id: UUID
    employee_id: Annotated[str, Field(pattern=EMPLOYEE_ID_PATTERN)]
    display_name: Annotated[str, Field(min_length=1, max_length=100)]
    # repr=False: hashes stay out of logs too.
    pin_hash: Annotated[str, Field(pattern=r"^\$argon2id\$")] | None = Field(
        default=None, repr=False
    )
    """argon2id hash; None: no PIN set, so PIN login is impossible."""
    has_consent: bool = False
    """True only when the latest consent record is granted for the current policy."""
    ppe_profile_override: UUID | None = None
    """Id of the worker's PPE profile; look it up with `ConfigBundle.resolve`."""
    preferred_language: Annotated[str, Field(min_length=2, max_length=16)] | None = None
    is_active: bool = True


class ConsentPolicyInfo(ApiModel):
    """The site's current consent policy text."""

    version: Annotated[int, Field(ge=1)]
    language: Annotated[str, Field(min_length=2, max_length=16)]
    body: Annotated[str, Field(min_length=1)]


class RetentionLocal(ApiModel):
    """Local evidence limits (contract section 6, "Limits")."""

    max_age_days: Annotated[int, Field(ge=1, le=365)] = 30
    max_disk_mb: Annotated[int, Field(ge=1)] = 5120


class ProfileThresholds(ApiModel):
    """A PPE profile and its fully resolved thresholds for this workstation.

    Resolved on the server like the station-level `thresholds`, with this
    profile as the PPE layer: defaults -> site -> this profile -> shift ->
    workstation.
    """

    ppe_profile_id: UUID
    ppe_profile: PpeProfile
    thresholds: Thresholds
    shift_thresholds: tuple[ShiftThresholds, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """The profile and thresholds to run for one worker and shift."""

    profile: PpeProfile
    thresholds: Thresholds
    shift_thresholds: tuple[ShiftThresholds, ...]
    """The shift table of the chosen profile (for building an `EngineConfig`)."""
    ppe_profile_id: UUID | None
    """The override id actually used; None for the station profile."""


class ConfigBundle(ApiModel):
    """`GET /config` response body."""

    version: NonNegativeInt
    generated_at: UtcDatetime
    site: SiteInfo
    workstation: WorkstationInfo
    cameras: Annotated[tuple[CameraConfig, ...], Field(min_length=1, max_length=4)]
    ppe_profile: PpeProfile
    """The station's profile."""
    thresholds: Thresholds
    """Fully resolved for the station profile, outside any shift-specific entry."""
    shift_thresholds: tuple[ShiftThresholds, ...] = ()
    profile_thresholds: tuple[ProfileThresholds, ...] = ()
    """Per-profile resolution for roster overrides (additive, schema revision 1)."""
    shifts: tuple[Shift, ...] = ()
    roster: tuple[RosterEntry, ...] = ()
    consent_policy: ConsentPolicyInfo | None = None
    capture: CaptureConfig = CaptureConfig()
    retention_local: RetentionLocal = RetentionLocal()
    target_app_version: Annotated[str, Field(max_length=64)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _ignore_unknown(cls, data: Any) -> Any:  # Any: raw input of a before-validator
        return strip_unknown_fields(cls, data)

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [p.ppe_profile_id for p in self.profile_thresholds]
        if len(set(ids)) != len(ids):
            raise ValueError("profile_thresholds has a ppe_profile_id twice")
        user_ids = [r.user_id for r in self.roster]
        if len(set(user_ids)) != len(user_ids):
            raise ValueError("roster has a user_id twice")
        employee_ids = [r.employee_id.casefold() for r in self.roster]
        if len(set(employee_ids)) != len(employee_ids):
            raise ValueError("roster has an employee_id twice")
        # Building every engine config checks cameras (one primary, unique
        # slots) and that each shift table only names known shifts.
        self.engine_config()
        for entry in self.profile_thresholds:
            self.engine_config(entry.ppe_profile_id)
        return self

    def resolve(self, ppe_profile_id: UUID | None, shift_id: UUID | None = None) -> ResolvedProfile:
        """Profile and thresholds for a worker's override and the active shift.

        Fallback order:

        1. `ppe_profile_id` is set and listed in `profile_thresholds`: that
           profile; its `shift_thresholds` entry for `shift_id`, else its
           `thresholds`.
        2. Otherwise (no override, or an override this bundle does not carry,
           e.g. from a server older than the change): the station profile;
           the station `shift_thresholds` entry for `shift_id`, else the
           station `thresholds`. A missing override is logged.
        """
        if ppe_profile_id is not None:
            for entry in self.profile_thresholds:
                if entry.ppe_profile_id == ppe_profile_id:
                    return ResolvedProfile(
                        profile=entry.ppe_profile,
                        thresholds=_for_shift(entry.shift_thresholds, shift_id, entry.thresholds),
                        shift_thresholds=entry.shift_thresholds,
                        ppe_profile_id=ppe_profile_id,
                    )
            logger.warning(
                "PPE override %s is not in config bundle %s; using the station profile",
                ppe_profile_id,
                self.version,
            )
        return ResolvedProfile(
            profile=self.ppe_profile,
            thresholds=_for_shift(self.shift_thresholds, shift_id, self.thresholds),
            shift_thresholds=self.shift_thresholds,
            ppe_profile_id=None,
        )

    def engine_config(self, ppe_profile_id: UUID | None = None) -> EngineConfig:
        """The engine config for the station, or for a worker's PPE override.

        Raises `pydantic.ValidationError` if the bundle's parts do not fit.
        """
        resolved = self.resolve(ppe_profile_id)
        return EngineConfig(
            cameras=self.cameras,
            profile=resolved.profile,
            thresholds=resolved.thresholds,
            shift_thresholds=resolved.shift_thresholds,
            shifts=self.shifts,
            capture=self.capture,
            timezone=self.site.timezone,
            config_version=self.version,
        )

    def roster_entry(self, employee_id: str) -> RosterEntry | None:
        """The roster entry for `employee_id` (case-insensitive), if any."""
        key = employee_id.casefold()
        return next((r for r in self.roster if r.employee_id.casefold() == key), None)


def _for_shift(
    table: tuple[ShiftThresholds, ...], shift_id: UUID | None, default: Thresholds
) -> Thresholds:
    if shift_id is not None:
        for entry in table:
            if entry.shift_id == shift_id:
                return entry.thresholds
    return default
