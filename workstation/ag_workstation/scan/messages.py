"""User-facing scan strings (English).

Keys only; the texts live in `ag_shared/i18n/catalogs/en.json`. The
scan screen shows `text(result.message_key)`, never a hard-coded string.
Wording is neutral: a warning is a prompt to refocus, not a reprimand
(CLAUDE.md rule 3).
"""

from enum import StrEnum

from ag_shared.i18n import t


class ScanMessageKey(StrEnum):
    """Catalog keys for scan results."""

    SCAN_OK = "scan.ok"
    SCAN_WARNED = "scan.warned"
    SCAN_WARNED_REPEATED = "scan.warned_repeated"
    SCAN_UNMONITORED = "scan.unmonitored"
    SCAN_ON_BREAK = "scan.on_break"
    SCAN_LOGIN_REQUIRED = "scan.login_required"


def text(key: ScanMessageKey, **values: object) -> str:
    """The text for `key` in the current locale, with `values` filled in."""
    return t(key.value, **values)
