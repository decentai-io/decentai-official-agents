"""The calendar: what is on (every calendar of the account, recurring meetings expanded, all-day events as days), one
event whole, free/busy, finding a time, and creating, moving, cancelling and answering events on the person's go-ahead.

Graph is asked for UTC and every time shown is converted here, in the person's zone (the ``timezone`` input, else
the mailbox's own, else UTC, said)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from standard import ToolBase
from standard.graph import address, bare
from standard.http import HttpError, segment

from .common import (IN_UTC, TEXT_IN_UTC, WEEKDAYS, Refused, body_text, client, clip, day, day_span, graph_moment,
                     guarded, is_date, local, moment, today, told, utc_text, utc_z, work_only, zone_for)
from .slots import clock, find_slots

EVENT = ("id,subject,start,end,isAllDay,location,organizer,attendees,isCancelled,isOrganizer,responseStatus,webLink,"
         "showAs,type,seriesMasterId,isOnlineMeeting,onlineMeeting,recurrence")
#: What counts as unavailable: tentative and out-of-office do; "free" and "working elsewhere" do not.
BUSY = {"busy", "tentative", "oof"}
RESPONSES = {"organizer": "organizer", "accepted": "accepted", "tentativelyAccepted": "tentative",
             "declined": "declined", "notResponded": "not_responded", "none": "none"}
ANSWERS = {"accept": "accept", "tentative": "tentativelyAccept", "decline": "decline"}
ORDINALS = {"first": "first", "second": "second", "third": "third", "fourth": "fourth", "last": "last"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


def event_row(event: dict[str, Any], zone) -> dict[str, Any]:
    all_day = bool(event.get("isAllDay"))
    if all_day:
        # An all-day event is days, not moments: it floats with the reader. Its end is exclusive in Outlook; the row
        # names its last day.
        first = str((event.get("start") or {}).get("dateTime") or "")[:10]
        after = str((event.get("end") or {}).get("dateTime") or "")[:10]
        start = first
        end = (date.fromisoformat(after) - timedelta(days=1)).isoformat() if after else first
    else:
        start, end = local(graph_moment(event.get("start")), zone), local(graph_moment(event.get("end")), zone)
    kind = str(event.get("type") or "singleInstance")
    found = {
        "event_id": str(event.get("id") or ""),
        "subject": str(event.get("subject") or "(no title)"),
        "start": start, "end": end, "all_day": all_day,
        "location": str((event.get("location") or {}).get("displayName") or ""),
        "organizer": address(event.get("organizer")),
        "is_organizer": bool(event.get("isOrganizer")),
        "my_response": RESPONSES.get(str((event.get("responseStatus") or {}).get("response") or "none"), "none"),
        "attendees": len(event.get("attendees") or []),
        "show_as": str(event.get("showAs") or ""),
        "recurring": kind in ("occurrence", "exception", "seriesMaster"),
        "link": str(event.get("webLink") or ""),
    }
    if event.get("seriesMasterId"):
        found["series_id"] = str(event["seriesMasterId"])
    join = str((event.get("onlineMeeting") or {}).get("joinUrl") or "")
    if join:
        found["online_meeting_url"] = join
    if event.get("isCancelled"):
        found["cancelled"] = True
    return found


def described(recurrence: Any) -> str:
    """A recurrence in words, written by code: "every 2 weeks on Monday, Wednesday, until 2027-01-31"."""
    pattern, span = (recurrence or {}).get("pattern") or {}, (recurrence or {}).get("range") or {}
    kind, every = str(pattern.get("type") or ""), int(pattern.get("interval") or 1)
    days = ", ".join(d.capitalize() for d in pattern.get("daysOfWeek") or [])
    nth = ORDINALS.get(str(pattern.get("index") or "first"), "first")
    month = MONTHS[int(pattern.get("month") or 1) - 1] if pattern.get("month") else ""
    unit = lambda one, many: one if every == 1 else f"{every} {many}"  # noqa: E731
    text = {
        "daily": f"every {unit('day', 'days')}",
        "weekly": f"every {unit('week', 'weeks')} on {days}",
        "absoluteMonthly": f"every {unit('month', 'months')} on day {pattern.get('dayOfMonth')}",
        "relativeMonthly": f"every {unit('month', 'months')} on the {nth} {days}",
        "absoluteYearly": f"every year on {month} {pattern.get('dayOfMonth')}",
        "relativeYearly": f"every year on the {nth} {days} of {month}",
    }.get(kind, kind)
    if span.get("startDate"):
        text += f", from {span['startDate']}"
    if span.get("type") == "endDate" and span.get("endDate"):
        text += f", until {span['endDate']}"
    elif span.get("type") == "numbered":
        text += f", {span.get('numberOfOccurrences')} times"
    return text


async def calendar_base(graph, wanted: Any) -> tuple[str, str]:
    """The path of the calendar named (by name or id), or the default one."""
    wanted = str(wanted or "").strip()
    if not wanted:
        return "me/calendar", ""
    found = await graph.pages("me/calendars", params={"$select": "id,name,isDefaultCalendar,canEdit,owner"}, most=200)
    for key in ("id", "name"):
        hits = [c for c in found if str(c.get(key) or "").lower() == wanted.lower()]
        if hits:
            return f"me/calendars/{segment(hits[0]['id'])}", str(hits[0].get("name") or "")
    names = ", ".join(str(c.get("name") or "") for c in found)
    raise Refused(f"There is no calendar {wanted!r}. Calendars: {names}.", "not_found")


async def own_busy(graph, zone, first: datetime, last: datetime) -> list[tuple[datetime, datetime]]:
    """The account's busy periods, read from its own calendar: what a personal account has instead of free/busy."""
    events = await graph.pages("me/calendar/calendarView", headers=IN_UTC, most=1000, params={
        "startDateTime": utc_z(first), "endDateTime": utc_z(last), "$top": 100,
        "$select": "id,start,end,isAllDay,showAs,isCancelled,responseStatus"})
    found = []
    for event in events:
        response = str((event.get("responseStatus") or {}).get("response") or "")
        if event.get("isCancelled") or response == "declined" or str(event.get("showAs") or "busy") not in BUSY:
            continue
        if event.get("isAllDay"):
            start = datetime.combine(date.fromisoformat(str(event["start"]["dateTime"])[:10]), datetime.min.time(),
                                     zone.info)
            end = datetime.combine(date.fromisoformat(str(event["end"]["dateTime"])[:10]), datetime.min.time(),
                                   zone.info)
        else:
            start, end = graph_moment(event.get("start")), graph_moment(event.get("end"))
        if start and end:
            found.append((start, end))
    return found


