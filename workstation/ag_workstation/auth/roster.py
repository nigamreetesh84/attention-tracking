"""Where login reads the roster: the currently applied config bundle.

The bundle is cached on the device, so login works offline with the last
roster the station received (`api-contract.md` section 6, "Offline login").
"""

import threading
from typing import Protocol
from uuid import UUID

from ag_shared.api import ConfigBundle, RosterEntry


class RosterSource(Protocol):
    """The currently applied config bundle (roster, PPE profiles, thresholds)."""

    def current_bundle(self) -> ConfigBundle | None:
        """The applied bundle, or None before the first bundle. Must not block or raise."""
        ...


class StaticRosterSource:
    """A `RosterSource` holding one bundle; the config applier replaces it. Thread-safe."""

    def __init__(self, bundle: ConfigBundle | None = None) -> None:
        self._lock = threading.Lock()
        self._bundle = bundle

    def current_bundle(self) -> ConfigBundle | None:
        """The bundle last set."""
        with self._lock:
            return self._bundle

    def set_bundle(self, bundle: ConfigBundle | None) -> None:
        """Replace the bundle (after the config applier accepted a new one)."""
        with self._lock:
            self._bundle = bundle


def roster_entry_by_user(bundle: ConfigBundle, user_id: UUID) -> RosterEntry | None:
    """The roster entry for `user_id`, if the bundle lists it."""
    return next((r for r in bundle.roster if r.user_id == user_id), None)
