"""The sync client's background thread in the service parent.

The thread is a daemon and owns all network I/O, so a hung server (or a
hung TLS handshake) can delay sync but never the supervisor tick, the engine
child or the UI's scan path, which do not wait on it. `stop()` gives the
thread a bounded time to finish its current request and then abandons it
(Python cannot kill a thread; the daemon flag lets the process exit).
"""

import logging
import threading
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger(__name__)


class Tickable(Protocol):
    """One round of work; returns seconds the caller may sleep. Must not raise."""

    def tick(self) -> float:
        """Run once."""
        ...


class SyncRunner:
    """Calls `client.tick()` until stopped."""

    def __init__(
        self,
        client: Tickable,
        *,
        min_sleep_s: float = 0.05,
        on_crash: Callable[[Exception], None] | None = None,
    ) -> None:
        self._client = client
        self._min_sleep_s = min_sleep_s
        self._on_crash = on_crash
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rounds = 0

    @property
    def running(self) -> bool:
        """Whether the thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the thread (once)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ag-sync", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sleep = self._client.tick()
            except Exception as exc:  # tick() must not raise; keep the loop alive anyway
                logger.exception("sync tick raised (%s)", type(exc).__name__)
                if self._on_crash is not None:
                    self._on_crash(exc)
                sleep = 1.0
            self.rounds += 1
            self._stop.wait(max(self._min_sleep_s, sleep))

    def stop(self, timeout: float = 2.0) -> bool:
        """Ask the thread to stop; True if it ended within `timeout`."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return True
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("sync thread still busy after %.1f s; abandoned", timeout)
            return False
        return True