async def busy_periods(graph, zone, first, last, people):
    """(busy rows, unchecked people, where it was read): free/busy (getSchedule) on a work account; on a personal
    account, or where free/busy is refused, the account's own calendar, and the others unchecked, saying why."""
    me = await graph.me()
    others = [p for p in dict.fromkeys(str(p).strip() for p in people or []) if p and p.lower() not in me.mine]
    rows, unchecked = [], []
    if await graph.personal():
        reason = "Seeing another person's free/busy needs a work or school (Microsoft 365) account."
    else:
        try:
            answer = await graph.request("POST", "me/calendar/getSchedule", write=False, headers=IN_UTC, json={
                "schedules": [me.address] + others,
                "startTime": {"dateTime": utc_text(first), "timeZone": "UTC"},
                "endTime": {"dateTime": utc_text(last), "timeZone": "UTC"}, "availabilityViewInterval": 30})
        except HttpError as failed:
            if failed.kind in ("auth", "rate", "scope"):
                raise
            reason = f"Microsoft would not give free/busy: {failed.message}"
        else:
            by = {str(i.get("scheduleId") or "").lower(): i for i in answer.get("value") or []}
            own_failed = False
            for email in [me.address] + others:
                item = by.get(email.lower())
                if item is None or item.get("error"):
                    if email == me.address:
                        own_failed = True
                        continue
                    unchecked.append({"email": email, "reason": str(((item or {}).get("error") or {}).get("message")
                                                                    or "Its calendar could not be read.")})
                    continue
                for entry in item.get("scheduleItems") or []:
                    if str(entry.get("status") or "") in BUSY:
                        start, end = graph_moment(entry.get("start")), graph_moment(entry.get("end"))
                        if start and end:
                            rows.append({"email": email, "start": start, "end": end})
            if own_failed:
                rows += [{"email": me.address, "start": s, "end": e} for s, e in await own_busy(graph, zone, first,
                                                                                                  last)]
            return rows, unchecked, "free/busy"
    rows = [{"email": me.address, "start": s, "end": e} for s, e in await own_busy(graph, zone, first, last)]
    return rows, [{"email": p, "reason": reason} for p in others], "own calendar"


