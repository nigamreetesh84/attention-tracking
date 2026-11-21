"""Server-corrected UTC shared by the workstation's processes (`api-contract.md` 6, "Clock").

The sync client in the service parent keeps the smoothed server offset
(`sync.clock_offset`). Records are also written by the UI process (scans,
sessions, disputes) and the engine child (episodes, buckets, evidence), so
the parent publishes the offset to a small plain-text file in the data
directory (`clock_offset.json`; an offset is not secret) and the other
processes read it.

Two time bases, on purpose:

- **device time** (`Clock.now()` of the raw clock): everything the
  workstation compares internally. In particular the capture gate compares
  the session start the UI pushes with the burst's first frame in device
  time on both sides, so no change of the offset can open the gate for a
  burst that began before the session (rule 5). Engine timing is monotonic
  and never touched.
- **corrected UTC** (`StampingClock.now()` / `stamp()`): only what is written
  into a record (queue items, evidence metadata).

Fail-open to the raw clock (offset 0): no file, a file that cannot be parsed,
an offset beyond `MAX_APPLIED_OFFSET_MS`, one older than `MAX_OFFSET_AGE_S`,
or one measured before the device clock was stepped or before a reboot
(device wall time and monotonic time disagree since it was written; the
monotonic clock is system-wide on Windows and Linux). A read error (on
Windows a reader can collide with the writer's atomic replace) keeps the
last good value for `HOLD_ON_READ_ERROR_S`.

The file is read at most once per `READ_INTERVAL_S` of monotonic time, so a
scan costs at most one small local read per second; the scan path already
writes the journal and the queue to the same disk.
"""

import contextlib
import json
import logging
import math
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

from ag_workstation.clock import Clock

logger = logging.getLogger(__name__)

OFFSET_FILE: Final = "clock_offset.json"
FORMAT_VERSION: Final = 1
MAX_APPLIED_OFFSET_MS: Final = 24 * 3600 * 1000
"""Larger offsets are not applied (raw clock). A day covers a PC set to the
wrong time zone; anything beyond is a fault, and the drift alert is raised
by the sync client either way."""
MAX_OFFSET_AGE_S: Final = 24 * 3600.0
"""An offset not re-measured for a day (station offline) is dropped."""
STEP_TOLERANCE_S: Final = 2.0
"""Disagreement between device wall time and monotonic time since the offset
was written beyond which the device clock is taken to have been stepped."""
READ_INTERVAL_S: Final = 1.0
HOLD_ON_READ_ERROR_S: Final = 10.0
PUBLISH_MIN_CHANGE_MS: Final = 20
PUBLISH_REFRESH_S: Final = 60.0
MAX_FILE_CHARS: Final = 1024
_TMP_PREFIX: Final = ".tmp-"


def offset_path(data_dir: Path) -> Path:
    """Where the service parent publishes the offset."""
    return data_dir / OFFSET_FILE


class OffsetSource(Protocol):
    """The server-minus-device offset to apply now."""

    def offset_ms(self) -> int:
        """Milliseconds to add to device time; 0 when unknown. Never raises."""
        ...


@dataclass(frozen=True, slots=True)
class PublishedOffset:
    """The file's content: the offset and when (device time) it was written."""

    offset_ms: int
    wall: datetime
    mono: float

    def to_text(self) -> str:
        """The JSON line written to the file."""
        return json.dumps(
            {
                "v": FORMAT_VERSION,
                "offset_ms": self.offset_ms,
                "wall": self.wall.isoformat(),
                "mono": self.mono,
            }
        )

    @staticmethod
    def parse(text: str) -> "PublishedOffset | None":
        """The offset in `text`, or None if it is not a valid file."""
        if len(text) > MAX_FILE_CHARS:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("v") != FORMAT_VERSION:
            return None
        offset, wall, mono = data.get("offset_ms"), data.get("wall"), data.get("mono")
        if not isinstance(offset, int) or isinstance(offset, bool):
            return None
        if not isinstance(mono, int | float) or isinstance(mono, bool) or not math.isfinite(mono):
            return None
        if not isinstance(wall, str):
            return None
        try:
            when = datetime.fromisoformat(wall)
        except ValueError:
            return None
        if when.utcoffset() is None:
            return None
        return PublishedOffset(offset, when, float(mono))

    def applicable_ms(self, mono: float, wall: datetime) -> int | None:
        """The offset if it may be applied at device time (`mono`, `wall`), else None."""
        if abs(self.offset_ms) > MAX_APPLIED_OFFSET_MS:
            return None
        elapsed = mono - self.mono
        if elapsed < -STEP_TOLERANCE_S or elapsed > MAX_OFFSET_AGE_S:
            return None  # another boot, or too old
        wall_elapsed = (wall - self.wall).total_seconds()
        if abs(wall_elapsed - elapsed) > STEP_TOLERANCE_S:
            return None  # the device clock was stepped since it was measured
        return self.offset_ms


