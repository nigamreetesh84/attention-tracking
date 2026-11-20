"""Label files of the test video set (`docs/detection-engine.md` section 9).

Each video (or signal trace) has a sidecar JSON label file:

    { "video": "ws1_cam1_0007.mp4", "fps": 15, "camera_role": "primary",
      "ppe_profile": "open_face", "session": "ws1_0007",
      "tags": {"glasses": false, "lighting": "low", "age_band": "30-45",
               "skin_tone_group": "IV-VI"},
      "intervals": [{"start_s": 0.0, "end_s": 41.2, "state": "attentive"}, ...] }

`session` pairs the recordings of one sitting from several cameras. Labels
never name or identify the person; tags are coarse groups for fairness.
"""

import itertools
import json
from pathlib import Path
from typing import Annotated, Self

from ag_shared.config import CameraRole
from ag_shared.ppe import PpeProfileKind
from ag_shared.states import AttentionState
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

LABEL_SUFFIX = ".labels.json"

_Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]

_LABEL_STATES = frozenset(AttentionState) - {AttentionState.ON_BREAK}


class LabelInterval(BaseModel):
    """One labelled stretch of time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_s: _Seconds
    end_s: _Seconds
    state: AttentionState

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("an interval must end after it starts")
        if self.state not in _LABEL_STATES:
            raise ValueError("on_break is not a labelled state; cut breaks out of the video")
        return self


TagValue = StrictBool | str

ALLOWED_TAGS: dict[str, frozenset[str] | type[bool]] = {
    "glasses": bool,
    "ir_camera": bool,
    "lighting": frozenset({"low", "normal", "bright", "backlit"}),
    "age_band": frozenset({"18-30", "30-45", "45-60", "60+"}),
    "skin_tone_group": frozenset({"I-III", "IV-VI"}),
    "facial_hair": frozenset({"none", "light", "full"}),
}
"""Coarse fairness groups only. A fixed vocabulary keeps names, employee ids
or free text out of labels, and so out of the committed baseline."""


class LabelFile(BaseModel):
    """The ground truth of one recording."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    video: Annotated[str, Field(min_length=1)]
    """File name next to the label file (a video or a `.trace.jsonl`)."""
    fps: Annotated[float, Field(gt=0, le=120)]
    camera_role: CameraRole = CameraRole.PRIMARY
    camera_slot: Annotated[int, Field(ge=1, le=4)] | None = None
    """Defaults to 1 for the primary and 2 for a secondary."""
    ppe_profile: PpeProfileKind = PpeProfileKind.OPEN_FACE
    session: str | None = None
    tags: dict[str, TagValue] = {}
    intervals: Annotated[tuple[LabelInterval, ...], Field(min_length=1)]

    @field_validator("video")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        # The label points next to itself; no paths, so a label cannot reach elsewhere.
        if Path(value).name != value or value in (".", ".."):
            raise ValueError("video must be a plain file name")
        return value

    @field_validator("tags")
    @classmethod
    def _coarse_tags(cls, value: dict[str, TagValue]) -> dict[str, TagValue]:
        for key, tag in value.items():
            allowed = ALLOWED_TAGS.get(key)
            if allowed is None:
                raise ValueError(f"tag {key!r} is not allowed; use {sorted(ALLOWED_TAGS)}")
            if allowed is bool:
                if not isinstance(tag, bool):
                    raise ValueError(f"tag {key!r} must be true or false")
            elif (
                not isinstance(tag, str) or not isinstance(allowed, frozenset) or tag not in allowed
            ):
                raise ValueError(f"tag {key!r} must be one of {sorted(allowed)}")
        return value

    @field_validator("intervals")
    @classmethod
    def _ordered(cls, value: tuple[LabelInterval, ...]) -> tuple[LabelInterval, ...]:
        for a, b in itertools.pairwise(value):
            if b.start_s < a.end_s - 1e-9:
                raise ValueError("intervals must be in order and must not overlap")
        return value

    @property
    def slot(self) -> int:
        """The camera slot this recording replays as."""
        if self.camera_slot is not None:
            return self.camera_slot
        return 1 if self.camera_role is CameraRole.PRIMARY else 2

    @property
    def duration_s(self) -> float:
        """End of the last labelled interval."""
        return self.intervals[-1].end_s

    def state_at(self, t: float) -> AttentionState | None:
        """The labelled state at `t`, None outside every interval."""
        for interval in self.intervals:
            if interval.start_s <= t < interval.end_s:
                return interval.state
        return None


def load_label(path: Path) -> LabelFile:
    """Read and validate one label file."""
    return LabelFile.model_validate(json.loads(path.read_text(encoding="utf-8")))


def find_labels(directory: Path) -> list[tuple[Path, LabelFile]]:
    """Every `*.labels.json` under `directory`, sorted by path."""
    return [(p, load_label(p)) for p in sorted(directory.rglob(f"*{LABEL_SUFFIX}"))]
