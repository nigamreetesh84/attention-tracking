"""UI-process side of sync: new config bundles and session commands.

The sync client in the service parent writes the last good bundle and the
session-command inbox; `SyncConsumer` polls both on a daemon thread in the
UI process, never on the scan path:

- a new bundle replaces the roster, is pushed to the engine child as the
  station config, and the session manager re-applies the open session's PPE
  override (`on_bundle`);
- pending `assign_worker` / `end_session` commands go to the session manager
  (`CommandPump`), and the sync client acknowledges them afterwards.
"""

import logging
import threading
from collections.abc import Callable
from typing import Final, Protocol

from ag_shared.api import ConfigBundle

from ag_workstation.sync.commands import CommandPump
from ag_workstation.sync.config_store import BundleFileWatcher

logger = logging.getLogger(__name__)

POLL_INTERVAL_S: Final = 1.0


class BundleTarget(Protocol):
    """`StaticRosterSource.set_bundle`."""

    def set_bundle(self, bundle: ConfigBundle | None) -> None:
        """Install a bundle."""
        ...


class SyncConsumer:
    """Polls the bundle file and the command inbox."""

    def __init__(
        self,
        *,
        watcher: BundleFileWatcher | None,
        roster: BundleTarget,
        pump: CommandPump | None,
        on_bundle: Callable[[ConfigBundle], None] | None = None,
        interval_s: float = POLL_INTERVAL_S,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._watcher = watcher
        self._roster = roster
        self._pump = pump
        self._on_bundle = on_bundle
        self._interval_s = interval_s
        self._on_close = on_close
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        """One round. Never raises."""
        if self._watcher is not None:
            try:
                bundle = self._watcher.poll()
                if bundle is not None:
                    self._roster.set_bundle(bundle)
                    logger.info("config bundle %d installed", bundle.version)
                    if self._on_bundle is not None:
                        self._on_bundle(bundle)
            except Exception as exc:
                logger.error("bundle update failed (%s)", type(exc).__name__)
        if self._pump is not None:
            try:
                self._pump.poll()
            except Exception as exc:
                logger.error("command pump failed (%s)", type(exc).__name__)

    def start(self) -> None:
        """Start polling on a daemon thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ag-sync-consumer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.poll_once()

    def close(self, timeout: float = 1.0) -> None:
        """Stop polling."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        if self._on_close is not None:
            close, self._on_close = self._on_close, None
            close()
