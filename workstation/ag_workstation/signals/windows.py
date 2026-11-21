"""Signal state that spans frames: baseline, windows and pattern detectors.

Each object is fed once per frame with the frame's timestamp (seconds on the
engine clock) and a measurement, or None when the frame gave none. They hold
numbers only and never read a clock, so they are deterministic in replay.
Timestamps must not go backwards; `SignalExtractor` resets them when they do.
"""

import math
from collections import deque
from collections.abc import Mapping

from ag_shared.signals import SignalReading

from ag_workstation.landmarks.types import Point
from ag_workstation.signals.geometry import clamp
from ag_workstation.signals.params import ExtractionParams

_EPS = 1e-9
"""Slack for comparing sums of float timestamps against a limit."""


class EarBaseline:
    """The per-session open-eye EAR that raw EAR is normalized against.

    Warm-up: the 90th percentile of the session's first valid samples, so
    blinks do not pull it down. Afterwards it follows open-eye frames only
    (quickly up, slowly down); closed eyes, however long, never lower it,
    otherwise a sleeping worker would become the new "open".
    """

    def __init__(self, params: ExtractionParams) -> None:
        self._p = params
        self._samples: list[float] = []
        self._start: float | None = None
        self._warm = False
        self._value = params.ear_baseline_max

    @property
    def value(self) -> float:
        """The current baseline (raw EAR units)."""
        return self._value

    def reset(self) -> None:
        """Start a new session."""
        self._samples.clear()
        self._start = None
        self._warm = False
        self._value = self._p.ear_baseline_max

    def normalize(self, ts: float, raw_ear: float) -> float:
        """Update the baseline with `raw_ear` and return raw / baseline."""
        p = self._p
        if not self._warm:
            if self._start is None:
                self._start = ts
            self._samples.append(raw_ear)
            ordered = sorted(self._samples)
            self._value = self._clamp(ordered[math.ceil(0.9 * (len(ordered) - 1))])
            if (
                ts - self._start >= p.ear_warmup_s - _EPS
                and len(self._samples) >= p.ear_warmup_min_samples
            ):
                self._warm = True
                self._samples.clear()
        elif raw_ear >= p.ear_closed_ratio * self._value:
            rate = p.ear_adapt_up if raw_ear > self._value else p.ear_adapt_down
            self._value = self._clamp(self._value + rate * (raw_ear - self._value))
        return raw_ear / self._value

    def _clamp(self, value: float) -> float:
        return clamp(value, self._p.ear_baseline_min, self._p.ear_baseline_max)


class PerclosWindow:
    """PERCLOS: the share of valid frames with closed eyes over a time window.

    Valid only when the frames in the window span at least `min_fill` of it
    and at least `min_valid_share` of them carry an eye measurement.
    """

    def __init__(self, *, min_valid_share: float, min_fill: float) -> None:
        self._min_valid_share = min_valid_share
        self._min_fill = min_fill
        self._frames: deque[tuple[float, bool | None]] = deque()

    def reset(self) -> None:
        """Forget all frames."""
        self._frames.clear()

    def update(self, ts: float, closed: bool | None, window_s: float) -> SignalReading:
        """Add one frame (`closed` is None when the eyes were not measured)."""
        self._frames.append((ts, closed))
        while self._frames[0][0] < ts - window_s - _EPS:
            self._frames.popleft()
        total = len(self._frames)
        valid = sum(1 for _, state in self._frames if state is not None)
        closed_count = sum(1 for _, state in self._frames if state)
        value = closed_count / valid if valid else 0.0
        span = ts - self._frames[0][0]
        ok = (
            valid > 0
            and span >= self._min_fill * window_s - _EPS
            and valid >= self._min_valid_share * total - _EPS
        )
        return SignalReading(value=value, valid=ok)


class BlinkRateCounter:
    """Closed-to-open transitions per minute over a time window."""

    def __init__(self, *, window_s: float, min_span_s: float, min_valid_share: float) -> None:
        self._window_s = window_s
        self._min_span_s = min_span_s
        self._min_valid_share = min_valid_share
        self._frames: deque[tuple[float, bool]] = deque()
        self._blinks: deque[float] = deque()
        self._last_closed: bool | None = None
        self._last_valid_ts: float | None = None

    def reset(self) -> None:
        """Forget all frames and blinks."""
        self._frames.clear()
        self._blinks.clear()
        self._last_closed = None
        self._last_valid_ts = None

    def update(self, ts: float, closed: bool | None, gap_tolerance_s: float) -> SignalReading:
        """Add one frame. An unmeasured stretch longer than `gap_tolerance_s`
        breaks a blink: the opening after it is not counted."""
        if closed is None:
            if self._last_valid_ts is None or ts - self._last_valid_ts > gap_tolerance_s + _EPS:
                self._last_closed = None
        else:
            if self._last_closed and not closed:
                self._blinks.append(ts)
            self._last_closed = closed
            self._last_valid_ts = ts
        self._frames.append((ts, closed is not None))
        horizon = ts - self._window_s - _EPS
        while self._frames[0][0] < horizon:
            self._frames.popleft()
        while self._blinks and self._blinks[0] < horizon:
            self._blinks.popleft()
        span = ts - self._frames[0][0]
        valid = sum(1 for _, measured in self._frames if measured)
        ok = (
            span >= self._min_span_s - _EPS
            and valid >= self._min_valid_share * len(self._frames) - _EPS
        )
        rate = len(self._blinks) * 60.0 / span if span > _EPS else 0.0
        return SignalReading(value=rate if ok else 0.0, valid=ok)


