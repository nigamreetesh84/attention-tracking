"""Worker entry point (``python -m ag_server.worker``).

The scheduler jobs (purge, reports, alerts) arrive in later tasks. For now the
worker validates its settings, so it refuses an unsafe production start exactly
like the app, and idles until it is stopped.
"""

from __future__ import annotations

import logging
import signal
import threading

from ag_server.settings import load_settings

log = logging.getLogger("ag_server.worker")


def run(stop: threading.Event | None = None) -> None:
    settings = load_settings()
    stop = stop or threading.Event()
    log.info("worker started in %s mode", settings.env)
    stop.wait()
    log.info("worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _s, _f: stop.set())
    run(stop)


if __name__ == "__main__":
    main()
