"""Site settings (`site.settings` JSONB, `docs/schema.md`).

Carried to workstations in the config bundle. Release 1 refuses
`fail_open = false` and `blocking_mode = true` (CLAUDE.md rule 1 and 2), so a
bundle that tries to enable either is invalid and the previous one stays.
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BeforeValidator, Field, model_validator

from ag_shared.api.common import ApiModel, lenient_enum


class DeploymentMode(StrEnum):
    """Where the server runs."""

    ON_PREMISE = "on_premise"
    PRIVATE_CLOUD = "private_cloud"
    HYBRID = "hybrid"


_Days = Annotated[int, Field(ge=1, le=3650)]


class SiteSettings(ApiModel):
    """Per-site settings with the schema defaults. Unknown keys are ignored."""

    retention_image_days: _Days = 30
    retention_metadata_days: _Days = 365
    retention_audit_days: Annotated[int, Field(ge=365, le=3650)] = 365
    fail_open: bool = True
    blocking_mode: bool = False
    live_video_enabled: bool = False
    pin_required: bool = True
    escalation_count: Annotated[int, Field(ge=1, le=100)] = 3
    escalation_window_min: Annotated[int, Field(ge=1, le=1440)] = 15
    deployment_mode: Annotated[
        DeploymentMode,
        BeforeValidator(lenient_enum(DeploymentMode, DeploymentMode.ON_PREMISE)),
    ] = DeploymentMode.ON_PREMISE
    default_language: Annotated[str, Field(min_length=2, max_length=16)] = "en"

    @model_validator(mode="after")
    def _release_one_rules(self) -> Self:
        if not self.fail_open:
            raise ValueError("release 1 requires fail_open = true")
        if self.blocking_mode:
            raise ValueError("release 1 refuses blocking_mode = true")
        return self
