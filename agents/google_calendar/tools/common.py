"""What every tool of this agent shares: the account and its two Google clients, refusals, times and zones.

- **Two APIs, one account.** Calendar lives at ``www.googleapis.com/calendar/v3`` and Tasks at
  ``tasks.googleapis.com/tasks/v1``. A test's ``api_base_url`` stands for Google's root, under which both paths are
  served.
- **Several accounts,** chosen by address (``standard.accounts``): every function takes an optional ``account``.
- **Time:** a time without an offset is in the person's zone (the calendar's own, or a ``timezone`` input), and
  "today" is today there, never in UTC.
"""

from __future__ import annotations

import functools
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from standard import ResourceDenied
from standard.accounts import account
from standard.google import Google, HttpError, segment
from standard.net import BaseAddress

CALENDAR = "https://www.googleapis.com/calendar/v3"
TASKS = "https://tasks.googleapis.com/tasks/v1"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class Refused(Exception):
    """A refusal said out loud: its words, and the kind the assistant reads. Nothing was written."""

    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.message, self.kind = message, kind


def answers(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """A function whose refusals and Google's are its error result, the same words every time."""

    @functools.wraps(method)
    async def answered(self: Any, call: Any) -> Any:
        try:
            return await method(self, call)
        except Refused as refused:
            return {"error": refused.message, "kind": refused.kind}, "error"
        except HttpError as failed:
            return failed.answer()

    return answered


async def client(call: Any, api: str = "calendar", wanted: Any = None, ask: bool = True) -> Google:
    """The Google client of the account wanted, over Calendar or Tasks; Refused when there is none to use."""
    secret, why = await account(call, "google", wanted, ask, "Google")
    if secret is None:
        raise Refused(why, "account")
    return over(secret, api)


def over(secret: dict[str, Any], api: str) -> Google:
    home = CALENDAR if api == "calendar" else TASKS
    root = BaseAddress.given(secret.get("api_base_url"))
    base = root.rstrip("/") + urlsplit(home).path if root else home
    return Google({**secret, "api_base_url": ""}, base)


def refused_resource(failed: ResourceDenied) -> Refused:
    return Refused(f"The platform refused: {failed}", failed.code or "refused")


# -- zones and times ---------------------------------------------------------------------------------------------

def zone_named(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise Refused(f"{name!r} is not an IANA time zone (for example Asia/Dubai, Europe/London).") from None


async def calendar_zone(agent: Any, google: Google, calendar_id: str, wanted: str = "") -> tuple[ZoneInfo, str]:
    """The zone asked for, else the calendar's own: read from Google once per chat for each calendar."""
    wanted = str(wanted or "").strip()
    if wanted:
        return zone_named(wanted), wanted
    known = agent.zones.get((google.account, calendar_id))
    if not known:
        found = await google.get(f"calendars/{segment(calendar_id)}")
        known = str(found.get("timeZone") or "UTC")
        agent.zones[(google.account, calendar_id)] = known
    return zone_named(known), known


def moment(text: Any, zone: ZoneInfo, what: str = "A time") -> datetime:
    """ISO 8601, with an offset or a trailing Z; a time without one is in ``zone``; a bare date is its midnight."""
    raw = str(text or "").strip()
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), time(0), zone)
        found = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except ValueError:
        raise Refused(f"{what} must be ISO 8601 (2026-09-10 or 2026-09-10T15:00), not {raw!r}.") from None
    return found if found.tzinfo else found.replace(tzinfo=zone)


def day(text: Any, what: str = "A date") -> date:
    raw = str(text or "").strip()
    try:
        if len(raw) != 10:
            raise ValueError
        return date.fromisoformat(raw)
    except ValueError:
        raise Refused(f"{what} must be YYYY-MM-DD, not {raw!r}.") from None


def clock(text: Any, what: str = "A time of day") -> time:
    raw = str(text or "").strip()
    try:
        hour, minute = raw.split(":")
        return time(int(hour), int(minute))
    except ValueError:
        raise Refused(f"{what} must be HH:MM, not {raw!r}.") from None


def google_time(text: Any) -> datetime | None:
    """A time Google wrote (RFC 3339, milliseconds kept), or None."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        found = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


def utc_text(found: datetime) -> str:
    """A moment as Google writes one: UTC, milliseconds, Z."""
    found = found.astimezone(timezone.utc)
    return found.strftime("%Y-%m-%dT%H:%M:%S.") + f"{found.microsecond // 1000:03d}Z"


def local(found: datetime, zone: ZoneInfo) -> str:
    return found.astimezone(zone).isoformat()


def today(zone: ZoneInfo) -> date:
    return datetime.now(zone).date()


def weekdays(names: Any) -> set[int]:
    wanted = [str(n).strip().lower() for n in names or []]
    unknown = [n for n in wanted if n not in WEEKDAYS]
    if unknown:
        raise Refused(f"working_days are weekday names (monday … sunday), not {', '.join(unknown)}.")
    if not wanted:
        raise Refused("working_days names no day.")
    return {WEEKDAYS.index(n) for n in wanted}


def plus_days(found: date, days: int) -> date:
    return found + timedelta(days=days)


def clip(text: Any, most: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= most else text[:most].rstrip() + "…"


def addresses(given: Any) -> list[str]:
    """Attendee addresses, trimmed, each once, in the order given."""
    seen, found = set(), []
    for raw in given or []:
        address = str(raw or "").strip()
        if address and address.lower() not in seen:
            seen.add(address.lower())
            found.append(address)
    bad = [a for a in found if "@" not in a or " " in a]
    if bad:
        raise Refused(f"Attendees are email addresses; these are not: {', '.join(bad)}. Find the address first, "
                      f"never guess one.")
    return found
