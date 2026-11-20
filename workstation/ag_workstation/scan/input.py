"""Barcode scanner input (PLAN 2.6; risks Q10: keyboard wedge everywhere, serial/HID later).

`ScannerInput` is the interface the scan screen wires to `ScanFlow.on_barcode`.
`KeyboardWedgeParser` is the release-1 implementation: the view forwards the
text of every key press (`feed`) and the parser assembles barcodes. It is pure
logic with an injected clock, so it has no Qt dependency.

A keyboard-wedge scanner "types" a whole code in a burst, a few milliseconds
per character, and ends it with a terminator (Enter by default). A person
types far slower. The parser therefore accepts a code only if every gap
between its keystrokes, the terminator included, is at most
`max_inter_key_s`; a slower gap discards what was typed so far. Control
characters are dropped, and a code longer than `max_length` is discarded
whole rather than cut (a truncated code would be a wrong code).
"""

import logging
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from ag_workstation.clock import Clock

logger = logging.getLogger(__name__)

BarcodeHandler = Callable[[str], None]
"""Receives one complete barcode."""

MAX_BARCODE_CHARS: Final = 4096
"""The `scan.barcode` wire limit (contract 4.3)."""


class ScannerInput(Protocol):
    """A source of complete barcodes."""

    def set_handler(self, handler: BarcodeHandler | None) -> None:
        """Where complete barcodes go (None: drop them)."""
        ...

    def start(self) -> None:
        """Begin delivering barcodes."""
        ...

    def stop(self) -> None:
        """Stop delivering barcodes; safe to call twice."""
        ...


class Terminator(StrEnum):
    """Keys a scanner may send after a code."""

    ENTER = "enter"
    TAB = "tab"


_TERMINATOR_CHARS: Final[dict[Terminator, frozenset[str]]] = {
    Terminator.ENTER: frozenset({"\r", "\n"}),
    Terminator.TAB: frozenset({"\t"}),
}


@dataclass(frozen=True, slots=True)
class WedgeConfig:
    """Keyboard-wedge parser settings."""

    terminators: frozenset[Terminator] = field(
        default_factory=lambda: frozenset({Terminator.ENTER})
    )
    max_inter_key_s: float = 0.05
    """Longest gap between two keystrokes of one scan. Scanners send a
    character every 1-20 ms; people rarely type faster than 80 ms per key."""
    min_length: int = 1
    max_length: int = MAX_BARCODE_CHARS

    def __post_init__(self) -> None:
        if not self.terminators:
            raise ValueError("at least one terminator is needed")
        if self.max_inter_key_s <= 0:
            raise ValueError("max_inter_key_s must be positive")
        if not 1 <= self.min_length <= self.max_length <= MAX_BARCODE_CHARS:
            raise ValueError(f"need 1 <= min_length <= max_length <= {MAX_BARCODE_CHARS}")


@dataclass(frozen=True, slots=True)
class WedgeStats:
    """Counters for diagnostics (never the typed text)."""

    accepted: int
    rejected_slow: int
    """Input discarded because a gap was too long (a person typing)."""
    rejected_short: int
    rejected_overflow: int


def _is_control(char: str) -> bool:
    # Cc: C0/C1 controls (GS of GS1 codes included); Cf: invisible format chars.
    return unicodedata.category(char) in ("Cc", "Cf")


