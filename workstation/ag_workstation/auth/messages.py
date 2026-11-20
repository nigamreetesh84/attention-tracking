"""User-facing login strings (English).

Keys only; the texts live in `ag_shared/i18n/catalogs/en.json`. The
UI shows `text(result.message_key, ...)`, never a hard-coded string. A failed
login has one message whatever the cause (unknown ID, wrong PIN, inactive
worker), so the screen cannot be used to find valid employee IDs.
"""

from enum import StrEnum

from ag_shared.i18n import t


class MessageKey(StrEnum):
    """Catalog keys for login results."""

    LOGIN_OK = "login.ok"
    LOGIN_FAILED = "login.failed"
    LOGIN_LOCKED = "login.locked"
    LOGIN_INVALID_INPUT = "login.invalid_input"
    LOGIN_UNAVAILABLE = "login.unavailable"


def text(key: MessageKey, **values: object) -> str:
    """The text for `key` in the current locale, with `values` filled in."""
    return t(key.value, **values)
