"""Today, in the person's zone, and money, in exact decimals.

"Today" is never taken from UTC when the person's zone is known: every function that needs it takes a `timezone`
input (an IANA name such as Asia/Dubai). Without one it is UTC, and the answer says so. Money is text holding an
exact decimal ("4800.00"), never a float."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .renewal_dates import iso

_MONEY = re.compile(r"^\d{1,13}(\.\d{1,4})?$")


def today_in(inputs) -> Tuple[Optional[date], str, str]:
    """(today, the zone's name, a problem): today as the person's clock reads it."""
    name = str(inputs.get("timezone") or "").strip()
    if not name:
        return datetime.now(timezone.utc).date(), "UTC", ""
    try:
        return datetime.now(ZoneInfo(name)).date(), name, ""
    except (ZoneInfoNotFoundError, ValueError):
        return None, name, f"'{name}' is not an IANA time zone (such as Asia/Dubai or Europe/London)."


def day_or_today(inputs, name: str) -> Tuple[Optional[date], str, str]:
    """(the date the input `name` gives, or today in the zone; the zone used; a problem)."""
    given = str(inputs.get(name) or "").strip()
    if given:
        found = iso(given)
        return (found, "", "") if found else (None, "", f"{name} must be YYYY-MM-DD.")
    return today_in(inputs)


def money(value) -> Tuple[Optional[str], str]:
    """(the amount as exact decimal text, a problem). A decimal point only: "4,800" is refused rather than guessed
    as four thousand eight hundred or four point eight."""
    if value is None or value == "":
        return None, ""
    text = str(value).strip()
    if isinstance(value, float):
        text = repr(value)
    if not _MONEY.fullmatch(text):
        return None, (f"value must be an amount like 4800 or 4800.50, digits with a decimal point only, "
                      f"not '{value}'.")
    try:
        return str(Decimal(text)), ""
    except InvalidOperation:
        return None, f"value '{value}' is not an amount."


def stored_money(value) -> Optional[str]:
    """A kept amount as exact text, whatever an earlier version stored (1.0.0 kept a number)."""
    if value in (None, ""):
        return None
    try:
        found = Decimal(str(value))
    except InvalidOperation:
        return None
    if found == found.to_integral_value() and "." in str(value) and str(value).endswith(".0"):
        found = found.quantize(Decimal(1))
    return str(found)
