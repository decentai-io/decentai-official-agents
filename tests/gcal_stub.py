"""A loopback Google: the Calendar v3 and Tasks v1 subset the Google Calendar agent calls, over real HTTP, under
``www.googleapis.com`` (Calendar at ``/calendar/v3``, Tasks at ``/tasks/v1``), holding fictional data for Sidra
Office Supplies. Several accounts, each with its own access token.

As strict as Google where its rules are known, because a stub that accepts what Google refuses hides a broken agent:

- **Throttling:** ``throttle`` queues answers for the next requests: 429 with ``Retry-After``, or 403 with the reason
  ``rateLimitExceeded`` (Google's way).
- **Events:** ``timeMin``/``timeMax`` need an offset; ``orderBy=startTime`` needs ``singleEvents=true``; an event that
  ends at or before its start is 400 ("The specified time range is empty."); a timed start without an offset or a
  ``timeZone`` is 400; a client id must be base32hex (``[a-v0-9]{5,1024}``) and unique (409, also for a cancelled
  one); ``sendUpdates`` must be all, externalOnly or none; ``conferenceData`` is ignored unless
  ``conferenceDataVersion=1``; a non-organiser may change only their own response.
- **Recurring events** (``RRULE:FREQ=DAILY|WEEKLY;COUNT=n``) are expanded only with ``singleEvents=true``.
- **Free/busy** answers per calendar as asked (``primary`` too); one this account can't read has ``errors``.
- **Tasks:** at most 100 a page (20 by default); due keeps only the date; ``updatedMin``/``completedMin`` compare to
  the millisecond; completed tasks are left out unless ``showCompleted``, hidden ones unless ``showHidden``, deleted
  ones unless ``showDeleted``; a completed task can't be nested.
- **Page tokens** are opaque, and only valid for the query that gave them (400 otherwise).
- **Clock:** the test's. Every change stamps ``clock`` plus a millisecond more, and every answer's ``Date`` header is
  the clock, so the agent reads Google's time from it.
"""

from __future__ import annotations

import itertools
import json
import re
import socket
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from zoneinfo import ZoneInfo

from tests.conftest import stands_for

BASE32HEX = re.compile(r"^[a-v0-9]{5,1024}$")


def when(text: str) -> datetime:
    """A time Google takes: an offset or Z is required."""
    raw = str(text)
    found = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    if found.tzinfo is None:
        raise ValueError(f"{raw} has no offset")
    return found


class Refuse(Exception):
    def __init__(self, status: int, message: str, reason: str = ""):
        super().__init__(message)
        self.status, self.message, self.reason = status, message, reason