class CalendarTool(ToolBase):
    id = "calendar"

    # -- reads ---------------------------------------------------------------------------------------------------
    @guarded
    async def list(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        token = str(call.inputs.get("page_token") or "")
        first, last = day_span(call.inputs.get("start") or today(zone).isoformat(), call.inputs.get("end"), zone)
        base, name = await calendar_base(graph, call.inputs.get("calendar"))
        if token:
            page = await graph.get("", absolute=token, headers=IN_UTC)
        else:
            page = await graph.get(f"{base}/calendarView", headers=IN_UTC, params={
                "startDateTime": utc_z(first), "endDateTime": utc_z(last), "$orderby": "start/dateTime",
                "$top": int(call.inputs.get("max_results") or 50), "$select": EVENT})
        events = [event_row(e, zone) for e in page.get("value") or [] if not e.get("isCancelled")]
        result = {"from": local(first, zone), "to": local(last, zone), "today": today(zone).isoformat(),
                  "events": events, "more": bool(page.get("@odata.nextLink")), **told(zone)}
        if name:
            result["calendar"] = name
        if page.get("@odata.nextLink"):
            result["next_page_token"] = str(page["@odata.nextLink"])
        return result, "success"

    @guarded
    async def get(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        event = await graph.get(f"me/events/{segment(call.inputs['event_id'])}", headers=TEXT_IN_UTC,
                                params={"$select": EVENT + ",body"})
        found = event_row(event, zone)
        found["description"] = clip(await body_text(event.get("body")), 20000)
        found["attendee_list"] = [{"email": bare(a), "name": str((a.get("emailAddress") or {}).get("name") or ""),
                                   "type": str(a.get("type") or "required"),
                                   "response": RESPONSES.get(str((a.get("status") or {}).get("response") or "none"),
                                                             "none")} for a in event.get("attendees") or []]
        recurrence = event.get("recurrence")
        if not recurrence and event.get("seriesMasterId"):
            master = await graph.get(f"me/events/{segment(event['seriesMasterId'])}", headers=IN_UTC,
                                     params={"$select": "id,recurrence"})
            recurrence = master.get("recurrence")
        if recurrence:
            found["repeats"] = described(recurrence)
        return {**found, **told(zone)}, "success"

    @guarded
    async def calendars(self, call):
        graph = await client(call)
        found = await graph.pages("me/calendars", params={
            "$select": "id,name,isDefaultCalendar,canEdit,owner,color"}, most=200)
        return {"calendars": [{"calendar_id": str(c.get("id") or ""), "name": str(c.get("name") or ""),
                               "owner": address(c.get("owner")), "can_edit": bool(c.get("canEdit")),
                               "default": bool(c.get("isDefaultCalendar"))} for c in found]}, "success"

    @guarded
    async def availability(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        first, last = day_span(call.inputs["start"], call.inputs.get("end"), zone)
        rows, unchecked, source = await busy_periods(graph, zone, first, last, call.inputs.get("people") or [])
        rows.sort(key=lambda r: (r["start"], r["email"]))
        return {"busy": [{"email": r["email"], "start": local(r["start"], zone), "end": local(r["end"], zone)}
                         for r in rows], "unchecked": unchecked, "source": source, **told(zone)}, "success"

    @guarded
    async def find_times(self, call):
        graph = await client(call)
        inputs = call.inputs
        zone = await zone_for(graph, inputs)
        from_date, to_date = day(inputs["from_date"], "from_date"), day(inputs["to_date"], "to_date")
        hours = inputs.get("working_hours") or {}
        try:
            working_start, working_end = clock(hours.get("start") or "09:00"), clock(hours.get("end") or "17:00")
            avoid = [(clock(a["start"]), clock(a["end"])) for a in inputs.get("avoid") or []]
        except (ValueError, KeyError) as wrong:
            raise Refused(f"Times of day are HH:MM: {wrong}") from None
        if inputs.get("working_days"):
            working_days = sorted({WEEKDAYS.index(str(d).lower()) for d in inputs["working_days"]})
        else:
            working_days = zone.working_days or [0, 1, 2, 3, 4]
        not_before = moment(inputs["not_before"], zone) if inputs.get("not_before") else datetime.now(timezone.utc)
        if to_date < from_date:
            raise Refused("to_date is before from_date.")
        if (to_date - from_date).days > 31:
            raise Refused(f"The window is {(to_date - from_date).days + 1} days, longer than 31: narrow it.")
        if working_end <= working_start:
            raise Refused("working_hours end before they start.")
        attendees = [str(a).strip() for a in inputs.get("attendees") or [] if str(a).strip()]
        first = datetime.combine(from_date, working_start, zone.info)
        last = datetime.combine(to_date, working_end, zone.info)
        rows, unchecked, source = await busy_periods(graph, zone, first, last, attendees)
        slots = find_slots(duration=timedelta(minutes=int(inputs["duration_minutes"])), from_date=from_date,
                           to_date=to_date, zone=zone.info, working_start=working_start, working_end=working_end,
                           working_days=working_days, avoid=avoid,
                           buffer=timedelta(minutes=int(inputs.get("buffer_minutes") or 0)),
                           busy=[(r["start"], r["end"]) for r in rows], not_before=not_before,
                           limit=int(inputs.get("max_candidates") or 5))
        me = await graph.me()
        candidates = []
        for slot in slots:
            ref = await call.records.create("proposal", {
                "start": slot["start"], "end": slot["end"], "timezone": zone.name, "status": "offered",
                "account": graph.account.lower(), "attendees": {"emails": [a for a in attendees
                                                                           if a.lower() not in me.mine]}})
            candidates.append({"proposal_ref": ref, **slot})
        return {"candidates": candidates, "unchecked": unchecked, "source": source,
                "working_days": [WEEKDAYS[d].capitalize() for d in working_days], **told(zone)}, "success"

    # -- writes --------------------------------------------------------------------------------------------------
    @guarded
    async def create(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        event = self._event(call.inputs, zone)
        return await self._created(call, graph, zone, event, str(uuid.uuid4()), "")

    @guarded
    async def invite(self, call):
        graph = await client(call)
        inputs = dict(call.inputs)
        zone = await zone_for(graph, inputs)
        proposal_ref = str(inputs.get("proposal_ref") or "")
        attendees = [str(a).strip() for a in inputs.get("attendees") or [] if str(a).strip()]
        if proposal_ref:
            proposal = (await call.records.read("proposal", proposal_ref))["fields"]
            if proposal.get("status") == "booked":
                raise Refused(f"That proposal was already booked (event {proposal.get('event_id')}).", "conflict")
            inputs.setdefault("start", proposal["start"])
            inputs.setdefault("end", proposal["end"])
            attendees = attendees or list((proposal.get("attendees") or {}).get("emails") or [])
        if not attendees:
            raise Refused("An invitation needs attendees: give their addresses (or use calendar_create for an event "
                          "of your own).")
        if inputs.get("teams"):
            await work_only(graph, "A Teams meeting link")
        event = self._event(inputs, zone)
        event["attendees"] = ([{"emailAddress": {"address": a}, "type": "required"} for a in attendees] +
                              [{"emailAddress": {"address": str(a).strip()}, "type": "optional"}
                               for a in inputs.get("optional_attendees") or [] if str(a).strip()])
        if inputs.get("teams"):
            event.update(isOnlineMeeting=True, onlineMeetingProvider="teamsForBusiness")
        # The same proposal booked twice is one event: Outlook drops a repeated transactionId.
        transaction = str(uuid.uuid5(uuid.NAMESPACE_URL, f"outlook:{graph.account}:{proposal_ref}")) if proposal_ref \
            else str(uuid.uuid4())
        result, status = await self._created(call, graph, zone, event, transaction, "invite")
        if status == "success":
            result["invited"] = attendees
            if inputs.get("teams") and not result.get("online_meeting_url"):
                result["note"] = ("Outlook created the meeting without a Teams link: the organization may not allow "
                                  "Teams meetings from Outlook.")
            if proposal_ref:
                await call.records.update("proposal", proposal_ref, {"status": "booked",
                                                                     "event_id": result["event_id"]})
        return result, status

    @guarded
    async def update(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        inputs = call.inputs
        event_id = str(inputs["event_id"])
        event = await graph.get(f"me/events/{segment(event_id)}", headers=IN_UTC, params={"$select": EVENT})
        if not event.get("isOrganizer") and event.get("attendees"):
            raise Refused(f"Only the organizer ({address(event.get('organizer'))}) can change this meeting; answer it "
                          f"with calendar_respond instead.", "denied")
        patch: dict[str, Any] = {}
        if inputs.get("subject"):
            patch["subject"] = str(inputs["subject"])
        if inputs.get("location") is not None and "location" in inputs:
            patch["location"] = {"displayName": str(inputs["location"])}
        if inputs.get("description"):
            patch["body"] = {"contentType": "text", "content": str(inputs["description"])}
        if inputs.get("start") or inputs.get("end"):
            patch.update(self._moved(event, inputs, zone))
        if not patch:
            raise Refused("Nothing to change: give a subject, start, end, location or description.")
        updated = await graph.patch(f"me/events/{segment(event_id)}", json=patch, headers=IN_UTC)
        if not updated.get("id"):
            updated = await graph.get(f"me/events/{segment(event_id)}", headers=IN_UTC, params={"$select": EVENT})
        return {**event_row(updated, zone), **told(zone)}, "success"

    @guarded
    async def cancel(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        event_id = str(call.inputs["event_id"])
        comment = str(call.inputs.get("comment") or "")
        event = await graph.get(f"me/events/{segment(event_id)}", headers=IN_UTC, params={"$select": EVENT})
        found = event_row(event, zone)
        if not event.get("isOrganizer"):
            # Only an organizer cancels a meeting; an attendee declines it, and the organizer is told.
            await graph.post(f"me/events/{segment(event_id)}/decline", json={"comment": comment, "sendResponse": True})
            return {"event_id": event_id, "subject": found["subject"], "cancelled": False, "declined": True,
                    "note": f"You are not the organizer ({found['organizer']}): the invitation was declined and the "
                            f"organizer told; the meeting goes on for the others."}, "success"
        if event.get("attendees"):
            await graph.post(f"me/events/{segment(event_id)}/cancel", json={"comment": comment})
            how = "cancelled, and the attendees were sent the cancellation"
        else:
            await graph.delete(f"me/events/{segment(event_id)}")
            how = "deleted (it had no attendees)"
        return {"event_id": event_id, "subject": found["subject"], "cancelled": True, "declined": False,
                "note": f"The event was {how}."}, "success"

    @guarded
    async def respond(self, call):
        graph = await client(call)
        event_id = str(call.inputs["event_id"])
        answer = str(call.inputs["response"])
        event = await graph.get(f"me/events/{segment(event_id)}", headers=IN_UTC, params={"$select": EVENT})
        if event.get("isOrganizer"):
            raise Refused("You organize this event: there is no invitation to answer. Use calendar_cancel to call it "
                          "off.")
        send = call.inputs.get("send_response")
        await graph.post(f"me/events/{segment(event_id)}/{ANSWERS[answer]}", json={
            "comment": str(call.inputs.get("comment") or ""), "sendResponse": True if send is None else bool(send)})
        return {"event_id": event_id, "subject": str(event.get("subject") or ""), "response": answer,
                "organizer": address(event.get("organizer")),
                "organizer_told": True if send is None else bool(send)}, "success"

    # -- helpers -------------------------------------------------------------------------------------------------
    @staticmethod
    def _event(inputs: dict[str, Any], zone) -> dict[str, Any]:
        event: dict[str, Any] = {"subject": str(inputs["subject"])}
        if not inputs.get("start"):
            raise Refused("Give start (and end, or duration_minutes).")
        if inputs.get("all_day") or (is_date(inputs["start"]) and (not inputs.get("end") or is_date(inputs["end"]))):
            first = day(inputs["start"], "start")
            final = day(inputs["end"], "end") if inputs.get("end") else first
            if final < first:
                raise Refused("The event's last day is before its first.")
            event.update(isAllDay=True,
                         start={"dateTime": f"{first.isoformat()}T00:00:00", "timeZone": zone.label},
                         end={"dateTime": f"{(final + timedelta(days=1)).isoformat()}T00:00:00",
                              "timeZone": zone.label})
        else:
            start = moment(inputs["start"], zone, "start")
            if inputs.get("end"):
                end = moment(inputs["end"], zone, "end")
            elif inputs.get("duration_minutes"):
                end = start + timedelta(minutes=int(inputs["duration_minutes"]))
            else:
                raise Refused("Give end, or duration_minutes.")
            if end <= start:
                raise Refused("The event ends before it starts.")
            event.update(start={"dateTime": utc_text(start), "timeZone": "UTC"},
                         end={"dateTime": utc_text(end), "timeZone": "UTC"})
        if inputs.get("location"):
            event["location"] = {"displayName": str(inputs["location"])}
        if inputs.get("description"):
            event["body"] = {"contentType": "text", "content": str(inputs["description"])}
        return event

    @staticmethod
    def _moved(event: dict[str, Any], inputs: dict[str, Any], zone) -> dict[str, Any]:
        """New start and end: a new start alone moves the end with it, keeping the event's length."""
        if event.get("isAllDay"):
            old_first = date.fromisoformat(str(event["start"]["dateTime"])[:10])
            old_after = date.fromisoformat(str(event["end"]["dateTime"])[:10])
            first = day(inputs["start"], "start") if inputs.get("start") else old_first
            after = (day(inputs["end"], "end") + timedelta(days=1)) if inputs.get("end") else \
                first + (old_after - old_first)
            if after <= first:
                raise Refused("The event's last day is before its first.")
            return {"start": {"dateTime": f"{first}T00:00:00", "timeZone": zone.label},
                    "end": {"dateTime": f"{after}T00:00:00", "timeZone": zone.label}}
        old_start, old_end = graph_moment(event.get("start")), graph_moment(event.get("end"))
        start = moment(inputs["start"], zone, "start") if inputs.get("start") else old_start
        end = moment(inputs["end"], zone, "end") if inputs.get("end") else start + (old_end - old_start)
        if end <= start:
            raise Refused("The event would end before it starts.")
        return {"start": {"dateTime": utc_text(start), "timeZone": "UTC"},
                "end": {"dateTime": utc_text(end), "timeZone": "UTC"}}

    async def _created(self, call, graph, zone, event, transaction, kind):
        base, _ = await calendar_base(graph, call.inputs.get("calendar"))
        event["transactionId"] = transaction
        await call.progress(f"Creating '{event['subject']}'")
        created = await graph.post(f"{base}/events", json=event, headers=IN_UTC)
        if not created.get("id"):
            raise Refused("Outlook accepted the event but returned no id; the outcome is unknown. Look at the calendar "
                          "before trying again.", "unknown")
        return {**event_row(created, zone), **told(zone)}, "success"
