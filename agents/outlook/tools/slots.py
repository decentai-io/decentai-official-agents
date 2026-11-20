"""Finding a time is arithmetic.

Given the busy periods of every calendar that could be checked, the working days and hours, windows to avoid (lunch)
and a buffer around meetings, the candidates are the slots of the wanted length that fit in what is left. Nothing here
asks a model; the same inputs always give the same slots.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

STEP = timedelta(minutes=15)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def clock(text: str) -> time:
    hour, minute = str(text).split(":")
    return time(int(hour), int(minute))


def _subtract(free: list[tuple[datetime, datetime]], busy: tuple[datetime, datetime]):
    start, end = busy
    kept = []
    for a, b in free:
        if end <= a or start >= b:
            kept.append((a, b))
            continue
        if start > a:
            kept.append((a, start))
        if end < b:
            kept.append((end, b))
    return kept


def find_slots(*, duration: timedelta, from_date: date, to_date: date, zone: ZoneInfo, working_start: time,
               working_end: time, working_days: list[int], avoid: list[tuple[time, time]], buffer: timedelta,
               busy: list[tuple[datetime, datetime]], not_before: datetime | None, limit: int) -> list[dict[str, Any]]:
    """Slots on working days, inside working hours, outside avoid windows, clear of every busy period plus the
    buffer on both sides, at 15-minute steps; at most ``limit``, at most two a day so a choice of days comes first."""
    padded = [(s - buffer, e + buffer) for s, e in busy]
    found: list[dict[str, Any]] = []
    day = from_date
    while day <= to_date and len(found) < limit:
        if day.weekday() in working_days:
            free = [(datetime.combine(day, working_start, zone), datetime.combine(day, working_end, zone))]
            for a_start, a_end in avoid:
                free = _subtract(free, (datetime.combine(day, a_start, zone), datetime.combine(day, a_end, zone)))
            for period in padded:
                free = _subtract(free, period)
            if not_before is not None:
                free = _subtract(free, (datetime.min.replace(tzinfo=timezone.utc), not_before))
            today = 0
            for a, b in free:
                cursor = _ceil(a.astimezone(zone))
                while cursor + duration <= b and today < 2 and len(found) < limit:
                    # A free period may begin where a busy one (read in UTC) ends: shown in the person's zone.
                    found.append({"start": cursor.astimezone(zone).isoformat(timespec="minutes"),
                                  "end": (cursor + duration).astimezone(zone).isoformat(timespec="minutes"),
                                  "weekday": WEEKDAYS[day.weekday()]})
                    today += 1
                    cursor = _ceil((cursor + duration + buffer).astimezone(zone))
                if today >= 2:
                    break
        day += timedelta(days=1)
    return found


def _ceil(moment: datetime) -> datetime:
    """Up to the next quarter hour, so slots read like meeting times."""
    floored = moment.replace(minute=(moment.minute // 15) * 15, second=0, microsecond=0)
    return floored if floored == moment else floored + STEP