class Account:
    def __init__(self, stub: "GoogleStub", email: str, token: str, zone: str):
        self.stub, self.email, self.token, self.zone = stub, email, token, zone
        self.calendars: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.overrides: dict[str, dict[str, dict[str, Any]]] = {}
        self.lists: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, list[dict[str, Any]]] = {}
        self.add_calendar(email, "Sidra", zone, primary=True)

    # -- calendar data ----------------------------------------------------------------------------------------
    def add_calendar(self, calendar_id: str, summary: str, zone: str, primary: bool = False,
                     role: str = "owner") -> str:
        self.calendars[calendar_id] = {"id": calendar_id, "summary": summary, "timeZone": zone,
                                       "primary": primary, "accessRole": role}
        self.events[calendar_id] = {}
        self.overrides[calendar_id] = {}
        return calendar_id

    def cal(self, calendar_id: str) -> str:
        return self.email if calendar_id == "primary" else calendar_id

    def add_event(self, summary: str, start: str, end: str, attendees=(), calendar_id: str = "", *,
                  organizer: str = "", all_day: bool = False, recurrence=None, transparent: bool = False,
                  my_response: str = "", guests_can_modify: bool = False) -> str:
        calendar_id = self.cal(calendar_id or self.email)
        event_id = f"ev{next(self.stub.ids):04d}"
        organizer = organizer or calendar_id
        people = [{"email": a, "responseStatus": "needsAction"} for a in attendees]
        if organizer != calendar_id:
            people.append({"email": calendar_id, "self": True, "responseStatus": my_response or "needsAction"})
            people.append({"email": organizer, "organizer": True, "responseStatus": "accepted"})
        event = {"id": event_id, "status": "confirmed", "summary": summary,
                 "start": {"date": start} if all_day else {"dateTime": start, "timeZone": self.zone},
                 "end": {"date": end} if all_day else {"dateTime": end, "timeZone": self.zone},
                 "organizer": {"email": organizer, **({"self": True} if organizer == calendar_id else {})},
                 "attendees": people, "htmlLink": f"https://www.google.com/calendar/event?eid={event_id}",
                 "updated": self.stub.stamp()}
        if not people:
            event.pop("attendees")
        if recurrence:
            event["recurrence"] = list(recurrence)
        if transparent:
            event["transparency"] = "transparent"
        if guests_can_modify:
            event["guestsCanModify"] = True
        self.events[calendar_id][event_id] = event
        return event_id

    def bounds(self, event: dict[str, Any], calendar_id: str) -> tuple[datetime, datetime]:
        zone = ZoneInfo(self.calendars[calendar_id]["timeZone"])
        start, end = event["start"], event["end"]
        if start.get("date"):
            return (datetime.combine(date.fromisoformat(start["date"]), datetime.min.time(), zone),
                    datetime.combine(date.fromisoformat(end["date"]), datetime.min.time(), zone))
        return when(start["dateTime"]), when(end["dateTime"])

    def expanded(self, calendar_id: str) -> list[dict[str, Any]]:
        """Every occurrence: plain events, and each instance of a recurring one (its exceptions applied)."""
        found = []
        for event in self.events[calendar_id].values():
            if not event.get("recurrence"):
                found.append(event)
                continue
            found.extend(self.instances(calendar_id, event))
        return found

    def instances(self, calendar_id: str, master: dict[str, Any]) -> list[dict[str, Any]]:
        rule = dict(p.split("=") for p in master["recurrence"][0].split(":", 1)[1].split(";"))
        step = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(days=7)}[rule["FREQ"]]
        start, end = self.bounds(master, calendar_id)
        found = []
        for n in range(int(rule.get("COUNT", "10"))):
            s, e = start + n * step, end + n * step
            instance_id = f"{master['id']}_{s.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
            made = {**{k: v for k, v in master.items() if k != "recurrence"}, "id": instance_id,
                    "recurringEventId": master["id"],
                    "originalStartTime": {"dateTime": s.isoformat()},
                    "start": {"dateTime": s.isoformat(), "timeZone": master["start"].get("timeZone")},
                    "end": {"dateTime": e.isoformat(), "timeZone": master["end"].get("timeZone")}}
            if master["start"].get("date"):
                made["start"], made["end"] = {"date": s.date().isoformat()}, {"date": e.date().isoformat()}
            found.append(self.overrides[calendar_id].get(instance_id, made))
        return found

    def find(self, calendar_id: str, event_id: str) -> Optional[dict[str, Any]]:
        if event_id in self.events[calendar_id]:
            return self.events[calendar_id][event_id]
        return next((e for e in self.expanded(calendar_id) if e["id"] == event_id), None)

    def busy(self, calendar_id: str, low: datetime, high: datetime) -> list[dict[str, str]]:
        periods = []
        for event in self.expanded(calendar_id):
            if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                continue
            mine = next((a for a in event.get("attendees") or [] if a.get("self")), None)
            if mine and mine.get("responseStatus") == "declined":
                continue
            s, e = self.bounds(event, calendar_id)
            if s < high and e > low:
                periods.append({"start": s.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "end": e.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        return sorted(periods, key=lambda p: p["start"])

    # -- tasks data -------------------------------------------------------------------------------------------
    def add_list(self, title: str) -> str:
        list_id = f"TDE{next(self.stub.ids):04d}"
        self.lists[list_id] = {"kind": "tasks#taskList", "id": list_id, "title": title, "updated": self.stub.stamp()}
        self.tasks[list_id] = []
        return list_id

    def add_task(self, list_id: str, title: str, *, due: str = "", notes: str = "", parent: str = "",
                 status: str = "needsAction") -> str:
        task_id = f"dGFzay{next(self.stub.ids):05d}"
        task: dict[str, Any] = {"kind": "tasks#task", "id": task_id, "title": title, "updated": self.stub.stamp(),
                                "status": status, "position": f"{len(self.tasks[list_id]):020d}",
                                "webViewLink": f"https://tasks.google.com/task/{task_id}"}
        if notes:
            task["notes"] = notes
        if due:
            task["due"] = f"{due[:10]}T00:00:00.000Z"
        if parent:
            task["parent"] = parent
        if status == "completed":
            task["completed"] = task["updated"]
            task["hidden"] = True
        self.tasks[list_id].append(task)
        return task_id

    def task(self, task_id: str) -> Optional[dict[str, Any]]:
        return next((t for rows in self.tasks.values() for t in rows if t["id"] == task_id), None)

    def apply(self, task: dict[str, Any], changes: dict[str, Any]) -> None:
        moment = self.stub.stamp()
        for key, value in changes.items():
            if key in ("id", "kind", "updated", "position", "webViewLink"):
                continue
            if value is None:
                task.pop(key, None)
            elif key == "due":
                task["due"] = f"{str(value)[:10]}T00:00:00.000Z"     # Google keeps the date only
            else:
                task[key] = value
        if changes.get("status") == "completed" and "completed" not in task:
            task["completed"] = moment
        elif changes.get("status") == "needsAction":
            task.pop("completed", None)
            task.pop("hidden", None)
        task["updated"] = moment

    def complete_in_app(self, task_id: str) -> None:
        """The person ticks a task off in Google's own app, which hides it."""
        self.apply(self.task(task_id), {"status": "completed"})
        self.task(task_id)["hidden"] = True

    def edit_in_app(self, task_id: str, **changes: Any) -> None:
        self.apply(self.task(task_id), changes)

    def delete_list(self, list_id: str) -> None:
        del self.lists[list_id]
        del self.tasks[list_id]


class GoogleStub:
    def __init__(self):
        self.accounts: dict[str, Account] = {}
        self.shared: dict[str, list[dict[str, str]]] = {}     # another person's calendar -> busy periods
        self.ids = itertools.count(1)
        self.clock = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
        self._ms = 0
        self.throttle: list[tuple[int, str]] = []
        self.requests: list[tuple[str, str, dict[str, Any], Any]] = []
        self.drop_next_insert = False
        self.meet_pending = False
        self.pages: dict[str, tuple[str, int]] = {}
        self._server: Optional[ThreadingHTTPServer] = None

    # -- the test's handles -----------------------------------------------------------------------------------
    def account(self, email: str, token: str = "at-1", zone: str = "Asia/Dubai") -> Account:
        self.accounts[token] = Account(self, email, token, zone)
        return self.accounts[token]

    def set_clock(self, text: str) -> None:
        self.clock, self._ms = when(text), 0

    def stamp(self) -> str:
        self._ms += 1
        moment = self.clock + timedelta(milliseconds=self._ms)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"

    def share(self, email: str, busy: list[dict[str, str]]) -> None:
        self.shared[email] = busy

    def sent(self, method: str, part: str = "") -> list[tuple[str, dict[str, Any], Any]]:
        return [(path, query, body) for m, path, query, body in self.requests if m == method and part in path]

    def page(self, rows: list[Any], query: dict[str, str], signature: str, default: int, cap: int,
             key: str = "items") -> dict[str, Any]:
        top = min(int(query.get("maxResults") or default), cap)
        offset = 0
        if query.get("pageToken"):
            held = self.pages.get(query["pageToken"])
            if held is None or held[0] != signature:
                raise Refuse(400, "Invalid page token.", "invalid")
            offset = held[1]
        answer: dict[str, Any] = {key: rows[offset: offset + top]}
        if offset + top < len(rows):
            token = f"CgP{next(self.ids):06d}"
            self.pages[token] = (signature, offset + top)
            answer["nextPageToken"] = token
        return answer

    # -- the server -------------------------------------------------------------------------------------------
    def start(self) -> "GoogleStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def date_time_string(self, timestamp=None):
                """Every answer's Date is the stub's clock: Google's time, as the agent reads it."""
                return format_datetime(stub.clock.replace(microsecond=0), usegmt=True)

            def _json(self, status: int, body: Any, headers: Optional[dict[str, str]] = None) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, status: int, message: str, reason: str = "") -> None:
                errors = [{"domain": "global", "reason": reason, "message": message}] if reason else []
                self._json(status, {"error": {"code": status, "message": message, "errors": errors}})

            def _handle(self, method: str) -> None:
                url = urllib.parse.urlparse(self.path)
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else None
                stub.requests.append((method, url.path, query, body))
                if stub.throttle:
                    status, how = stub.throttle.pop(0)
                    if status == 429:
                        return self._json(429, {"error": {"code": 429, "message": "Too many requests",
                                                          "status": "RESOURCE_EXHAUSTED"}},
                                          {"Retry-After": how or "0"})
                    return self._error(403, "Rate Limit Exceeded", how or "rateLimitExceeded")
                account = stub.accounts.get((self.headers.get("Authorization") or "")[len("Bearer "):])
                if account is None:
                    return self._error(401, "Request had invalid authentication credentials.", "authError")
                parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
                try:
                    if parts[:2] == ["calendar", "v3"]:
                        found = Calendar(stub, account).route(method, parts[2:], query, body, self)
                    elif parts[:2] == ["tasks", "v1"]:
                        found = Tasks(stub, account).route(method, parts[2:], query, body)
                    else:
                        raise Refuse(404, f"no route {url.path}")
                except Refuse as refused:
                    return self._error(refused.status, refused.message, refused.reason)
                if found is Calendar.DROPPED:
                    return
                if found is None:
                    return self._json(204, None)
                self._json(200, found)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_PATCH(self):  # noqa: N802
                self._handle("PATCH")

            def do_DELETE(self):  # noqa: N802
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("www.googleapis.com", self._server)

    def secret(self, account: Account, access_token: str = "") -> dict[str, Any]:
        """The credential as the platform hands it to the agent: the address, a fresh access token, and the stub's
        root, under which both APIs are served."""
        return {"account": account.email, "access_token": access_token or account.token, "api_base_url": self.url}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


class Calendar:
    DROPPED = object()

    def __init__(self, stub: GoogleStub, account: Account):
        self.stub, self.account = stub, account

    def calendar(self, calendar_id: str) -> str:
        found = self.account.cal(calendar_id)
        if found not in self.account.calendars:
            raise Refuse(404, "Not Found", "notFound")
        return found

    def route(self, method: str, parts: list[str], query: dict[str, str], body: Any, handler: Any) -> Any:
        if parts == ["users", "me", "calendarList"] and method == "GET":
            rows = list(self.account.calendars.values())
            return self.stub.page(rows, query, "calendarList", 100, 250)
        if parts == ["freeBusy"] and method == "POST":
            return self.free_busy(body or {})
        if parts[:1] == ["calendars"] and len(parts) == 2 and method == "GET":
            found = self.account.calendars[self.calendar(parts[1])]
            return {"id": found["id"], "summary": found["summary"], "timeZone": found["timeZone"]}
        if parts[:1] == ["calendars"] and len(parts) >= 3 and parts[2] == "events":
            calendar_id = self.calendar(parts[1])
            if len(parts) == 3 and method == "GET":
                return self.listing(calendar_id, query)
            if len(parts) == 3 and method == "POST":
                return self.insert(calendar_id, query, body or {}, handler)
            if len(parts) == 4:
                event = self.account.find(calendar_id, parts[3])
                if event is None:
                    raise Refuse(404, "Not Found", "notFound")
                if method == "GET":
                    return event
                self.updates(query)
                if method == "PATCH":
                    return self.patch(calendar_id, event, body or {}, query)
                if method == "DELETE":
                    if event.get("status") == "cancelled":
                        raise Refuse(410, "Resource has been deleted", "deleted")
                    self.store(calendar_id, {**event, "status": "cancelled", "updated": self.stub.stamp()})
                    return None
        raise Refuse(404, f"no route {'/'.join(parts)}")

    def store(self, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        if event.get("recurringEventId"):
            self.account.overrides[calendar_id][event["id"]] = event
        else:
            self.account.events[calendar_id][event["id"]] = event
        return event

    @staticmethod
    def updates(query: dict[str, str]) -> None:
        if query.get("sendUpdates", "none") not in ("all", "externalOnly", "none"):
            raise Refuse(400, "Invalid value for sendUpdates.", "invalid")

    def listing(self, calendar_id: str, query: dict[str, str]) -> dict[str, Any]:
        try:
            low = when(query["timeMin"]) if "timeMin" in query else datetime.min.replace(tzinfo=timezone.utc)
            high = when(query["timeMax"]) if "timeMax" in query else datetime.max.replace(tzinfo=timezone.utc)
        except ValueError:
            raise Refuse(400, "Bad Request", "badRequest") from None
        if query.get("orderBy") == "startTime" and query.get("singleEvents") != "true":
            raise Refuse(400, "The requested ordering is not available for the particular query.", "badRequest")
        if int(query.get("maxResults") or 250) > 2500:
            raise Refuse(400, "Invalid value for maxResults.", "invalid")
        if query.get("singleEvents") == "true":
            events = self.account.expanded(calendar_id)
        else:
            events = list(self.account.events[calendar_id].values())
        q = query.get("q", "").lower()
        rows = [e for e in events if e.get("status") != "cancelled" and (not q or q in e.get("summary", "").lower())
                and self.account.bounds(e, calendar_id)[0] < high and self.account.bounds(e, calendar_id)[1] > low]
        rows.sort(key=lambda e: self.account.bounds(e, calendar_id)[0])
        signature = json.dumps([calendar_id, {k: v for k, v in query.items() if k not in ("pageToken",)}])
        answer = self.stub.page(rows, query, signature, 250, 2500)
        answer["timeZone"] = query.get("timeZone") or self.account.calendars[calendar_id]["timeZone"]
        return answer

    def check_times(self, event: dict[str, Any], calendar_id: str) -> None:
        for side in ("start", "end"):
            given = event.get(side) or {}
            if not given.get("date") and not given.get("dateTime"):
                raise Refuse(400, f"Missing {side} time.", "required")
            if given.get("dateTime"):
                try:
                    when(given["dateTime"])
                except ValueError:
                    if not given.get("timeZone"):
                        raise Refuse(400, f"Missing time zone definition for {side} time.", "required") from None
        start, end = event["start"], event["end"]
        if bool(start.get("date")) != bool(end.get("date")):
            raise Refuse(400, "Start and end times must either both be date or both be dateTime.", "invalid")
        s, e = self.account.bounds(event, calendar_id)
        if e <= s:
            raise Refuse(400, "The specified time range is empty.", "timeRangeEmpty")

    def meet(self, event: dict[str, Any], body: dict[str, Any], query: dict[str, str]) -> None:
        if query.get("conferenceDataVersion") != "1" or not body.get("conferenceData"):
            event.pop("conferenceData", None)     # version 0: conference data in the body is ignored
            return
        request = body["conferenceData"].get("createRequest") or {}
        if not request.get("requestId") or (request.get("conferenceSolutionKey") or {}).get("type") != "hangoutsMeet":
            raise Refuse(400, "Invalid conference request.", "invalid")
        code = "pending" if self.stub.meet_pending else "success"
        event["conferenceData"] = {"createRequest": {**request, "status": {"statusCode": code}}}
        if code == "success":
            link = f"https://meet.google.com/abc-{event['id'][:4]}-xyz"
            event["hangoutLink"] = link
            event["conferenceData"]["entryPoints"] = [{"entryPointType": "video", "uri": link}]

    def insert(self, calendar_id: str, query: dict[str, str], body: dict[str, Any], handler: Any) -> Any:
        self.updates(query)
        event_id = str(body.get("id") or f"gen{next(self.stub.ids):06d}")
        if not BASE32HEX.match(event_id):
            raise Refuse(400, "Invalid resource id value.", "invalid")
        if event_id in self.account.events[calendar_id]:
            raise Refuse(409, "The requested identifier already exists.", "duplicate")
        self.check_times(body, calendar_id)
        event = {**body, "id": event_id, "status": "confirmed", "organizer": {"email": calendar_id, "self": True},
                 "creator": {"email": self.account.email, "self": True}, "updated": self.stub.stamp(),
                 "htmlLink": f"https://www.google.com/calendar/event?eid={event_id}",
                 "attendees": [{"responseStatus": "needsAction", **a} for a in body.get("attendees") or []]}
        if not event["attendees"]:
            event.pop("attendees")
        self.meet(event, body, query)
        self.account.events[calendar_id][event_id] = event
        if self.stub.drop_next_insert:
            # Written, but the answer never arrives.
            self.stub.drop_next_insert = False
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.close_connection = True
            return self.DROPPED
        return event

    def patch(self, calendar_id: str, event: dict[str, Any], body: dict[str, Any], query: dict[str, str]) -> Any:
        organiser = (event.get("organizer") or {}).get("self") is True
        if not organiser:
            touched = set(body) - {"attendees"}
            if touched and not event.get("guestsCanModify"):
                raise Refuse(403, "You need to have writer access to this calendar.", "forbiddenForNonOrganizer")
            if "attendees" in body:
                before = {a["email"].lower(): a for a in event.get("attendees") or []}
                after = {a["email"].lower(): a for a in body["attendees"]}
                if set(before) != set(after) or any(
                        {k: v for k, v in after[e].items() if k not in ("responseStatus", "comment")}
                        != {k: v for k, v in before[e].items() if k not in ("responseStatus", "comment")}
                        or (not before[e].get("self") and after[e].get("responseStatus")
                            != before[e].get("responseStatus")) for e in before):
                    raise Refuse(403, "Only the organizer can change other attendees.", "forbiddenForNonOrganizer")
        merged = {**event, **{k: v for k, v in body.items() if k != "id"}, "updated": self.stub.stamp()}
        if "attendees" in body:
            merged["attendees"] = [{"responseStatus": "needsAction", **a} for a in body["attendees"]]
        self.check_times(merged, calendar_id)
        if "conferenceData" in body:
            self.meet(merged, body, query)
        return self.store(calendar_id, merged)

    def free_busy(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            low, high = when(body["timeMin"]), when(body["timeMax"])
        except (KeyError, ValueError):
            raise Refuse(400, "Bad Request", "badRequest") from None
        if high <= low:
            raise Refuse(400, "The specified time range is empty.", "timeRangeEmpty")
        items = body.get("items") or []
        if len(items) > 50:
            raise Refuse(400, "Too many calendars requested.", "tooManyCalendarsRequested")
        calendars = {}
        for item in items:
            asked = item["id"]
            own = self.account.cal(asked)
            if own in self.account.calendars:
                calendars[asked] = {"busy": self.account.busy(own, low, high)}
            elif asked in self.stub.shared:
                calendars[asked] = {"busy": [p for p in self.stub.shared[asked]
                                             if when(p["end"]) > low and when(p["start"]) < high]}
            else:
                calendars[asked] = {"busy": [], "errors": [{"domain": "global", "reason": "notFound"}]}
        return {"kind": "calendar#freeBusy", "timeMin": body["timeMin"], "timeMax": body["timeMax"],
                "calendars": calendars}


class Tasks:
    def __init__(self, stub: GoogleStub, account: Account):
        self.stub, self.account = stub, account

    def list_id(self, name: str) -> str:
        found = next(iter(self.account.lists), "") if name == "@default" else name
        if found not in self.account.lists:
            raise Refuse(404, "Requested entity was not found.", "notFound")
        return found

    def find(self, list_id: str, task_id: str) -> dict[str, Any]:
        task = next((t for t in self.account.tasks[list_id] if t["id"] == task_id and not t.get("deleted")), None)
        if task is None:
            raise Refuse(404, "Requested entity was not found.", "notFound")
        return task

    def route(self, method: str, parts: list[str], query: dict[str, str], body: Any) -> Any:
        if parts == ["users", "@me", "lists"]:
            if method == "GET":
                return self.stub.page(list(self.account.lists.values()), query, "lists", 1000, 1000)
            if method == "POST":
                return self.account.lists[self.account.add_list(str((body or {}).get("title") or ""))]
        if parts[:3] == ["users", "@me", "lists"] and len(parts) == 4 and method == "GET":
            return self.account.lists[self.list_id(parts[3])]
        if parts[:1] == ["lists"] and len(parts) >= 3 and parts[2] == "tasks":
            list_id = self.list_id(parts[1])
            if len(parts) == 3 and method == "GET":
                return self.select(list_id, query)
            if len(parts) == 3 and method == "POST":
                return self.insert(list_id, query, body or {})
            task = self.find(list_id, parts[3])
            if len(parts) == 5 and parts[4] == "move" and method == "POST":
                return self.move(list_id, task, query)
            if len(parts) == 4 and method == "GET":
                return task
            if len(parts) == 4 and method == "PATCH":
                self.account.apply(task, body or {})
                return task
            if len(parts) == 4 and method == "DELETE":
                for row in self.account.tasks[list_id]:
                    if row["id"] == task["id"] or row.get("parent") == task["id"]:
                        self.account.apply(row, {"deleted": True})
                return None
        raise Refuse(404, f"no route {'/'.join(parts)}")

    def select(self, list_id: str, query: dict[str, str]) -> dict[str, Any]:
        def flag(name: str, default: bool) -> bool:
            return query.get(name, str(default).lower()) == "true"

        rows = list(self.account.tasks[list_id])
        if not flag("showDeleted", False):
            rows = [t for t in rows if not t.get("deleted")]
        if not flag("showCompleted", True):
            rows = [t for t in rows if t["status"] != "completed"]
        if not flag("showHidden", False):
            rows = [t for t in rows if not t.get("hidden")]
        try:
            if "updatedMin" in query:
                rows = [t for t in rows if when(t["updated"]) >= when(query["updatedMin"])]
            if "completedMin" in query:
                rows = [t for t in rows if t.get("completed") and when(t["completed"]) >= when(query["completedMin"])]
            if "dueMax" in query:
                rows = [t for t in rows if t.get("due") and when(t["due"]) < when(query["dueMax"])]
        except ValueError:
            raise Refuse(400, "Invalid value for a time bound.", "invalid") from None
        signature = json.dumps([list_id, {k: v for k, v in query.items() if k not in ("pageToken",)}])
        return self.stub.page(rows, query, signature, 20, 100)

    def insert(self, list_id: str, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        parent = query.get("parent", "")
        if parent:
            self.find(list_id, parent)
        task_id = self.account.add_task(list_id, str(body.get("title") or ""), notes=str(body.get("notes") or ""),
                                        parent=parent, due=str(body.get("due") or ""))
        return self.account.task(task_id)

    def move(self, list_id: str, task: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
        target = self.list_id(query["destinationTasklist"]) if query.get("destinationTasklist") else list_id
        parent = query.get("parent", "")
        if task["status"] == "completed" and (parent or query.get("previous")):
            raise Refuse(400, "Completed tasks can't be nested or placed.", "invalid")
        if parent:
            self.find(target, parent)
        if target != list_id:
            self.account.tasks[list_id] = [t for t in self.account.tasks[list_id] if t["id"] != task["id"]]
            self.account.tasks[target].append(task)
        self.account.apply(task, {"parent": parent or None})
        return task
