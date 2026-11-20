"""Events: read them in the person's zone, see who is free, find a time, and book, move, invite, answer or cancel.

- **Reading.** Recurring events are expanded (``singleEvents=true``), so each occurrence is a row; an all-day event's
  ``end`` is its last day (Google keeps the day after).
- **Free/busy and slots are code** (``slots.py``): an attendee whose calendar can't be read is named, never free.
- **Creating is idempotent.** The event's id is chosen here from what was asked (or ``request_id``), so asking twice,
  or again after an answer was lost, finds the first event instead of making a second.
- **A booked slot is re-checked** against free/busy just before it is written.
- **Moving keeps the length** unless a new end or duration is given; start and end are always sent together.
- **Cancelling** deletes the event only when this calendar organises it; otherwise it is declined, and the result
  says which happened.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from standard import ResourceDenied, ToolBase
from standard.google import Google, HttpError, segment

from .common import (Refused, addresses, answers, calendar_zone, client, clip, clock, day, local, moment,
                     plus_days, refused_resource, today, weekdays)
from .slots import find_slots, overlapping

WINDOW_DAYS = 31
FREEBUSY_MOST = 50


def event_id_for(seed: str) -> str:
    """An event id Google takes: base32hex (0-9, a-v), 52 characters, the same for the same seed."""
    return base64.b32hexencode(hashlib.sha256(seed.encode("utf-8")).digest()).decode("ascii").lower().rstrip("=")


def meet_of(event: dict[str, Any]) -> tuple[str, str]:
    """(the Meet link, or "", and the state of its creation: success, pending, failure, or "")."""
    data = event.get("conferenceData") or {}
    link = str(event.get("hangoutLink") or "")
    if not link:
        link = next((str(p.get("uri") or "") for p in data.get("entryPoints") or []
                     if p.get("entryPointType") == "video"), "")
    state = str(((data.get("createRequest") or {}).get("status") or {}).get("statusCode") or "")
    return link, state or ("success" if link else "")


def me(event: dict[str, Any]) -> dict[str, Any] | None:
    return next((a for a in event.get("attendees") or [] if a.get("self") is True), None)


def organises(event: dict[str, Any]) -> bool:
    return (event.get("organizer") or {}).get("self") is True


def span(event: dict[str, Any], zone) -> tuple[str, str, bool]:
    """(start, end, all day) as the person reads them: times in their zone; an all-day event's last day."""
    start, end = event.get("start") or {}, event.get("end") or {}
    if start.get("date"):
        last = str(end.get("date") or start["date"])
        try:
            last = plus_days(day(last), -1).isoformat()
        except Refused:
            pass
        return str(start["date"]), max(last, str(start["date"])), True
    times = []
    for side in (start, end):
        found = side.get("dateTime")
        try:
            times.append(local(moment(found, zone), zone) if found else "")
        except Refused:
            times.append(str(found or ""))
    return times[0], times[1], False


def row_of(event: dict[str, Any], zone) -> dict[str, Any]:
    start, end, all_day = span(event, zone)
    mine = me(event)
    link, _ = meet_of(event)
    row = {"event_id": str(event.get("id") or ""), "summary": str(event.get("summary") or "(no title)"),
           "start": start, "end": end, "all_day": all_day, "status": str(event.get("status") or ""),
           "organizer": str((event.get("organizer") or {}).get("email") or ""), "i_organise": organises(event),
           "attendees": [str(a.get("email") or "") for a in event.get("attendees") or [] if a.get("email")],
           "link": str(event.get("htmlLink") or "")}
    for key, value in (("location", str(event.get("location") or "")), ("meet_link", link),
                       ("my_response", str((mine or {}).get("responseStatus") or "")),
                       ("series_id", str(event.get("recurringEventId") or ""))):
        if value:
            row[key] = value
    if event.get("recurrence"):
        row["recurrence"] = [str(r) for r in event["recurrence"]]
    return row


