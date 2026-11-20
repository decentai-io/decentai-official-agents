"""What every Outlook tool shares: the account's client, the person's time zone, failures as answers, and the text
of a message body."""

from __future__ import annotations

import asyncio
import functools
import html
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from standard import ResourceDenied
from standard.graph import Graph, graph_for
from standard.http import HttpError, segment

from .zones import Zone, resolve

#: Every request asks for immutable ids (an item keeps its id when it moves); these add Outlook's own preferences.
IMMUTABLE = 'IdType="ImmutableId"'
TEXT = {"Prefer": f'{IMMUTABLE}, outlook.body-content-type="text"'}
IN_UTC = {"Prefer": f'{IMMUTABLE}, outlook.timezone="UTC"'}
TEXT_IN_UTC = {"Prefer": f'{IMMUTABLE}, outlook.body-content-type="text", outlook.timezone="UTC"'}

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class Refused(Exception):
    """A function's own refusal, said out loud: its words and its kind."""

    def __init__(self, message: str, kind: str = "invalid", **extra: Any):
        super().__init__(message)
        self.message, self.kind, self.extra = message, kind, extra

    def answer(self) -> tuple[dict[str, Any], str]:
        return {"error": self.message, "kind": self.kind, **self.extra}, "error"


def guarded(method):
    """A function whose failures come back as answers the assistant reads, and whose clients are closed after."""

    @functools.wraps(method)
    async def run(self, call):
        call.clients = []
        try:
            return await method(self, call)
        except Refused as refused:
            return refused.answer()
        except HttpError as failed:
            return failed.answer()
        except ResourceDenied as denied:
            return {"error": str(denied), "kind": denied.code or "refused"}, "error"
        finally:
            for graph in getattr(call, "clients", []):
                graph.close()

    return run


async def client(call: Any, account: Any = None, ask: bool = True) -> Graph:
    """The client over the account wanted (the call's ``account`` input unless named), or a refusal saying why."""
    graph, why = await graph_for(call, account, ask)
    if graph is None:
        raise Refused(why, "auth")
    getattr(call, "clients", []).append(graph)
    return graph


async def work_only(graph: Graph, what: str) -> None:
    """Refuse, before anything is written, a feature that exists only for work or school accounts."""
    if await graph.personal():
        raise Refused(f"{what} needs a work or school (Microsoft 365) account; {graph.account or 'this account'} is "
                      f"a personal Microsoft account (Outlook.com), where Microsoft does not offer it.", "unsupported")


# -- the person's zone ----------------------------------------------------------------------------------------------

async def zone_for(graph: Graph, inputs: dict[str, Any], soft: bool = False) -> Zone:
    """The zone dates are reckoned in: the ``timezone`` input, else the mailbox's own (mailboxSettings), else UTC,
    saying so. ``soft``: any failure to read the mailbox's falls back to UTC (reading mail never fails for it)."""
    cached = getattr(graph, "zone", None)
    wanted = str(inputs.get("timezone") or "").strip()
    if wanted:
        zone = resolve(wanted)
        if zone is None:
            raise Refused(f"timezone {wanted!r} is not a time zone: give an IANA name such as Asia/Dubai or "
                          f"Europe/London.")
        if cached is not None:
            zone.working_days = cached.working_days
        return zone
    if cached is not None:
        return cached
    try:
        settings = await graph.get("me/mailboxSettings", params={"$select": "timeZone,workingHours"})
    except HttpError as failed:
        if not soft and failed.kind not in ("scope", "denied", "not_found", "invalid"):
            raise
        zone = resolve("UTC")
        zone.source = "default"
        zone.note = ("The mailbox's time zone could not be read (" + failed.kind + "), so times are in UTC; pass "
                     "timezone (an IANA name such as Asia/Dubai). Reconnecting the account grants "
                     "MailboxSettings.Read." if failed.kind in ("scope", "denied") else
                     "The mailbox's time zone could not be read, so times are in UTC; pass timezone.")
        graph.zone = zone
        return zone
    raw = str(settings.get("timeZone") or "")
    zone = resolve(raw)
    if zone is None:
        zone = resolve("UTC")
        zone.source = "default"
        zone.note = f"The mailbox's time zone {raw!r} is not one this agent knows, so times are in UTC; pass timezone."
    else:
        zone.source = "mailbox"
    days = ((settings.get("workingHours") or {}).get("daysOfWeek")) or []
    zone.working_days = sorted({WEEKDAYS.index(d.lower()) for d in days if str(d).lower() in WEEKDAYS})
    graph.zone = zone
    return zone


def told(zone: Zone) -> dict[str, Any]:
    """What a result says about the zone it was reckoned in."""
    return {"timezone": zone.name, **({"timezone_note": zone.note} if zone.note else {})}


def today(zone: Zone) -> date:
    return datetime.now(zone.info).date()


