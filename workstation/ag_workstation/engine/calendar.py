"""Shift and break calendar: which shift is active and whether it is a break.

Shift and break times are site-local wall times (`docs/schema.md`); the
engine works in UTC. A shift may cross midnight; `days_of_week` names the day
it starts on (0 = Monday). Local times are turned into UTC instants with
`fold=0` and all comparisons happen in UTC, so around a DST change a break in
the repeated hour applies to its first pass only.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ag_shared.config import Shift


def load_timezone(name: str) -> ZoneInfo:
    """The IANA timezone `name`; ValueError if it does not exist."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


@dataclass(frozen=True, slots=True)
class CalendarStatus:
    """The calendar at one moment."""

    shift_id: UUID | None
    on_break: bool
    break_name: str | None = None
    shift_end_utc: datetime | None = None


_NO_SHIFT = CalendarStatus(shift_id=None, on_break=False)


class ShiftCalendar:
    """Answers "which shift, and is it a break?" for a UTC instant."""

    def __init__(self, shifts: tuple[Shift, ...], timezone: str) -> None:
        self._shifts = shifts
        self._tz = load_timezone(timezone)

    def _local(self, day: date, at: time) -> datetime:
        return datetime.combine(day, at, tzinfo=self._tz)

    def _span(self, day: date, start: time, end: time) -> tuple[datetime, datetime]:
        """[begin, end) in UTC."""
        begin = self._local(day, start)
        end_day = day if end > start else day + timedelta(days=1)
        return begin.astimezone(UTC), self._local(end_day, end).astimezone(UTC)

    def status(self, now_utc: datetime) -> CalendarStatus:
        """The active shift and break at `now_utc` (timezone-aware)."""
        if now_utc.tzinfo is None:
            raise ValueError("now_utc must be timezone-aware")
        local = now_utc.astimezone(self._tz)
        now = now_utc.astimezone(UTC)
        for day in (local.date(), local.date() - timedelta(days=1)):
            for shift in self._shifts:
                if day.weekday() not in shift.days_of_week:
                    continue
                begin, end = self._span(day, shift.start_time, shift.end_time)
                if not begin <= now < end:
                    continue
                for brk in shift.breaks:
                    # A break belongs to its shift: it starts on the shift's
                    # start day, or on the next day if it lies after midnight.
                    brk_day = day if brk.start_time >= shift.start_time else day + timedelta(1)
                    b_begin, b_end = self._span(brk_day, brk.start_time, brk.end_time)
                    if b_begin <= now < b_end:
                        return CalendarStatus(shift.id, True, brk.name, end)
                return CalendarStatus(shift.id, False, None, end)
        return _NO_SHIFT
