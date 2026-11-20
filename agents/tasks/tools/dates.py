"""Dates, deterministically. A date is accepted when the text states it whole (ISO, or a written day, month and
year), or read from a known meeting date (a weekday name, a day and month without a year), and then the reading is
said, never applied silently. "Next Friday", "next week", "soon", "12/09" are not dates here: they stay as text
for a person to resolve."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional, Tuple

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?:\s+(\d{4}))?\b")
_MONTH_DAY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b")
_WEEKDAY = re.compile(r"\b(?:(next|this)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def parse_iso(text: str) -> Optional[date]:
    match = _ISO.search(str(text or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def resolve(text: str, anchor: Optional[date]) -> Tuple[Optional[date], str]:
    """(date, how). how is 'iso' or 'written' for a date the text states whole; 'no_year' for a written day and
    month whose year was taken from the anchor (the next year if it would be in the past); 'weekday' for a weekday
    name or 'tomorrow', the first such day strictly after the anchor; 'ambiguous' for "next Friday", which people
    use for two different days; '' when the text is not a date. Only 'iso' and 'written' are stated by the text:
    the others are readings, and the caller says them out loud."""
    raw = str(text or "").strip()
    if not raw:
        return None, ""
    found = parse_iso(raw)
    if found:
        return found, "iso"
    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        match = pattern.search(raw)
        if not match:
            continue
        day, month = (match.group(1), match.group(2)) if order == "dm" else (match.group(2), match.group(1))
        number = MONTHS.get(month.lower())
        if number is None:
            continue
        year = match.group(3)
        try:
            if year:
                return date(int(year), number, int(day)), "written"
            if anchor is None:
                return None, ""
            candidate = date(anchor.year, number, int(day))
            if candidate < anchor:
                candidate = date(anchor.year + 1, number, int(day))
            return candidate, "no_year"
        except ValueError:
            return None, ""
    match = _WEEKDAY.search(raw)
    if match and anchor is not None:
        if (match.group(1) or "").lower() == "next":
            return None, "ambiguous"
        target = WEEKDAYS.index(match.group(2).lower())
        ahead = (target - anchor.weekday()) % 7 or 7
        return anchor + timedelta(days=ahead), "weekday"
    if match:
        return None, "ambiguous" if (match.group(1) or "").lower() == "next" else ""
    if re.search(r"tomorrow", raw, re.I) and anchor is not None:
        return anchor + timedelta(days=1), "weekday"
    return None, ""


def reading(text: str, found: Optional[date], how: str, anchor: Optional[date]) -> str:
    """What a date the notes did not state whole was read as, in words, for the person to check; "" when the text
    stated it."""
    said = str(text or "").strip()
    if how == "no_year" and found:
        return f"'{said}' gives no year: read as {found.isoformat()}, the next such day after the meeting. Check it."
    if how == "weekday" and found and anchor:
        return (f"'{said}' is relative: read as {found.isoformat()} ({found.strftime('%A')}), the first after the "
                f"meeting on {anchor.isoformat()}. Check it.")
    if how == "ambiguous":
        return f"'{said}' can mean this week's or the following week's day: not resolved. Ask which date."
    return ""
