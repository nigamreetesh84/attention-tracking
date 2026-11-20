"""Building blocks for the device API payload models (`docs/api-contract.md`).

Wire rules implemented here (contract section 2):

- Both sides ignore unknown fields: every payload model derives from
  `ApiModel` (`extra="ignore"`). Shared domain models that forbid unknown
  keys on purpose (thresholds, PPE profiles, camera config) are embedded
  through `strip_unknown_fields`, which drops unknown keys before they are
  validated.
- Enums may gain values: `lenient_enum` maps an unknown value to a safe
  default and logs it; `drop_unknown_enum_keys` drops unknown map keys.
- Timestamps are timezone-aware UTC with millisecond precision
  (`UtcDatetime`).
- Secrets are `WireSecret`: masked in `repr` and in Python-mode dumps, sent
  in clear only in JSON (which goes over TLS).
"""

import logging
import types
import typing
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Final, TypeVar, get_args, get_origin

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
)

logger = logging.getLogger("ag_shared.api")

SCHEMA_REVISION: Final = 1
"""Payload schema revision within API major version 1 (header `X-AG-Schema`).

Bump it for every change to an API model's JSON schema; the snapshot test in
`tests/shared/test_api_schema_snapshot.py` enforces this. Queue records store
the revision they were written with."""

API_PREFIX: Final = "/api/v1/device"
HEADER_SCHEMA: Final = "X-AG-Schema"
HEADER_APP_VERSION: Final = "X-AG-App-Version"
HEADER_AUTHORIZATION: Final = "Authorization"


class ApiModel(BaseModel):
    """Base of every wire payload: immutable, unknown fields ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)


def _to_utc_ms(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware UTC")
    value = value.astimezone(UTC)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _iso_ms(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_to_utc_ms),
    PlainSerializer(_iso_ms, return_type=str, when_used="json"),
]
"""Aware datetime, normalized to UTC and truncated to milliseconds.
Naive input is refused. JSON form: `2026-11-20T12:00:00.123Z`."""

WireSecret = Annotated[
    SecretStr,
    Field(min_length=1),
    PlainSerializer(lambda s: s.get_secret_value(), return_type=str, when_used="json"),
]
"""A credential (enrollment token, device secret, access token)."""

Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
CameraSlot = Annotated[int, Field(ge=1, le=4)]
NonNegativeInt = Annotated[int, Field(ge=0)]

E = TypeVar("E", bound=Enum)


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def lenient_enum(enum_cls: type[E], default: E) -> Callable[[object], object]:
    """A before-validator mapping values `enum_cls` does not know to `default`.

    Used as `Annotated[MyEnum, BeforeValidator(lenient_enum(MyEnum, X))]`.
    Non-string input is passed through so ordinary validation rejects it.
    """
    known = {m.value for m in enum_cls}

    def validate(value: object) -> object:
        if isinstance(value, enum_cls) or not isinstance(value, str) or value in known:
            return value
        logger.warning(
            "unknown %s value %s mapped to %s", enum_cls.__name__, _short(value), default.value
        )
        return default

    return validate


def drop_unknown_enum_keys(enum_cls: type[Enum]) -> Callable[[object], object]:
    """A before-validator dropping mapping keys that `enum_cls` does not know."""

    def validate(value: object) -> object:
        return _drop_keys(enum_cls, value) if isinstance(value, Mapping) else value

    return validate


def _drop_keys(enum_cls: type[Enum], value: Mapping[object, object]) -> dict[object, object]:
    known = {m.value for m in enum_cls}
    kept = {k: v for k, v in value.items() if isinstance(k, enum_cls) or k in known}
    for key in value.keys() - kept.keys():
        logger.warning("unknown %s key %s dropped", enum_cls.__name__, _short(key))
    return kept


def strip_unknown_fields(model: type[BaseModel], data: object) -> object:
    """Recursively drop keys that `model` (and its nested models) do not define.

    This lets strict domain models (`extra="forbid"`) ride inside a payload
    whose sender may be newer. Unknown keys of maps keyed by an enum are
    dropped too. Values are not otherwise touched; validation still runs.
    """
    if not isinstance(data, Mapping):
        return data
    fields = {(info.alias or name): info for name, info in model.model_fields.items()}
    out: dict[object, object] = {}
    for key, value in data.items():
        info = fields.get(key) if isinstance(key, str) else None
        if info is None:
            logger.warning("unknown field %s of %s ignored", _short(key), model.__name__)
            continue
        out[key] = _strip_value(info.annotation, value)
    return out


def _strip_value(annotation: object, value: object) -> object:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return _strip_value(args[0], value)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return strip_unknown_fields(annotation, value)
    if origin in (typing.Union, types.UnionType):
        # Only the unambiguous case (X | None) is handled; that is all the
        # shared models use.
        options = [a for a in args if a is not types.NoneType]
        return _strip_value(options[0], value) if len(options) == 1 else value
    if origin in (tuple, list) and args and isinstance(value, list | tuple):
        return [_strip_value(args[0], item) for item in value]
    if origin is dict and len(args) == 2 and isinstance(value, Mapping):
        key_type, value_type = args
        if isinstance(key_type, type) and issubclass(key_type, Enum):
            value = _drop_keys(key_type, value)
        return {k: _strip_value(value_type, v) for k, v in value.items()}
    return value