# -- times ----------------------------------------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(text or "").strip())
    return text[:-1] + "+00:00" if text.endswith("Z") else text


def moment(text: Any, zone: Zone, what: str = "time") -> datetime:
    """A person's ISO date-time (an offset, Z, or none: then in their zone), or a date (its midnight there)."""
    raw = str(text or "").strip()
    try:
        parsed = datetime.fromisoformat(_clean(raw))
    except ValueError:
        raise Refused(f"{what} must be an ISO 8601 date (2026-11-25) or date-time (2026-11-25T14:00), not "
                      f"{raw!r}.") from None
    return parsed.replace(tzinfo=zone.info) if parsed.tzinfo is None else parsed


def day(text: Any, what: str = "date") -> date:
    raw = str(text or "").strip()
    try:
        return date.fromisoformat(raw[:10]) if len(raw) >= 10 and raw[4] == "-" else date.fromisoformat(raw)
    except (ValueError, IndexError):
        raise Refused(f"{what} must be a date, YYYY-MM-DD, not {raw!r}.") from None


def is_date(text: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(text or "").strip()))


def graph_moment(value: Any) -> datetime | None:
    """Graph's {dateTime, timeZone} as an aware moment; None when there is none."""
    text = str((value or {}).get("dateTime") or "")
    if not text:
        return None
    zone = resolve(str((value or {}).get("timeZone") or "UTC")) or resolve("UTC")
    try:
        parsed = datetime.fromisoformat(_clean(text))
    except ValueError:
        return None
    return parsed.replace(tzinfo=zone.info) if parsed.tzinfo is None else parsed


def graph_day(value: Any, zone: Zone) -> str:
    """The day a {dateTime, timeZone} falls on for the person. A day written as a midnight (a due date, a completion
    To Do rounded) is that day as written; any other moment is the day it is in the person's zone."""
    text = str((value or {}).get("dateTime") or "")
    if not text:
        return ""
    if text[11:19] in ("", "00:00:00"):
        return text[:10]
    found = graph_moment(value)
    return found.astimezone(zone.info).date().isoformat() if found else text[:10]


def utc_text(when: datetime) -> str:
    """Graph's dateTime for a moment in UTC: no offset, to the second."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def utc_z(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(when: datetime | None, zone: Zone) -> str:
    return when.astimezone(zone.info).isoformat(timespec="minutes") if when else ""


def local_text(text: Any, zone: Zone) -> str:
    """Graph's own UTC time stamp (receivedDateTime) in the person's zone."""
    raw = str(text or "")
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(_clean(raw))
    except ValueError:
        return raw
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return local(parsed, zone)


def day_span(start: Any, end: Any, zone: Zone) -> tuple[datetime, datetime]:
    """A period from a person's start and end: a date as its whole day, an end left out as one day."""
    first = moment(start, zone, "start")
    if end in (None, ""):
        last = first + timedelta(days=1)
    else:
        last = moment(end, zone, "end")
        if is_date(end):
            last += timedelta(days=1)
    if last <= first:
        raise Refused("end is not after start.")
    if last - first > timedelta(days=366):
        raise Refused("The period is longer than a year; narrow it.")
    return first, last


# -- text -----------------------------------------------------------------------------------------------------------

def html_text(content: str) -> str:
    """The text a person reads in an HTML body: scripts, styles and comments gone, blocks on their own lines."""
    content = re.sub(r"(?is)<!--.*?-->|<(script|style|head)\b.*?</\1\s*>", "", content)
    content = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h[1-6]|li|table|blockquote)\s*>", "\n", content)
    content = re.sub(r"(?i)<li\b[^>]*>", "- ", content)
    content = re.sub(r"(?i)</t[dh]\s*>", "\t", content)
    content = html.unescape(re.sub(r"<[^>]+>", "", content)).replace("\xa0", " ")
    content = re.sub(r"[ \t]+\n", "\n", content)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


async def body_text(body: Any) -> str:
    """A body's text: as Outlook gave it when it is text (it was asked for text), from HTML when only that came."""
    content = str((body or {}).get("content") or "")
    if str((body or {}).get("contentType") or "").lower() == "html":
        return await asyncio.to_thread(html_text, content)
    return content.replace("\r\n", "\n").strip()


def clip(text: Any, most: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= most else text[:most].rstrip() + "…"


def senders(text: Any) -> str:
    """'Dana@x.example, @harbourline.example' as lower case, tidy."""
    return ",".join(p.strip().lower() for p in str(text or "").replace(";", ",").split(",") if p.strip())


__all__ = ["IN_UTC", "IMMUTABLE", "TEXT", "TEXT_IN_UTC", "Refused", "WEEKDAYS", "body_text", "client", "clip",
           "day", "day_span", "graph_day", "graph_moment", "guarded", "html_text", "is_date", "local", "local_text",
           "moment", "segment", "senders", "today", "told", "utc_text", "utc_z", "work_only", "zone_for"]
