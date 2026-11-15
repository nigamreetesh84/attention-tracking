"""One camera's capture loop: connect, read, reconnect with backoff.

Every failure (factory, open, read, close, a hang) is caught here and becomes
the `offline` status. Nothing raises out of this layer, and one camera's
problems never touch another camera's thread.
"""

import logging
import math
import threading
from collections import deque

from ag_workstation.capture.backoff import Backoff
from ag_workstation.capture.latest import LatestFrame
from ag_workstation.capture.types import (
    CameraHealth,
    CameraSource,
    CameraStatus,
    EndOfStream,
    Frame,
    Image,
    SourceFactory,
)
from ag_workstation.clock import Clock, SystemClock

log = logging.getLogger(__name__)

_FPS_WINDOW = 30


def _safe_close(source: CameraSource | None, slot: int) -> None:
    if source is None:
        return
    try:
        source.close()
    except Exception as exc:  # a broken driver must not escape the worker
        log.debug("camera slot %d: close failed (%s)", slot, type(exc).__name__)


class CameraWorker:
    """Captures from one camera into a `LatestFrame` slot.

    Drive it on its own thread with `start`/`stop`, or call `step` directly
    (tests, replay). Connection attempts use a fresh source from
    `source_factory` each time. A blocking `open` or `read` longer than
    `stale_after_s` reports `offline` (`Hung`); `restart_if_hung` then
    abandons that thread and starts a new one (at most `max_abandoned` hung
    threads are kept around, so a dead driver cannot leak threads).

    Logs carry the slot and the exception type only, never a URI.
    """

    def __init__(
        self,
        slot: int,
        source_factory: SourceFactory,
        *,
        clock: Clock | None = None,
        backoff: Backoff | None = None,
        stale_after_s: float = 5.0,
        max_abandoned: int = 2,
    ) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        self.slot = slot
        self.frames = LatestFrame()
        self._factory = source_factory
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._backoff = backoff if backoff is not None else Backoff()
        self._stale_after = stale_after_s
        self._max_abandoned = max_abandoned

        self._lock = threading.Lock()
        self._gen = 0
        self._source: CameraSource | None = None
        self._next_attempt = -math.inf
        self._busy_since: float | None = None
        self._status = CameraStatus.STARTING
        self._ended = False
        self._seq = 0
        self._recent: deque[float] = deque(maxlen=_FPS_WINDOW)
        self._failures = 0
        self._reconnects = 0
        self._last_error: str | None = None
        self._last_frame_ts: float | None = None
        self._offline_since: float | None = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._abandoned: list[threading.Thread] = []

    # --- stepping ----------------------------------------------------------

    def step(self) -> float | None:
        """Do one unit of work: an attempt to connect and/or one read.

        Returns the seconds to wait before the next step (0 after a frame),
        or None when this worker has finished (end of stream or stopped).
        """
        with self._lock:
            gen = self._gen
        return self._step(gen)

    def _step(self, gen: int) -> float | None:
        with self._lock:
            if gen != self._gen or self._ended:
                return None
            source = self._source
            now = self._clock.monotonic()
            if source is None and now < self._next_attempt:
                return self._next_attempt - now
            self._busy_since = now
        try:
            if source is None:
                source = self._factory()
                source.open()
                with self._lock:
                    if gen == self._gen:
                        self._source = source
            image = source.read()
        except EndOfStream:
            self._end(gen, source)
            return None
        except Exception as exc:  # boundary: any failure means offline, never a raise
            return self._fail(gen, source, type(exc).__name__)
        if image is None:
            return self._fail(gen, source, "NoFrame")
        return self._deliver(gen, source, image)

    def _deliver(self, gen: int, source: CameraSource, image: Image) -> float | None:
        with self._lock:
            if gen != self._gen:
                stale = True
            else:
                stale = False
                now = self._clock.monotonic()
                self._busy_since = None
                if self._status is CameraStatus.OFFLINE:
                    self._reconnects += 1
                    log.info("camera slot %d online", self.slot)
                self._status = CameraStatus.ONLINE
                self._offline_since = None
                self._backoff.reset()
                self._seq += 1
                self._recent.append(now)
                self._last_frame_ts = now
                frame = Frame(
                    camera_slot=self.slot,
                    seq=self._seq,
                    ts=now,
                    captured_at=self._clock.now(),
                    image=image,
                )
        if stale:
            # This thread was abandoned while blocked; drop its frame and device.
            _safe_close(source, self.slot)
            return None
        self.frames.put(frame)
        return 0.0

    def _fail(self, gen: int, source: CameraSource | None, error: str) -> float | None:
        _safe_close(source, self.slot)
        with self._lock:
            if gen != self._gen:
                return None
            now = self._clock.monotonic()
            self._busy_since = None
            self._source = None
            if self._status is not CameraStatus.OFFLINE:
                log.warning("camera slot %d offline (%s)", self.slot, error)
                self._status = CameraStatus.OFFLINE
            else:
                log.debug("camera slot %d still offline (%s)", self.slot, error)
            if self._offline_since is None:
                self._offline_since = now
            self._failures += 1
            self._last_error = error
            delay = self._backoff.next_delay()
            self._next_attempt = now + delay
            return delay

    def _end(self, gen: int, source: CameraSource | None) -> None:
        _safe_close(source, self.slot)
        with self._lock:
            if gen == self._gen:
                self._busy_since = None
                self._source = None
                self._ended = True
                self._status = CameraStatus.STOPPED
                log.info("camera slot %d stream ended", self.slot)
        return None

    # --- health ------------------------------------------------------------

    def health(self) -> CameraHealth:
        """A snapshot of status and counters, with hangs and staleness applied."""
        with self._lock:
            now = self._clock.monotonic()
            status = self._status
            error = self._last_error
            offline_since = self._offline_since
            live = status is not CameraStatus.STOPPED
            if live and self._busy_since is not None and now - self._busy_since > self._stale_after:
                status, error = CameraStatus.OFFLINE, "Hung"
                if offline_since is None:
                    offline_since = self._busy_since
            elif (
                status is CameraStatus.ONLINE
                and self._last_frame_ts is not None
                and now - self._last_frame_ts > self._stale_after
            ):
                status, error = CameraStatus.OFFLINE, "Stale"
                offline_since = self._last_frame_ts
            fps = 0.0
            if status is CameraStatus.ONLINE and len(self._recent) >= 2:
                span = self._recent[-1] - self._recent[0]
                if span > 0:
                    fps = (len(self._recent) - 1) / span
            return CameraHealth(
                slot=self.slot,
                status=status,
                fps=fps,
                frames_captured=self._seq,
                frames_dropped=self.frames.dropped,
                failures=self._failures,
                reconnects=self._reconnects,
                last_error=error,
                last_frame_ts=self._last_frame_ts,
                offline_since=offline_since,
            )

    # --- threading ---------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """A capture thread is active and not asked to stop."""
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        """Start the capture thread (no-op when already running)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._spawn(self._gen)

    def _spawn(self, gen: int) -> None:
        # Caller holds self._lock.
        self._thread = threading.Thread(
            target=self._run, args=(gen,), name=f"camera-{self.slot}-{gen}", daemon=True
        )
        self._thread.start()

    def _run(self, gen: int) -> None:
        while not self._stop.is_set():
            try:
                delay = self._step(gen)
            except Exception as exc:  # defensive: _step handles its own errors
                log.error("camera slot %d loop error (%s)", self.slot, type(exc).__name__)
                delay = 1.0
            if delay is None:
                return
            if delay > 0:
                self._stop.wait(delay)

    def restart_if_hung(self) -> bool:
        """Abandon a thread stuck in `open`/`read` and start a fresh one.

        Returns True when a restart happened. The abandoned thread closes its
        source and exits as soon as its blocked call returns.
        """
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive() or self._stop.is_set():
                return False
            now = self._clock.monotonic()
            if self._busy_since is None or now - self._busy_since <= self._stale_after:
                return False
            self._abandoned = [t for t in self._abandoned if t.is_alive()]
            if len(self._abandoned) >= self._max_abandoned:
                return False
            self._abandoned.append(thread)
            self._gen += 1
            # The abandoned thread still owns this source; it closes it itself.
            self._source = None
            if self._offline_since is None:
                self._offline_since = self._busy_since
            self._busy_since = None
            self._status = CameraStatus.OFFLINE
            self._failures += 1
            self._last_error = "Hung"
            self._next_attempt = now + self._backoff.next_delay()
            log.warning("camera slot %d hung; restarting capture", self.slot)
            self._spawn(self._gen)
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop capturing and release the device. Never raises."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            self._gen += 1
            source = self._source
            self._source = None
            self._busy_since = None
            self._status = CameraStatus.STOPPED
            # A thread still blocked owns the source and closes it on return.
            owned_elsewhere = thread is not None and thread.is_alive()
        if not owned_elsewhere:
            _safe_close(source, self.slot)
