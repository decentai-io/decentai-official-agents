"""What the tools share: today in the person's zone, weeks, hours, and records as rows.

Hours are kept as text with two decimals ("1.50"), reckoned as Decimal and handed out as that text, so a week adds up
to exactly what its days add up to, and a budget's remainder is exact. A week is an ISO week, "2026-W37", Monday to
Sunday. "Today" is the person's: every function that defaults to today or this week takes a `timezone` (IANA); without
one it is UTC, and the answer says so. The working days a gap is counted on are a setting: Monday to Friday by
default, Sunday to Thursday where that is the week.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
HOURS = re.compile(r"^\d{1,6}(\.\d{1,4})?$")
CENT = Decimal("0.01")
ZERO = Decimal(0)
DAY_LIMIT = Decimal(24)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
#: The days off in each working week, as date.weekday() numbers (Monday 0).
WEEKENDS = {"mon_fri": (5, 6), "sun_thu": (4, 5)}


def zone_of(inputs: Dict[str, Any]) -> Tuple[Optional[Any], str, str]:
    """(the zone, its name, a problem)."""
    name = str(inputs.get("timezone") or "").strip()
    if not name:
        return timezone.utc, "UTC", ""
    try:
        return ZoneInfo(name), name, ""
    except (ZoneInfoNotFoundError, ValueError):
        return None, name, f"'{name}' is not an IANA time zone (such as Asia/Dubai or Europe/London)."


def today(inputs: Optional[Dict[str, Any]] = None) -> date:
    """Today on the person's clock; UTC when their zone isn't given. A bad zone is refused before this is asked."""
    zone, _, _ = zone_of(inputs or {})
    return datetime.now(zone or timezone.utc).date()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_date(text: Any) -> Optional[date]:
    text = str(text or "").strip()
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def week_of(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_days(week: Any) -> Optional[List[date]]:
    found = WEEK.match(str(week or "").strip())
    if not found:
        return None
    try:
        monday = date.fromisocalendar(int(found.group(1)), int(found.group(2)), 1)
    except ValueError:
        return None
    return [monday + timedelta(days=i) for i in range(7)]


def working(day: date, work_week: str = "mon_fri") -> bool:
    return day.weekday() not in WEEKENDS.get(work_week, WEEKENDS["mon_fri"])


def hours_of(value: Any) -> Optional[Decimal]:
    """Hours as an exact decimal, from text ("1.5") or a number as JSON carried it; None when it isn't one. A float
    is read by its shortest text (0.1 is "0.1"), never by its binary value."""
    if value is None or isinstance(value, bool):
        return None
    text = repr(value) if isinstance(value, float) else str(value).strip()
    if not HOURS.fullmatch(text):
        try:
            found = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        if not found.is_finite() or found < 0:
            return None
    else:
        found = Decimal(text)
    return found.quantize(CENT, rounding=ROUND_HALF_UP)


def quarter_hours(minutes: Any) -> Decimal:
    """A length of time to the nearest quarter hour, never less than one."""
    quarters = (Decimal(str(minutes)) / 15).to_integral_value(rounding=ROUND_HALF_UP)
    return (max(quarters, Decimal(1)) * Decimal("0.25")).quantize(CENT)


def as_text(hours: Decimal) -> str:
    return str(Decimal(hours).quantize(CENT))


def total_of(values) -> Decimal:
    return sum((hours_of(v) or ZERO for v in values), ZERO)


def code_of(value: Any) -> str:
    return str(value or "").strip().upper()


def keys_of(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (record or {}).get("fields") or {}


def entry_row(ref: str, keys: Dict[str, Any]) -> Dict[str, Any]:
    return {"entry_ref": ref, "date": str(keys.get("date") or ""),
            "week": str(keys.get("week") or ""),
            "project_code": str(keys.get("project_code") or ""),
            "hours": as_text(hours_of(keys.get("hours")) or ZERO),
            "note": str(keys.get("note") or ""),
            "source": str(keys.get("source") or "manual"),
            "event_id": str(keys.get("event_id") or ""),
            "status": str(keys.get("status") or "draft")}


def project_row(ref: str, keys: Dict[str, Any]) -> Dict[str, Any]:
    row = {"project_ref": ref, "code": str(keys.get("code") or ""),
           "name": str(keys.get("name") or ""), "client": str(keys.get("client") or ""),
           "billable": keys.get("billable") == "yes",
           "status": str(keys.get("status") or "active")}
    budget = hours_of(keys.get("budget_hours")) if keys.get("budget_hours") else None
    if budget is not None:
        row["budget_hours"] = as_text(budget)
    return row


async def projects(call) -> Dict[str, Dict[str, Any]]:
    """Every project by code: {"ref", "fields"}."""
    return {code_of(keys_of(r).get("code")): {"ref": str(r.get("ref") or ""),
                                               "fields": keys_of(r)}
            for r in await call.records.list("project", {})}


async def entries(call, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return [entry_row(str(r.get("ref") or ""), keys_of(r))
            for r in await call.records.list("entry", filters or {})]


async def week_record(call, week: str) -> Optional[Dict[str, Any]]:
    found = await call.records.list("week", {"week": week})
    return found[0] if found else None


async def locked(call, week: str) -> bool:
    return keys_of(await week_record(call, week)).get("status") == "submitted"