class OffsetPublisher:
    """Writes the sync client's offset for the other processes (service parent).

    Called from the sync thread after a round that measured the offset. Never
    raises; a failed write leaves the previous file (or none).
    """

    def __init__(
        self,
        path: Path,
        clock: Clock,
        *,
        min_change_ms: int = PUBLISH_MIN_CHANGE_MS,
        refresh_s: float = PUBLISH_REFRESH_S,
    ) -> None:
        """`clock` is the raw device clock (the anchors must be device time)."""
        self._path = path
        self._clock = clock
        self._min_change = min_change_ms
        self._refresh = refresh_s
        self._last: tuple[int, float] | None = None

    def publish(self, offset_ms: int) -> bool:
        """Write `offset_ms` if it changed or the file is due a refresh; True if written."""
        try:
            mono = self._clock.monotonic()
            last = self._last
            if (
                last is not None
                and abs(offset_ms - last[0]) < self._min_change
                and mono - last[1] < self._refresh
            ):
                return False
            text = PublishedOffset(int(offset_ms), self._clock.now(), mono).to_text()
            self._write(text)
        except Exception as exc:
            logger.warning("clock offset not published (%s)", type(exc).__name__)
            return False
        self._last = (int(offset_ms), mono)
        return True

    def _write(self, text: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.parent / f"{_TMP_PREFIX}{self._path.name}.{secrets.token_hex(4)}"
        try:
            with open(tmp, "x", encoding="ascii", newline="\n") as fh:
                fh.write(text + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


def _read_text(path: Path) -> str:
    with open(path, encoding="ascii") as fh:
        return fh.read(MAX_FILE_CHARS + 1)


class SharedClockOffset:
    """`OffsetSource` reading the published file (UI process and engine child). Thread-safe."""

    def __init__(
        self,
        path: Path | None,
        clock: Clock,
        *,
        read_interval_s: float = READ_INTERVAL_S,
        read_text: Callable[[Path], str] | None = None,
    ) -> None:
        """`path` None: no data directory, always the raw clock."""
        self._path = path
        self._clock = clock
        self._interval = read_interval_s
        self._read = read_text or _read_text
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._held: PublishedOffset | None = None
        self._error_since: float | None = None
        self._value = 0
        self._logged: str | None = None

    def offset_ms(self) -> int:
        """The offset to apply now; 0 (raw clock) when none applies. Never raises."""
        if self._path is None:
            return 0
        try:
            with self._lock:
                mono = self._clock.monotonic()
                if self._checked_at is None or mono - self._checked_at >= self._interval:
                    self._checked_at = mono
                    self._refresh(self._path, mono)
                return self._value
        except Exception as exc:
            logger.error("clock offset unavailable (%s); raw clock", type(exc).__name__)
            return 0

    def _refresh(self, path: Path, mono: float) -> None:
        try:
            text = self._read(path)
        except FileNotFoundError:
            self._drop("no clock offset published; raw clock")
            return
        except OSError as exc:
            if self._error_since is None:
                self._error_since = mono
            if self._held is not None and mono - self._error_since < HOLD_ON_READ_ERROR_S:
                self._apply(mono)
                return
            self._drop(f"clock offset unreadable ({type(exc).__name__}); raw clock")
            return
        self._error_since = None
        parsed = PublishedOffset.parse(text)
        if parsed is None:
            self._drop("clock offset file invalid; raw clock")
            return
        self._held = parsed
        self._apply(mono)

    def _apply(self, mono: float) -> None:
        held = self._held
        value = None if held is None else held.applicable_ms(mono, self._clock.now())
        if value is None:
            self._value = 0
            self._note("clock offset not applicable (bounds, age or clock step); raw clock")
        else:
            self._value = value
            self._note(None)

    def _drop(self, why: str) -> None:
        self._held = None
        self._value = 0
        self._note(why)

    def _note(self, why: str | None) -> None:
        # Log changes only: this runs once a second.
        if why != self._logged:
            if why is not None:
                logger.warning(why)
            else:
                logger.info("server clock offset applied to records")
            self._logged = why


class StampingClock:
    """A `Clock` whose `now()` is corrected UTC, for stamping records.

    `monotonic()` is the base clock's (intervals are never shifted); `raw`
    is the device clock for anything compared inside the workstation.
    """

    def __init__(self, base: Clock, offset: OffsetSource) -> None:
        self._base = base
        self._offset = offset

    @property
    def raw(self) -> Clock:
        """The device clock."""
        return self._base

    def monotonic(self) -> float:
        """The base clock's monotonic seconds."""
        return self._base.monotonic()

    def now(self) -> datetime:
        """Device UTC plus the server offset."""
        return self._base.now() + timedelta(milliseconds=self._offset.offset_ms())

    def stamp(self, device_time: datetime) -> datetime:
        """`device_time` (a device-clock UTC time) in corrected UTC."""
        return device_time + timedelta(milliseconds=self._offset.offset_ms())


def stamping_clock(base: Clock, data_dir: Path | None) -> StampingClock:
    """Corrected UTC from the offset published under `data_dir` (raw if None)."""
    path = offset_path(data_dir) if data_dir is not None else None
    return StampingClock(base, SharedClockOffset(path, base))