class KeyboardWedgeParser:
    """Assembles barcodes from keyboard-wedge keystrokes. Thread-safe; never raises from `feed`."""

    def __init__(self, clock: Clock, config: WedgeConfig | None = None) -> None:
        self._clock = clock
        self._config = config or WedgeConfig()
        self._terminators = frozenset().union(
            *(_TERMINATOR_CHARS[t] for t in self._config.terminators)
        )
        self._lock = threading.Lock()
        self._handler: BarcodeHandler | None = None
        self._active = True
        self._buffer: list[str] = []
        self._overflow = False
        self._pending_keys = 0
        """Keystrokes since the last terminator or discard (control chars included)."""
        self._last_key: float | None = None
        self._accepted = 0
        self._rejected_slow = 0
        self._rejected_short = 0
        self._rejected_overflow = 0

    # -- ScannerInput ----------------------------------------------------

    def set_handler(self, handler: BarcodeHandler | None) -> None:
        """Where complete barcodes go."""
        with self._lock:
            self._handler = handler

    def start(self) -> None:
        """Accept keystrokes (the default)."""
        with self._lock:
            self._active = True

    def stop(self) -> None:
        """Ignore keystrokes and drop any partial code."""
        with self._lock:
            self._active = False
            self._clear()

    @property
    def active(self) -> bool:
        """Whether keystrokes are being parsed."""
        with self._lock:
            return self._active

    # -- parsing -----------------------------------------------------------

    def reset(self) -> None:
        """Drop any partial code (e.g. when the screen changes)."""
        with self._lock:
            self._clear()

    def stats(self) -> WedgeStats:
        """Counters since start."""
        with self._lock:
            return WedgeStats(
                self._accepted, self._rejected_slow, self._rejected_short, self._rejected_overflow
            )

    def feed(self, text: str) -> list[str]:
        """Process the text of one key event (all characters share its time).

        Returns the barcodes completed by this event, after handing each to
        the handler. A failing handler is logged, never raised to the view.
        """
        now = self._clock.monotonic()
        done: list[str] = []
        with self._lock:
            if not self._active:
                return done
            for char in text:
                code = self._feed_char(char, now)
                if code is not None:
                    done.append(code)
            handler = self._handler
        for code in done:
            if handler is not None:
                try:
                    handler(code)
                except Exception as exc:
                    logger.error("barcode handler failed: %s", type(exc).__name__)
        return done

    def _clear(self) -> None:
        self._buffer.clear()
        self._overflow = False
        self._pending_keys = 0
        self._last_key = None

    def _feed_char(self, char: str, now: float) -> str | None:
        if (
            self._pending_keys
            and self._last_key is not None
            and now - self._last_key > self._config.max_inter_key_s
        ):
            # Too slow for a scanner: what came before was typed by a person.
            self._rejected_slow += 1
            self._clear()
        self._last_key = now
        if char in self._terminators:
            return self._terminate()
        self._pending_keys += 1
        if _is_control(char) or self._overflow:
            return None
        if len(self._buffer) >= self._config.max_length:
            self._overflow = True
            self._buffer.clear()
            return None
        self._buffer.append(char)
        return None

    def _terminate(self) -> str | None:
        code: str | None = None
        if self._overflow:
            self._rejected_overflow += 1
        elif len(self._buffer) >= self._config.min_length:
            code = "".join(self._buffer)
            self._accepted += 1
        elif self._pending_keys:
            self._rejected_short += 1
        # An empty terminator (the LF of CR LF) is ignored silently.
        self._clear()
        return code


class _UnsupportedScannerInput:
    """Base of the not-yet-built scanner transports (risks Q10)."""

    transport = "unsupported"

    def __init__(self, port: str) -> None:
        self.port = port
        self._handler: BarcodeHandler | None = None

    def set_handler(self, handler: BarcodeHandler | None) -> None:
        """Store the handler for when the transport exists."""
        self._handler = handler

    def start(self) -> None:
        """Not available in release 1."""
        raise NotImplementedError(f"{self.transport} scanners are not supported in release 1")

    def stop(self) -> None:
        """Nothing to stop."""


class SerialScannerInput(_UnsupportedScannerInput):
    """Serial (COM port) scanner: interface stub; keyboard wedge is the release-1 default."""

    transport = "serial"


class HidScannerInput(_UnsupportedScannerInput):
    """HID point-of-sale scanner: interface stub; keyboard wedge is the release-1 default."""

    transport = "hid"