class EventsTool(ToolBase):
    id = "events"

    # -- what every function starts with ------------------------------------------------------------------------
    async def opened(self, call, account: Any = None) -> tuple[Google, str, Any, str]:
        google = await client(call, "calendar", account)
        calendar_id = str(call.inputs.get("calendar_id") or "primary").strip() or "primary"
        zone, name = await calendar_zone(self.agent, google, calendar_id, call.inputs.get("timezone") or "")
        return google, calendar_id, zone, name

    @staticmethod
    def path(calendar_id: str, event_id: str = "") -> str:
        return f"calendars/{segment(calendar_id)}/events" + (f"/{segment(event_id)}" if event_id else "")

    async def read(self, google: Google, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        """The event by id, or None when Google has none (a cancelled one is returned, with its status)."""
        found = await google.request("GET", self.path(calendar_id, event_id), allow=(404, 410), raw=True)
        if found.status_code in (404, 410):
            return None
        return found.json()

    async def trail(self, call, google: Google, calendar_id: str, event: dict[str, Any], zone, status: str) -> str:
        """A booking record: what this agent did to which event, for the person's trail."""
        start, end, _ = span(event, zone)
        try:
            return await call.records.create("booking", {
                "event_id": str(event.get("id") or ""), "calendar_id": calendar_id, "account": google.account,
                "start": start, "end": end, "status": status, "summary": str(event.get("summary") or ""),
                "attendees": ", ".join(row_of(event, zone)["attendees"])})
        except ResourceDenied:
            return ""

    # -- reads ---------------------------------------------------------------------------------------------------
    @answers
    async def list(self, call):
        google, calendar_id, zone, name = await self.opened(call)
        inputs = call.inputs
        low = moment(inputs["time_min"], zone, "time_min") if inputs.get("time_min") else moment(
            today(zone).isoformat(), zone)
        high = moment(inputs["time_max"], zone, "time_max") if inputs.get("time_max") else low + timedelta(days=1)
        if high <= low:
            raise Refused("time_max must be after time_min.")
        params = {"timeMin": low.isoformat(), "timeMax": high.isoformat(), "singleEvents": "true",
                  "orderBy": "startTime", "maxResults": int(inputs.get("max_results") or 50), "timeZone": name}
        if inputs.get("query"):
            params["q"] = str(inputs["query"])
        if inputs.get("page_token"):
            params["pageToken"] = str(inputs["page_token"])
        page = await google.get(self.path(calendar_id), params=params)
        result = {"account": google.account, "calendar_id": calendar_id, "timezone": name,
                  "today": today(zone).isoformat(), "from": local(low, zone), "to": local(high, zone),
                  "events": [row_of(e, zone) for e in page.get("items") or [] if e.get("status") != "cancelled"]}
        if page.get("nextPageToken"):
            result["next_page_token"] = str(page["nextPageToken"])
        return result, "success"

    @answers
    async def get(self, call):
        google, calendar_id, zone, name = await self.opened(call)
        event = await google.get(self.path(calendar_id, str(call.inputs["event_id"])))
        row = row_of(event, zone)
        row["timezone"] = name
        row["responses"] = [{"email": str(a.get("email") or ""),
                             "response": str(a.get("responseStatus") or "needsAction"),
                             "organizer": a.get("organizer") is True, "optional": a.get("optional") is True}
                            for a in event.get("attendees") or []]
        if event.get("description"):
            row["description"] = clip(event["description"], 4000)
        return row, "success"

    # -- who is free ---------------------------------------------------------------------------------------------
    async def busy(self, google: Google, low: datetime, high: datetime, own: list[str], attendees: list[str],
                   zone_name: str) -> tuple[list[tuple[str, datetime, datetime]], list[dict[str, str]]]:
        """Busy periods of the account's calendars and each attendee, and who could not be checked. Google answers
        per calendar; an error entry means this account can't read that calendar."""
        wanted: list[str] = []
        for who in [*own, *attendees]:
            if who.lower() not in {w.lower() for w in wanted} and not (
                    who.lower() == google.account.lower() and "primary" in wanted):
                wanted.append(who)
        if len(wanted) > FREEBUSY_MOST:
            raise Refused(f"Google checks at most {FREEBUSY_MOST} calendars at once; {len(wanted)} were asked.")
        answer = await google.post("freeBusy", json={
            "timeMin": low.isoformat(), "timeMax": high.isoformat(), "timeZone": zone_name,
            "items": [{"id": w} for w in wanted]})
        calendars = answer.get("calendars") or {}
        lowered = {str(k).lower(): v for k, v in calendars.items()}
        busy, unchecked = [], []
        for who in wanted:
            entry = calendars.get(who) or lowered.get(who.lower())
            if entry is None and who == "primary":
                entry = lowered.get(google.account.lower())
            shown = google.account if who == "primary" else who
            if entry is None or entry.get("errors"):
                reasons = [str(e.get("reason") or "") for e in (entry or {}).get("errors") or []]
                unchecked.append({"who": shown, "reason": ", ".join(r for r in reasons if r) or "not readable"})
                continue
            for period in entry.get("busy") or []:
                busy.append((shown, moment(period.get("start"), timezone.utc), moment(period.get("end"), timezone.utc)))
        return busy, unchecked

    @answers
    async def availability(self, call):
        google, calendar_id, zone, name = await self.opened(call)
        inputs = call.inputs
        low, high = moment(inputs["time_min"], zone, "time_min"), moment(inputs["time_max"], zone, "time_max")
        if high <= low:
            raise Refused("time_max must be after time_min.")
        own = self.own(inputs, calendar_id)
        busy, unchecked = await self.busy(google, low, high, own, addresses(inputs.get("attendees")), name)
        return {"timezone": name, "busy": [{"who": w, "start": local(a, zone), "end": local(b, zone)}
                                           for w, a, b in sorted(busy, key=lambda b: (b[1], b[0]))],
                "unchecked": unchecked}, "success"

    @staticmethod
    def own(inputs: dict[str, Any], calendar_id: str) -> list[str]:
        """The account's own calendars that count as busy: primary, the one booked on, and any named."""
        own = ["primary"] + ([calendar_id] if calendar_id != "primary" else [])
        return own + [str(c) for c in inputs.get("calendars") or [] if str(c) not in own]

    @answers
    async def find_times(self, call):
        google, calendar_id, zone, name = await self.opened(call)
        inputs = call.inputs
        from_date, to_date = day(inputs["from_date"], "from_date"), day(inputs["to_date"], "to_date")
        if to_date < from_date:
            raise Refused(f"to_date ({to_date}) is before from_date ({from_date}).")
        if (to_date - from_date).days >= WINDOW_DAYS:
            raise Refused(f"The window is {(to_date - from_date).days + 1} days; at most {WINDOW_DAYS} are looked "
                          f"at once. Narrow it.")
        hours = inputs.get("working_hours") or {}
        start_clock = clock(hours.get("start") or "09:00", "working_hours.start")
        end_clock = clock(hours.get("end") or "17:00", "working_hours.end")
        if end_clock <= start_clock:
            raise Refused(f"working_hours end ({end_clock:%H:%M}) before they start ({start_clock:%H:%M}).")
        avoid = [(clock(a["start"], "avoid.start"), clock(a["end"], "avoid.end")) for a in inputs.get("avoid") or []]
        days = weekdays(inputs.get("working_days") or ["monday", "tuesday", "wednesday", "thursday", "friday"])
        now = datetime.now(timezone.utc)
        not_before = moment(inputs["not_before"], zone, "not_before") if inputs.get("not_before") else now
        attendees = addresses(inputs.get("attendees"))
        buffer = timedelta(minutes=int(inputs.get("buffer_minutes") or 0))
        low = datetime.combine(from_date, start_clock, zone) - buffer
        high = datetime.combine(to_date, end_clock, zone) + buffer
        busy, unchecked = await self.busy(google, low, high, self.own(inputs, calendar_id), attendees, name)
        if any(u["who"].lower() == google.account.lower() for u in unchecked):
            raise Refused("The account's own calendar could not be read, so no time can be proposed.", "denied")
        slots = find_slots(duration=timedelta(minutes=int(inputs["duration_minutes"])), from_date=from_date,
                           to_date=to_date, zone=zone, days=days, working_start=start_clock, working_end=end_clock,
                           avoid=avoid, buffer=buffer, busy=[(a, b) for _, a, b in busy], not_before=not_before,
                           limit=int(inputs.get("max_candidates") or 5), per_day=int(inputs.get("per_day") or 2))
        candidates = []
        for slot in slots:
            try:
                ref = await call.records.create("proposal", {
                    "start": slot["start"], "end": slot["end"], "timezone": name, "account": google.account,
                    "calendar_id": calendar_id, "status": "offered", "attendees": ", ".join(attendees)})
            except ResourceDenied as failed:
                raise refused_resource(failed) from None
            candidates.append({"proposal_ref": ref, **slot})
        return {"timezone": name, "candidates": candidates, "unchecked": unchecked}, "success"

    # -- creating ------------------------------------------------------------------------------------------------
    def times(self, inputs: dict[str, Any], zone, name: str) -> tuple[dict, dict, datetime | None, datetime | None]:
        """Google's start and end of a new event, from start with end or duration, or all day."""
        if not inputs.get("start"):
            raise Refused("Give start, with end or duration_minutes (or a proposal_ref).")
        if inputs.get("all_day"):
            first = day(inputs["start"], "start")
            last = day(inputs["end"], "end") if inputs.get("end") else first
            if last < first:
                raise Refused(f"end ({last}) is before start ({first}).")
            return {"date": first.isoformat()}, {"date": plus_days(last, 1).isoformat()}, None, None
        start = moment(inputs["start"], zone, "start")
        if inputs.get("end"):
            end = moment(inputs["end"], zone, "end")
        elif inputs.get("duration_minutes"):
            end = start + timedelta(minutes=int(inputs["duration_minutes"]))
        else:
            raise Refused("Give end or duration_minutes with start.")
        if end <= start:
            raise Refused(f"The event would end ({end.isoformat()}) before it starts ({start.isoformat()}).")
        return ({"dateTime": start.isoformat(), "timeZone": name}, {"dateTime": end.isoformat(), "timeZone": name},
                start, end)

    async def insert(self, google: Google, calendar_id: str, body: dict[str, Any], params: dict[str, Any],
                     seed: str) -> tuple[dict[str, Any], bool]:
        """(the event, whether it already existed): written under an id chosen from ``seed``, so the same request
        twice is one event. A write whose answer was lost is looked up by that id, and sent once more only when
        Google has no such event, which the id makes safe."""
        for attempt in range(3):
            event_id = event_id_for(seed if attempt == 0 else f"{seed}#{attempt}")
            found = await self.read(google, calendar_id, event_id)
            if found is not None and found.get("status") != "cancelled":
                return found, True
            if found is None:
                break
            # That id belonged to an event since cancelled: the same request made anew gets a new id.
        else:
            raise Refused("The same event was made and cancelled three times; give a request_id to make it again.")
        body = {**body, "id": event_id}
        path = self.path(calendar_id)
        try:
            return await google.post(path, params=params, json=body), False
        except HttpError as failed:
            if failed.kind not in ("unknown", "conflict"):
                raise
            lost = failed
        found = await self.read(google, calendar_id, event_id)
        if found is not None:
            return found, lost.kind == "conflict"
        if lost.kind == "conflict":
            raise lost
        return await google.post(path, params=params, json=body), False

    async def meet_settled(self, google: Google, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """A Meet link Google is still making, given two seconds to arrive."""
        for _ in range(2):
            if meet_of(event)[1] != "pending":
                break
            await asyncio.sleep(1)
            event = await google.get(self.path(calendar_id, str(event["id"])), params={"conferenceDataVersion": 1})
        return event

    @staticmethod
    def seed(google: Google, calendar_id: str, inputs: dict[str, Any], body: dict[str, Any]) -> str:
        if inputs.get("request_id"):
            return f"{google.account}|{calendar_id}|{inputs['request_id']}"
        return json.dumps([google.account, calendar_id, body.get("summary"), body.get("start"), body.get("end"),
                           sorted(a["email"].lower() for a in body.get("attendees") or []),
                           inputs.get("proposal_ref") or ""], ensure_ascii=False)

    @staticmethod
    def details(inputs: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        for field in ("description", "location"):
            if inputs.get(field):
                body[field] = str(inputs[field])
        if inputs.get("recurrence"):
            rule = str(inputs["recurrence"]).strip()
            rule = rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"
            body["recurrence"] = [rule]
        if inputs.get("show_as") == "free":
            body["transparency"] = "transparent"
        return body

    @answers
    async def create(self, call):
        """An event on the person's own calendar with nobody invited: a block of time, a reminder, a day off."""
        google, calendar_id, zone, name = await self.opened(call)
        inputs = call.inputs
        start, end, _, _ = self.times(inputs, zone, name)
        body = self.details(inputs, {"summary": str(inputs["summary"]), "start": start, "end": end})
        event, already = await self.insert(google, calendar_id, body, {"sendUpdates": "none"},
                                           self.seed(google, calendar_id, inputs, body))
        ref = await self.trail(call, google, calendar_id, event, zone, "created")
        row = row_of(event, zone)
        return {"event_id": row["event_id"], "calendar_id": calendar_id, "start": row["start"], "end": row["end"],
                "all_day": row["all_day"], "link": row["link"], "already": already,
                **({"booking_ref": ref} if ref else {})}, "success"

    @answers
    async def book(self, call):
        """A meeting with attendees, from a proposal or a time: the slot re-checked, then the invitations sent."""
        inputs = call.inputs
        proposal_ref = str(inputs.get("proposal_ref") or "")
        proposal: dict[str, Any] = {}
        if proposal_ref:
            try:
                proposal = (await call.records.read("proposal", proposal_ref))["fields"]
            except ResourceDenied as failed:
                raise Refused(f"No proposal {proposal_ref}: {failed}", "not_found") from None
            if proposal.get("status") == "booked":
                raise Refused(f"That proposal was already booked (event {proposal.get('event_id')}).", "conflict")
        google, calendar_id, zone, name = await self.opened(call, inputs.get("account") or proposal.get("account"))
        if proposal:
            if proposal.get("account") and proposal["account"].lower() != google.account.lower():
                raise Refused(f"That proposal was found on {proposal['account']}, not {google.account}.")
            calendar_id = str(inputs.get("calendar_id") or proposal.get("calendar_id") or calendar_id)
            asked = {**inputs, "start": proposal["start"], "end": proposal["end"], "all_day": False}
            asked.pop("duration_minutes", None)
            attendees = addresses(inputs.get("attendees") or [a for a in str(proposal.get("attendees") or "")
                                                              .split(",")])
        else:
            asked, attendees = inputs, addresses(inputs.get("attendees"))
        attendees = [a for a in attendees if a.lower() != google.account.lower()]
        if not attendees:
            raise Refused("A meeting names its attendees; with nobody to invite, use events_create.")
        start, end, low, high = self.times(asked, zone, name)
        body = self.details(inputs, {"summary": str(inputs["summary"]), "start": start, "end": end,
                                     "attendees": [{"email": a} for a in attendees]})
        send = str(inputs.get("send_updates") or "all")
        params: dict[str, Any] = {"sendUpdates": send}
        seed = self.seed(google, calendar_id, inputs, body)
        if inputs.get("meet"):
            body["conferenceData"] = {"createRequest": {"requestId": event_id_for(seed)[:40],
                                                        "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
            params["conferenceDataVersion"] = 1
        unchecked: list[dict[str, str]] = []
        made = await self.read(google, calendar_id, event_id_for(seed))
        if not (made and made.get("status") != "cancelled") and low is not None:
            # The slot as it is now, not as it was when proposed.
            busy, unchecked = await self.busy(google, low, high, self.own(inputs, calendar_id), attendees, name)
            clash = overlapping([(a, b) for _, a, b in busy], low, high)
            if clash and not inputs.get("book_anyway"):
                taken = [{"who": w, "start": local(a, zone), "end": local(b, zone)} for w, a, b in busy
                         if a < high and b > low]
                return {"error": "That time is no longer free: " + "; ".join(
                    f"{t['who']} {t['start']}–{t['end']}" for t in taken) + ". Nothing was booked. Find another time "
                    "(events_find_times), or book it anyway only if the person says so.", "kind": "conflict"}, "error"
        event, already = await self.insert(google, calendar_id, body, params, seed)
        if inputs.get("meet"):
            event = await self.meet_settled(google, calendar_id, event)
        ref = await self.trail(call, google, calendar_id, event, zone, "created")
        if proposal_ref:
            try:
                await call.records.update("proposal", proposal_ref, {"status": "booked",
                                                                     "event_id": str(event.get("id") or "")})
            except ResourceDenied:
                pass
        row = row_of(event, zone)
        link, state = meet_of(event)
        result = {"event_id": row["event_id"], "calendar_id": calendar_id, "start": row["start"], "end": row["end"],
                  "link": row["link"], "invited": row["attendees"], "notified": send, "already": already,
                  "unchecked": unchecked, **({"booking_ref": ref} if ref else {})}
        if inputs.get("meet"):
            result["meet"] = state or "failure"
            if link:
                result["meet_link"] = link
        return result, "success"

    # -- changing ------------------------------------------------------------------------------------------------
    async def mine(self, call, google: Google, calendar_id: str, what: str, may: str = "") -> dict[str, Any]:
        """The event, when this calendar may change it: it organises it, or the organiser let guests do ``may``."""
        event = await google.get(self.path(calendar_id, str(call.inputs["event_id"])))
        if event.get("status") == "cancelled":
            raise Refused("That event is cancelled.", "not_found")
        if not organises(event) and not (may and event.get(may) is True):
            organizer = (event.get("organizer") or {}).get("email") or "someone else"
            raise Refused(f"Only the organiser can {what}: {organizer} organises this event, not {google.account}. "
                          f"Answer the invitation (events_respond) or ask the organiser.", "denied")
        return event

    @answers
    async def update(self, call):
        google, calendar_id, zone, name = await self.opened(call)
        inputs = call.inputs
        event = await self.mine(call, google, calendar_id, "move or edit it", "guestsCanModify")
        patch: dict[str, Any] = {}
        for field in ("summary", "description", "location"):
            if inputs.get(field) is not None and str(inputs[field]) != str(event.get(field) or ""):
                patch[field] = str(inputs[field])
        if inputs.get("start") or inputs.get("end") or inputs.get("duration_minutes"):
            patch.update(self.moved(event, inputs, zone, name))
        if not patch:
            raise Refused("Nothing to change: give a summary, description, location, start, end or duration_minutes.")
        updated = await google.patch(self.path(calendar_id, str(event["id"])), json=patch,
                                     params={"sendUpdates": str(inputs.get("send_updates") or "all")})
        ref = await self.trail(call, google, calendar_id, updated, zone, "updated")
        row = row_of(updated, zone)
        return {"event_id": row["event_id"], "start": row["start"], "end": row["end"], "all_day": row["all_day"],
                "link": row["link"], "whole_series": bool(event.get("recurrence")),
                "notified": str(inputs.get("send_updates") or "all"), **({"booking_ref": ref} if ref else {})}, "success"

    def moved(self, event: dict[str, Any], inputs: dict[str, Any], zone, name: str) -> dict[str, Any]:
        """New start and end together: the length kept unless a new end or duration is given."""
        start, end = event.get("start") or {}, event.get("end") or {}
        if start.get("date"):
            first, after = day(start["date"]), day(end.get("date") or start["date"])
            length = max(1, (after - first).days)
            new_first = day(inputs["start"], "start") if inputs.get("start") else first
            if inputs.get("end"):
                new_after = plus_days(day(inputs["end"], "end"), 1)
            elif inputs.get("duration_minutes"):
                raise Refused("An all-day event is moved by dates: give start and end (YYYY-MM-DD).")
            else:
                new_after = plus_days(new_first, length)
            if new_after <= new_first:
                raise Refused(f"The event would end before it starts ({new_first} to {plus_days(new_after, -1)}).")
            return {"start": {"date": new_first.isoformat()}, "end": {"date": new_after.isoformat()}}
        old_start, old_end = moment(start.get("dateTime"), zone), moment(end.get("dateTime"), zone)
        new_start = moment(inputs["start"], zone, "start") if inputs.get("start") else old_start
        if inputs.get("end"):
            new_end = moment(inputs["end"], zone, "end")
        elif inputs.get("duration_minutes"):
            new_end = new_start + timedelta(minutes=int(inputs["duration_minutes"]))
        else:
            new_end = new_start + (old_end - old_start)
        if new_end <= new_start:
            raise Refused(f"The event would end ({new_end.isoformat()}) before it starts ({new_start.isoformat()}).")
        # The event keeps its own zone (what Google shows it in), unless the person named another.
        kept = name if inputs.get("timezone") else str(start.get("timeZone") or name)
        return {"start": {"dateTime": new_start.isoformat(), "timeZone": kept},
                "end": {"dateTime": new_end.isoformat(), "timeZone": kept}}

    @answers
    async def attendees(self, call):
        google, calendar_id, zone, _ = await self.opened(call)
        inputs = call.inputs
        add, remove = addresses(inputs.get("add")), addresses(inputs.get("remove"))
        if not add and not remove:
            raise Refused("Name someone to add or remove.")
        event = await self.mine(call, google, calendar_id, "remove attendees" if remove else "invite others",
                                "" if remove else "guestsCanInviteOthers")
        kept = list(event.get("attendees") or [])
        present = {str(a.get("email") or "").lower() for a in kept}
        missing = [r for r in remove if r.lower() not in present]
        kept = [a for a in kept if str(a.get("email") or "").lower() not in {r.lower() for r in remove}]
        added = [a for a in add if a.lower() not in present]
        kept += [{"email": a} for a in added]
        send = str(inputs.get("send_updates") or "all")
        updated = await google.patch(self.path(calendar_id, str(event["id"])), json={"attendees": kept},
                                     params={"sendUpdates": send})
        ref = await self.trail(call, google, calendar_id, updated, zone, "updated")
        return {"event_id": str(updated.get("id") or ""), "attendees": row_of(updated, zone)["attendees"],
                "added": added, "removed": [r for r in remove if r not in missing], "not_attending": missing,
                "notified": send, **({"booking_ref": ref} if ref else {})}, "success"

    async def answer(self, google: Google, calendar_id: str, event: dict[str, Any], response: str,
                     comment: str, send: str) -> dict[str, Any]:
        """The account's own answer to an invitation: its attendee entry changed, the others sent back as they
        were (Google takes the attendee list whole)."""
        listed = []
        for attendee in event.get("attendees") or []:
            attendee = dict(attendee)
            if attendee.get("self") is True:
                attendee["responseStatus"] = response
                if comment:
                    attendee["comment"] = comment
            listed.append(attendee)
        return await google.patch(self.path(calendar_id, str(event["id"])), json={"attendees": listed},
                                  params={"sendUpdates": send})

    @answers
    async def respond(self, call):
        google, calendar_id, zone, _ = await self.opened(call)
        inputs = call.inputs
        event = await google.get(self.path(calendar_id, str(inputs["event_id"])))
        if me(event) is None:
            if organises(event):
                raise Refused("This account organises the event, so there is no invitation to answer.")
            raise Refused(f"{google.account} is not among this event's attendees, so it has nothing to answer.",
                          "denied")
        send = str(inputs.get("send_updates") or "all")
        updated = await self.answer(google, calendar_id, event, str(inputs["response"]),
                                    str(inputs.get("comment") or ""), send)
        await self.trail(call, google, calendar_id, updated, zone, "responded")
        return {"event_id": str(updated.get("id") or ""), "response": str((me(updated) or {}).get("responseStatus")
                                                                          or inputs["response"]),
                "organizer": str((updated.get("organizer") or {}).get("email") or ""), "notified": send}, "success"

    @answers
    async def cancel(self, call):
        google, calendar_id, zone, _ = await self.opened(call)
        inputs = call.inputs
        event = await google.get(self.path(calendar_id, str(inputs["event_id"])))
        if event.get("status") == "cancelled":
            return {"event_id": str(event.get("id") or ""), "done": "already_cancelled", "notified": "none",
                    "told": []}, "success"
        send = str(inputs.get("send_updates") or "all")
        others = [a for a in row_of(event, zone)["attendees"] if a.lower() != google.account.lower()]
        if organises(event):
            await google.delete(self.path(calendar_id, str(event["id"])), params={"sendUpdates": send})
            await self.trail(call, google, calendar_id, event, zone, "cancelled")
            return {"event_id": str(event["id"]), "done": "cancelled", "notified": send,
                    "told": others if send != "none" else [],
                    "whole_series": bool(event.get("recurrence"))}, "success"
        if me(event) is None:
            organizer = (event.get("organizer") or {}).get("email") or "someone else"
            raise Refused(f"{google.account} neither organises this event ({organizer} does) nor is invited to it, "
                          f"so it can't cancel or decline it.", "denied")
        updated = await self.answer(google, calendar_id, event, "declined", str(inputs.get("comment") or ""), send)
        await self.trail(call, google, calendar_id, updated, zone, "declined")
        organizer = str((event.get("organizer") or {}).get("email") or "")
        return {"event_id": str(event["id"]), "done": "declined", "notified": send,
                "told": [organizer] if organizer and send != "none" else [],
                "why": f"{organizer or 'Someone else'} organises it, so only they can cancel it for everyone; "
                       f"it was declined for {google.account} instead."}, "success"
