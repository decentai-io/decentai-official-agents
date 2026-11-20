"""Finding a time is arithmetic.

Given the busy periods of every calendar that could be checked, the days to look at, which weekdays are working days
(Monday to Friday by default; Sunday to Thursday in the Gulf), working hours, avoid windows (lunch) and a buffer to keep
clear around existing meetings, the candidates are the slots of the wanted length that fit entirely in what is left.
Everything is reckoned in UTC and shown in the person's zone, so a change of offset (summer time) moves nothing. No
model is asked; the same inputs always give the same slots.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

STEP = timedelta(minutes=15)
NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
Span = tuple[datetime, datetime]


def subtract(free: list[Span], busy: Span) -> list[Span]:
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


def overlapping(busy: list[Span], start: datetime, end: datetime) -> list[Span]:
    return [(a, b) for a, b in busy if a < end and b > start]


def find_slots(*, duration: timedelta, from_date: date, to_date: date, zone: ZoneInfo, days: set[int],
               working_start: time, working_end: time, avoid: list[tuple[time, time]], buffer: timedelta,
               busy: list[Span], not_before: datetime, limit: int, per_day: int) -> list[dict[str, Any]]:
    """Slots on working days, inside working hours, outside avoid windows, clear of every busy period plus the buffer
    on both sides, starting on the quarter hour; at most ``limit``, and at most ``per_day`` on one day, so the person
    sees a choice of days before a choice of hours."""
    utc = timezone.utc

    def at(day: date, clock: time) -> datetime:
        return datetime.combine(day, clock, zone).astimezone(utc)

    padded = [(s.astimezone(utc) - buffer, e.astimezone(utc) + buffer) for s, e in busy]
    found: list[dict[str, Any]] = []
    day = from_date
    while day <= to_date and len(found) < limit:
        if day.weekday() in days:
            free = [(at(day, working_start), at(day, working_end))]
            for a_start, a_end in avoid:
                free = subtract(free, (at(day, a_start), at(day, a_end)))
            for period in padded:
                free = subtract(free, period)
            free = subtract(free, (datetime.min.replace(tzinfo=utc), not_before.astimezone(utc)))
            taken = 0
            for a, b in free:
                cursor = ceil(a)
                while cursor + duration <= b and taken < per_day and len(found) < limit:
                    found.append({"start": cursor.astimezone(zone).isoformat(),
                                  "end": (cursor + duration).astimezone(zone).isoformat(),
                                  "weekday": NAMES[day.weekday()]})
                    taken += 1
                    cursor = ceil(cursor + duration + buffer)
                if taken >= per_day:
                    break
        day += timedelta(days=1)
    return found


def ceil(moment: datetime) -> datetime:
    """Up to the next quarter hour, so slots read like meeting times."""
    floored = moment.replace(minute=(moment.minute // 15) * 15, second=0, microsecond=0)
    return floored if floored == moment else floored + STEP
