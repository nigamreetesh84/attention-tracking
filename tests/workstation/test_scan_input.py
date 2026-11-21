"""Keyboard-wedge barcode parser and the scanner input interface (PLAN 2.6, risks Q10)."""

import pytest
from ag_workstation.clock import FakeClock
from ag_workstation.scan import (
    MAX_BARCODE_CHARS,
    SHORT_CODE_CHARS,
    HidScannerInput,
    KeyboardWedgeParser,
    ScannerInput,
    SerialScannerInput,
    Terminator,
    WedgeConfig,
)

SCANNER_GAP = 0.008
HUMAN_GAP = 0.15


def _burst(
    parser: KeyboardWedgeParser, clock: FakeClock, text: str, gap: float = SCANNER_GAP
) -> list[str]:
    out: list[str] = []
    for char in text:
        out += parser.feed(char)
        clock.advance(gap)
    return out


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def parser(clock: FakeClock) -> KeyboardWedgeParser:
    return KeyboardWedgeParser(clock)


def test_scanner_burst_with_enter_is_one_barcode(
    parser: KeyboardWedgeParser, clock: FakeClock
) -> None:
    got: list[str] = []
    parser.set_handler(got.append)
    assert _burst(parser, clock, "ABC-123\r") == ["ABC-123"]
    assert got == ["ABC-123"]
    assert parser.stats().accepted == 1


def test_crlf_gives_one_barcode(parser: KeyboardWedgeParser, clock: FakeClock) -> None:
    assert _burst(parser, clock, "X1\r\nX2\n") == ["X1", "X2"]
    stats = parser.stats()
    assert (stats.accepted, stats.rejected_short, stats.rejected_slow) == (2, 0, 0)


def test_human_typing_is_rejected(parser: KeyboardWedgeParser, clock: FakeClock) -> None:
    got: list[str] = []
    parser.set_handler(got.append)
    assert _burst(parser, clock, "12345\r", gap=HUMAN_GAP) == []
    assert got == []
    assert parser.stats().rejected_slow >= 1


def test_slow_enter_after_fast_burst_is_rejected(
    parser: KeyboardWedgeParser, clock: FakeClock
) -> None:
    _burst(parser, clock, "ABC")
    clock.advance(HUMAN_GAP)
    assert parser.feed("\r") == []


def test_stray_human_key_does_not_prefix_next_scan(
    parser: KeyboardWedgeParser, clock: FakeClock
) -> None:
    parser.feed("q")
    clock.advance(2.0)
    assert _burst(parser, clock, "CODE9\r") == ["CODE9"]


def test_tab_terminator_configurable(clock: FakeClock) -> None:
    parser = KeyboardWedgeParser(clock, WedgeConfig(terminators=frozenset({Terminator.TAB})))
    assert _burst(parser, clock, "AB\tCD\r\t") == ["AB", "CD"]


def test_control_characters_are_stripped(parser: KeyboardWedgeParser, clock: FakeClock) -> None:
    # GS (0x1d) of a GS1 code, a BEL, a zero-width format char and Tab (not a terminator here).
    assert _burst(parser, clock, "01\x1d2\x07X​Y\tZ\r") == ["012XYZ"]


def test_max_length_discards_whole_code(clock: FakeClock) -> None:
    parser = KeyboardWedgeParser(clock, WedgeConfig(max_length=5))
    assert _burst(parser, clock, "123456789\r") == []
    assert parser.stats().rejected_overflow == 1
    assert _burst(parser, clock, "12345\r") == ["12345"]


def test_min_length_and_terminator_only(clock: FakeClock) -> None:
    parser = KeyboardWedgeParser(clock, WedgeConfig(min_length=3))
    assert _burst(parser, clock, "AB\r") == []
    assert parser.stats().rejected_short == 1
    assert _burst(parser, clock, "\r\r") == []
    assert parser.stats().rejected_short == 1


def test_multi_char_key_event_shares_timestamp(
    parser: KeyboardWedgeParser, clock: FakeClock
) -> None:
    assert parser.feed("PASTED-CODE\r") == ["PASTED-CODE"]


def test_stop_drops_partial_and_ignores_input(
    parser: KeyboardWedgeParser, clock: FakeClock
) -> None:
    _burst(parser, clock, "ABC")
    parser.stop()
    assert not parser.active
    assert _burst(parser, clock, "DEF\r") == []
    parser.start()
    assert parser.active
    assert _burst(parser, clock, "GHI\r") == ["GHI"]


def test_reset_drops_partial(parser: KeyboardWedgeParser, clock: FakeClock) -> None:
    _burst(parser, clock, "ABC")
    parser.reset()
    assert _burst(parser, clock, "D\r") == ["D"]


def test_failing_handler_never_reaches_the_view(
    parser: KeyboardWedgeParser, clock: FakeClock
) -> None:
    def boom(_: str) -> None:
        raise RuntimeError("handler bug")

    parser.set_handler(boom)
    assert _burst(parser, clock, "OK\r") == ["OK"]
    parser.set_handler(None)
    assert _burst(parser, clock, "OK2\r") == ["OK2"]


def test_default_max_length_is_wire_limit(parser: KeyboardWedgeParser) -> None:
    assert parser.feed("9" * MAX_BARCODE_CHARS + "\r") == ["9" * MAX_BARCODE_CHARS]
    assert parser.feed("9" * (MAX_BARCODE_CHARS + 1) + "\r") == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"terminators": frozenset()},
        {"max_inter_key_s": 0},
        {"min_length": 0},
        {"min_length": 5, "max_length": 4},
        {"max_length": MAX_BARCODE_CHARS + 1},
    ],
)
def test_config_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WedgeConfig(**kwargs)  # type: ignore[arg-type]  # deliberately bad values


@pytest.mark.parametrize("cls", [SerialScannerInput, HidScannerInput])
def test_serial_and_hid_are_interface_stubs(cls: type[SerialScannerInput]) -> None:
    scanner: ScannerInput = cls("COM3")
    scanner.set_handler(print)
    with pytest.raises(NotImplementedError):
        scanner.start()
    scanner.stop()


def test_wedge_parser_is_a_scanner_input(parser: KeyboardWedgeParser) -> None:
    scanner: ScannerInput = parser
    scanner.start()
    scanner.stop()


def test_short_codes_are_accepted_and_counted_by_default(
    parser: KeyboardWedgeParser, clock: FakeClock
) -> None:
    """2.R open item 5: a 1-2 character code at scanner speed is recorded (rule 1:
    a real scan is never rejected); it is counted so a site can see how often
    it happens before choosing a minimum length."""
    assert WedgeConfig().min_length == 1
    assert _burst(parser, clock, "7\r") == ["7"]
    assert _burst(parser, clock, "AB\r") == ["AB"]
    assert _burst(parser, clock, "ABC\r") == ["ABC"]
    stats = parser.stats()
    assert (stats.accepted, stats.short_codes, stats.rejected_short) == (3, 2, 0)
    assert SHORT_CODE_CHARS == 3


def test_short_code_threshold_is_configurable(clock: FakeClock) -> None:
    parser = KeyboardWedgeParser(clock, WedgeConfig(short_code_chars=5))
    assert _burst(parser, clock, "ABCD\r") == ["ABCD"]
    assert parser.stats().short_codes == 1
    with pytest.raises(ValueError):
        WedgeConfig(short_code_chars=0)
