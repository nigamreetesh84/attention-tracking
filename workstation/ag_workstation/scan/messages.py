"""User-facing scan strings (English).

Kept in one module until the string catalog (PLAN 2.7) takes them over: the
scan screen shows `text(result.message_key)`, never a hard-coded string.
Wording is neutral: a warning is a prompt to refocus, not a reprimand
(CLAUDE.md rule 3).
"""

from enum import StrEnum
from typing import Final


class ScanMessageKey(StrEnum):
    """Catalog keys for scan results."""

    SCAN_OK = "scan.ok"
    SCAN_WARNED = "scan.warned"
    SCAN_WARNED_REPEATED = "scan.warned_repeated"
    SCAN_UNMONITORED = "scan.unmonitored"
    SCAN_ON_BREAK = "scan.on_break"
    SCAN_LOGIN_REQUIRED = "scan.login_required"


ENGLISH: Final[dict[ScanMessageKey, str]] = {
    ScanMessageKey.SCAN_OK: "Scan recorded.",
    ScanMessageKey.SCAN_WARNED: "Scan recorded. Please stay focused on your work.",
    ScanMessageKey.SCAN_WARNED_REPEATED: (
        "Scan recorded. Several alerts in a short time: your supervisor will check in."
    ),
    ScanMessageKey.SCAN_UNMONITORED: "Scan recorded. Monitoring is not available right now.",
    ScanMessageKey.SCAN_ON_BREAK: "Scan recorded during a break.",
    ScanMessageKey.SCAN_LOGIN_REQUIRED: "Scan recorded. Please log in with your employee ID.",
}


def text(key: ScanMessageKey, **values: object) -> str:
    """The English text for `key` with `values` filled in."""
    return ENGLISH[key].format(**values)
