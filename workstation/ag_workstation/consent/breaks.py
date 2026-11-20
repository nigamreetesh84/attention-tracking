"""Scheduled breaks of the bundle's shifts, evaluated in the site timezone.

Shift and break times are site-local wall times (`ag_shared.config.Shift`);
a shift may cross midnight and `days_of_week` names the day it starts on.
Break offsets are measured from the shift start, so a break after midnight
of an overnight shift falls on the next calendar day. Local times are
turned into UTC with the site's IANA zone (first occurrence on a DST fold).
"""

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ag_shared.config import Shift

_DAY = timedelta(days=1)


def _wall(t: time) -> timedelta:
    return timedelta(hours=t.hour, minutes=t.minute, seconds=t.second, microseconds=t.microsecond)


def _since(start: time, t: time) -> timedelta:
    """Wall time from `start` to `t`, wrapping past midnight."""
    return (_wall(t) - _wall(start)) % _DAY


def break_intervals(
    shifts: Iterable[Shift], zone: ZoneInfo, start: datetime, end: datetime
) -> Iterator[tuple[datetime, datetime]]:
    """UTC [start, end) of every scheduled break of shifts that could touch [start, end]."""
    first = start.astimezone(zone).date() - _DAY
    last = end.astimezone(zone).date()
    for shift in shifts:
        day = first
        while day <= last:
            if day.weekday() in shift.days_of_week:
                shift_start = datetime.combine(day, shift.start_time)
                for brk in shift.breaks:
                    local_start = shift_start + _since(shift.start_time, brk.start_time)
                    local_end = local_start + _since(brk.start_time, brk.end_time)
                    yield (
                        local_start.replace(tzinfo=zone).astimezone(UTC),
                        local_end.replace(tzinfo=zone).astimezone(UTC),
                    )
            day += _DAY


def on_break(shifts: Iterable[Shift], timezone: str, start: datetime, end: datetime) -> bool:
    """Whether any scheduled break overlaps the UTC span [start, end].

    Raises for an unknown timezone or naive times; the gate treats that as closed.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    zone = ZoneInfo(timezone)
    return any(
        b_start <= end and start < b_end
        for b_start, b_end in break_intervals(shifts, zone, start, end)
    )
