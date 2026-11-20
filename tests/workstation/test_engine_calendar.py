"""Shift and break calendar in the site timezone."""

from datetime import UTC, datetime, time
from uuid import UUID

import pytest
from ag_shared.config import Shift, ShiftBreak
from ag_workstation.engine.calendar import ShiftCalendar, load_timezone

DAY = UUID("00000000-0000-7000-8000-000000000001")
NIGHT = UUID("00000000-0000-7000-8000-000000000002")

DAY_SHIFT = Shift(
    id=DAY,
    name="Day",
    start_time=time(8, 0),
    end_time=time(17, 0),
    days_of_week=(0, 1, 2, 3, 4),
    breaks=(ShiftBreak(name="Lunch", start_time=time(12, 30), end_time=time(13, 0)),),
)
NIGHT_SHIFT = Shift(
    id=NIGHT,
    name="Night",
    start_time=time(22, 0),
    end_time=time(6, 0),
    days_of_week=(0,),  # starts Monday night only
    breaks=(
        ShiftBreak(name="Tea", start_time=time(23, 30), end_time=time(23, 45)),
        ShiftBreak(name="Meal", start_time=time(2, 0), end_time=time(2, 30)),
    ),
)
CAL = ShiftCalendar((DAY_SHIFT, NIGHT_SHIFT), "Asia/Kolkata")


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC instant for 2026-11-<day> hh:mm India time (UTC+5:30)."""
    local = datetime(2026, 11, day, hour, minute, tzinfo=load_timezone("Asia/Kolkata"))
    return local.astimezone(UTC)


# 2026-11-16 is a Monday.


@pytest.mark.parametrize(
    ("at", "shift", "on_break", "name"),
    [
        (ist(16, 7, 59), None, False, None),
        (ist(16, 8, 0), DAY, False, None),
        (ist(16, 12, 30), DAY, True, "Lunch"),
        (ist(16, 12, 59), DAY, True, "Lunch"),
        (ist(16, 13, 0), DAY, False, None),
        (ist(16, 17, 0), None, False, None),
        (ist(15, 10, 0), None, False, None),  # Sunday
        (ist(16, 22, 0), NIGHT, False, None),
        (ist(16, 23, 40), NIGHT, True, "Tea"),
        (ist(17, 2, 10), NIGHT, True, "Meal"),  # after midnight, Tuesday
        (ist(17, 5, 59), NIGHT, False, None),
        (ist(17, 6, 0), None, False, None),
        (ist(17, 22, 30), None, False, None),  # no night shift on Tuesday
        (ist(17, 2, 10).replace(day=18), None, False, None),
    ],
)
def test_status(at: datetime, shift: UUID | None, on_break: bool, name: str | None) -> None:
    status = CAL.status(at)
    assert status.shift_id == shift
    assert status.on_break is on_break
    assert status.break_name == name


def test_shift_end_is_reported_in_utc() -> None:
    status = CAL.status(ist(16, 23, 0))
    assert status.shift_end_utc == ist(17, 6, 0)


def test_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CAL.status(datetime(2026, 11, 16, 10, 0))


def test_unknown_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        ShiftCalendar((), "Nowhere/Special")


def test_no_shifts_means_never_a_break() -> None:
    status = ShiftCalendar((), "UTC").status(datetime(2026, 1, 1, tzinfo=UTC))
    assert status.shift_id is None
    assert not status.on_break


def test_break_in_the_repeated_dst_hour_applies_to_the_first_pass_only() -> None:
    shift = Shift(
        id=DAY,
        name="Night",
        start_time=time(0, 0),
        end_time=time(6, 0),
        days_of_week=(6,),  # 2026-11-01 is a Sunday; New York falls back at 02:00
        breaks=(ShiftBreak(name="Tea", start_time=time(1, 15), end_time=time(1, 45)),),
    )
    cal = ShiftCalendar((shift,), "America/New_York")
    first = datetime(2026, 11, 1, 5, 15, tzinfo=UTC)  # 01:15 EDT
    second = datetime(2026, 11, 1, 6, 15, tzinfo=UTC)  # 01:15 EST, the repeat
    assert cal.status(first).on_break
    assert not cal.status(second).on_break
    assert cal.status(second).shift_id == DAY
