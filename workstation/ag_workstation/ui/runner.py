"""Where presenters run slow or possibly hanging work: never on the UI thread.

`ThreadRunner` runs jobs in order on one daemon thread (a hung job keeps that
thread, never the UI, and a daemon thread cannot keep the process alive).
`InlineRunner` runs a job at once, for tests.
"""

import logging
import queue
import threading
from collections.abc import Callable
from typing import Final, Protocol

logger = logging.getLogger(__name__)

Job = Callable[[], None]


class Runner(Protocol):
    """Runs jobs off the caller's thread (or inline in tests)."""

    def submit(self, job: Job) -> None:
        """Queue `job`. Raises `RuntimeError` if the runner is closed."""
        ...


def _run(job: Job) -> None:
    try:
        job()
    except Exception as exc:
        logger.error("ui job failed: %s", type(exc).__name__)


class InlineRunner:
    """Runs each job immediately on the calling thread."""

    def submit(self, job: Job) -> None:
        """Run `job` now; its exceptions are logged, not raised."""
        _run(job)


_STOP: Final = object()


class ThreadRunner:
    """Runs jobs in submission order on one daemon thread."""

    def __init__(self, name: str) -> None:
        self._jobs: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name=f"ag-ui-{name}", daemon=True)
        self._thread.start()

    def submit(self, job: Job) -> None:
        """Queue `job`."""
        with self._lock:
            if self._closed:
                raise RuntimeError("runner is closed")
            self._jobs.put(job)

    def close(self, timeout: float = 0.5) -> None:
        """Stop after the queued jobs; waits at most `timeout` (a hung job is left behind)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._jobs.put(_STOP)
        self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is _STOP:
                return
            if callable(job):
                _run(job)
