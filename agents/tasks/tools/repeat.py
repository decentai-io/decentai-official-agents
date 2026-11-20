"""Recurring tasks: the rule is kept in code, never left to the model. A task that repeats daily, weekly, monthly or
yearly
has a due date; completing it makes the next one, due one step after the previous due date (not after the day it
was done, so a late completion doesn't drift the schedule). A monthly task keeps the day of the month it started
on: due the 31st, it is next due the 30th in a month of 30 days and the 31st again after that. A yearly task keeps its
month and day: due 29 February, it is due the 28th in a year that has no 29th, and the 29th again in a leap year."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional

RULES = ("daily", "weekly", "monthly", "yearly")


def next_due(due: date, rule: str, day_of_month: Optional[int] = None) -> date:
    if rule == "daily":
        return due + timedelta(days=1)
    if rule == "weekly":
        return due + timedelta(days=7)
    if rule == "monthly":
        year, month = (due.year + 1, 1) if due.month == 12 else (due.year, due.month + 1)
        wanted = day_of_month or due.day
        return date(year, month, min(wanted, calendar.monthrange(year, month)[1]))
    if rule == "yearly":
        wanted = day_of_month or due.day
        return date(due.year + 1, due.month, min(wanted, calendar.monthrange(due.year + 1, due.month)[1]))
    raise ValueError(f"no rule '{rule}'")


def first_after(due: date, rule: str, day_of_month: Optional[int], after: date) -> date:
    """The next due date strictly after `after`, when completion came so late that several were missed: a missed
    occurrence is not made just to be overdue."""
    found = next_due(due, rule, day_of_month)
    while found <= after:
        found = next_due(found, rule, day_of_month)
    return found
