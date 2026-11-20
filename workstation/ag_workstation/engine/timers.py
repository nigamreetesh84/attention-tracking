"""Condition timers and event counters with gap tolerance.

`docs/detection-engine.md` section 4: `gap_tolerance_s` means a brief
interruption (a missing, invalid or contrary frame) does not reset a timer.
Everything here is driven by frame timestamps; nothing reads a clock.
"""

from collections import deque

_EPS = 1e-9
"""Slack for comparing float timestamp differences against a limit."""


class ConditionTimer:
    """How long a condition has held, bridging gaps up to `gap_tolerance_s`.

    `update(ts, True)` extends the run; `False` or `None` (not measured)
    leaves it alone until the last true frame is more than the gap tolerance
    old, then the run ends. The duration is measured between the first and
    the latest true frame, so a gap never adds time that was not observed.
    """

    def __init__(self) -> None:
        self._start: float | None = None
        self._last_true: float | None = None

    def reset(self) -> None:
        """End any run."""
        self._start = None
        self._last_true = None

    def update(self, ts: float, condition: bool | None, gap_tolerance_s: float) -> float:
        """Add one frame and return the current run length in seconds (0 = no run)."""
        if condition:
            if self._start is None or (
                self._last_true is not None and ts - self._last_true > gap_tolerance_s + _EPS
            ):
                self._start = ts
            self._last_true = ts
        elif self._last_true is not None and ts - self._last_true > gap_tolerance_s + _EPS:
            self.reset()
        return self.duration

    @property
    def duration(self) -> float:
        """Length of the current run in seconds."""
        if self._start is None or self._last_true is None:
            return 0.0
        return self._last_true - self._start

    def exceeds(self, seconds: float) -> bool:
        """True once the run is strictly longer than `seconds`."""
        return self.duration > seconds + _EPS


class EdgeCounter:
    """Counts rising edges of a boolean signal inside a sliding time window.

    Used for yawns (true while a yawn lasts: one yawn is one rising edge) and
    head nods (true on the frame a nod completes).
    """

    def __init__(self) -> None:
        self._events: deque[float] = deque()
        self._previous = False

    def reset(self) -> None:
        """Forget all events."""
        self._events.clear()
        self._previous = False

    def update(self, ts: float, value: bool | None, window_s: float) -> int:
        """Add one frame and return the number of events in the last `window_s`."""
        current = bool(value)
        if current and not self._previous:
            self._events.append(ts)
        # An unmeasured frame does not end an event in progress.
        if value is not None:
            self._previous = current
        while self._events and ts - self._events[0] > window_s + _EPS:
            self._events.popleft()
        return len(self._events)

    @property
    def count(self) -> int:
        """Events currently inside the window."""
        return len(self._events)


class ShareWindow:
    """Share of valid samples that were true over a sliding time window."""

    def __init__(self) -> None:
        self._samples: deque[tuple[float, bool | None]] = deque()

    def reset(self) -> None:
        """Forget all samples."""
        self._samples.clear()

    def update(self, ts: float, value: bool | None, window_s: float) -> None:
        """Add one sample (None = not measured)."""
        self._samples.append((ts, value))
        while self._samples and ts - self._samples[0][0] > window_s + _EPS:
            self._samples.popleft()

    def valid_share(self) -> float:
        """Share of samples that were measured (0 when empty)."""
        if not self._samples:
            return 0.0
        return sum(v is not None for _, v in self._samples) / len(self._samples)

    def __len__(self) -> int:
        return len(self._samples)
