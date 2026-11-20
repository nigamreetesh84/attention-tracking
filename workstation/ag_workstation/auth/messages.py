"""User-facing login strings (English).

Kept in one module until the string catalog (PLAN 2.7) takes them over: the
UI shows `text(result.message_key, ...)`, never a hard-coded string. A failed
login has one message whatever the cause (unknown ID, wrong PIN, inactive
worker), so the screen cannot be used to find valid employee IDs.
"""

from enum import StrEnum
from typing import Final


class MessageKey(StrEnum):
    """Catalog keys for login results."""

    LOGIN_OK = "login.ok"
    LOGIN_FAILED = "login.failed"
    LOGIN_LOCKED = "login.locked"
    LOGIN_INVALID_INPUT = "login.invalid_input"
    LOGIN_UNAVAILABLE = "login.unavailable"


ENGLISH: Final[dict[MessageKey, str]] = {
    MessageKey.LOGIN_OK: "Welcome, {name}.",
    MessageKey.LOGIN_FAILED: "Employee ID or PIN is not correct.",
    MessageKey.LOGIN_LOCKED: "Too many attempts. Try again in {minutes} min.",
    MessageKey.LOGIN_INVALID_INPUT: (
        "Enter your employee ID (letters and digits, up to 16) and your 4-6 digit PIN."
    ),
    MessageKey.LOGIN_UNAVAILABLE: "Login is not available right now. Ask your supervisor.",
}


def text(key: MessageKey, **values: object) -> str:
    """The English text for `key` with `values` filled in."""
    return ENGLISH[key].format(**values)