class YawnDetector:
    """A yawn: the mouth open for more than `min_s` without interruption."""

    def __init__(self, *, min_s: float) -> None:
        self._min_s = min_s
        self._open_since: float | None = None
        self._last_open: float | None = None

    def reset(self) -> None:
        """Forget the running mouth-open period."""
        self._open_since = None
        self._last_open = None

    def update(self, ts: float, mouth_open: bool | None, gap_tolerance_s: float) -> bool | None:
        """Whether a yawn is in progress; None when the mouth was not measured.

        Unmeasured frames up to `gap_tolerance_s` do not restart the timer.
        """
        if mouth_open is None:
            if self._last_open is not None and ts - self._last_open > gap_tolerance_s + _EPS:
                self.reset()
            return None
        if not mouth_open:
            self.reset()
            return False
        if self._open_since is None:
            self._open_since = ts
        self._last_open = ts
        return ts - self._open_since > self._min_s + _EPS


class NodDetector:
    """A head nod: the pitch drops and comes back up shortly after.

    The reference is the highest pitch of the last `ref_window_s`. When the
    pitch falls `drop_deg` below it a drop starts; returning to within
    `recover_deg` of the reference inside `max_s` is a nod, reported on that
    one frame. Staying down longer is a new posture, not a nod.
    """

    def __init__(
        self, *, drop_deg: float, recover_deg: float, max_s: float, ref_window_s: float
    ) -> None:
        self._drop_deg = drop_deg
        self._recover_deg = recover_deg
        self._max_s = max_s
        self._ref_window_s = ref_window_s
        self._history: deque[tuple[float, float]] = deque()
        self._reference = 0.0
        self._drop_started: float | None = None
        self._last_ts: float | None = None

    def reset(self) -> None:
        """Forget the reference and any drop in progress."""
        self._history.clear()
        self._drop_started = None
        self._last_ts = None

    def update(self, ts: float, pitch: float | None, gap_tolerance_s: float) -> bool | None:
        """True on the frame a nod completes; None when pitch is unknown."""
        if pitch is None:
            if self._last_ts is None or ts - self._last_ts > gap_tolerance_s + _EPS:
                self.reset()
            return None
        self._last_ts = ts
        if self._drop_started is None:
            self._history.append((ts, pitch))
            while self._history[0][0] < ts - self._ref_window_s - _EPS:
                self._history.popleft()
            reference = max(value for _, value in self._history)
            if pitch <= reference - self._drop_deg:
                self._reference = reference
                self._drop_started = ts
            return False
        timed_out = ts - self._drop_started > self._max_s + _EPS
        recovered = pitch >= self._reference - self._recover_deg
        if timed_out or recovered:
            # Either way the reference restarts from where the head is now.
            self._drop_started = None
            self._history.clear()
            self._history.append((ts, pitch))
            return recovered and not timed_out
        return False


TrackedPoints = Mapping[str, tuple[Point, float]]
"""Name -> (position in image heights on both axes, body scale in image
heights). The scale (face width, shoulder width) makes the speed independent
of how far the worker is from the camera."""


class MotionTracker:
    """Movement energy: mean landmark speed over a short window.

    The unit is body scales per second (a value of 1 means the tracked
    points move one face or shoulder width per second). 0 is perfectly
    still.
    """

    def __init__(self, *, window_s: float, max_gap_s: float, min_fill: float) -> None:
        self._window_s = window_s
        self._max_gap_s = max_gap_s
        self._min_fill = min_fill
        self._speeds: deque[tuple[float, float]] = deque()
        self._previous: tuple[float, dict[str, tuple[Point, float]]] | None = None

    def reset(self) -> None:
        """Forget all movement."""
        self._speeds.clear()
        self._previous = None

    def update(self, ts: float, points: TrackedPoints | None) -> SignalReading:
        """Add one frame's tracked points (None or empty: nothing tracked)."""
        if points:
            speed = self._speed(ts, points)
            if speed is not None:
                self._speeds.append((ts, speed))
            self._previous = (ts, dict(points))
        elif self._previous is not None and ts - self._previous[0] > self._max_gap_s + _EPS:
            self._previous = None
        while self._speeds and self._speeds[0][0] < ts - self._window_s - _EPS:
            self._speeds.popleft()
        if len(self._speeds) < 2:
            return SignalReading.invalid()
        newest, oldest = self._speeds[-1][0], self._speeds[0][0]
        if (
            ts - newest > self._max_gap_s + _EPS
            or newest - oldest < self._min_fill * self._window_s - _EPS
        ):
            return SignalReading.invalid()
        mean = math.fsum(speed for _, speed in self._speeds) / len(self._speeds)
        return SignalReading(value=mean)

    def _speed(self, ts: float, points: TrackedPoints) -> float | None:
        if self._previous is None:
            return None
        previous_ts, previous = self._previous
        dt = ts - previous_ts
        if dt <= _EPS or dt > self._max_gap_s + _EPS:
            return None
        speeds = [
            math.hypot(point.x - previous[name][0].x, point.y - previous[name][0].y) / scale / dt
            for name, (point, scale) in points.items()
            if name in previous and scale > _EPS
        ]
        if not speeds or not all(map(math.isfinite, speeds)):
            return None
        return math.fsum(speeds) / len(speeds)
