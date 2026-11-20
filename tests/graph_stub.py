"""A loopback Microsoft Graph for the Outlook agent: mail, calendar and To Do of fictional accounts of Sidra Office
Supplies, over real HTTP, as strict as the real service wherever its rule is known.

What it refuses, as Graph does:
- a ``$filter`` with an ``$orderby`` whose properties do not lead the filter, in order (InefficientFilter: "The
  restriction or sort order is too complex for this operation");
- ``$search`` with ``$filter``, ``$orderby`` or ``$skip`` on messages;
- ``mailboxSettings`` for a token without MailboxSettings.Read;
- ``getSchedule`` and Teams meetings for a personal account (a token that is not a JWT naming a work tenant);
- an attachment POSTed whole over 4 MB, an upload-session PUT that carries a token, out of order or over 4 MB;
- cancelling a meeting one does not organize, answering one's own;
- ``calendarView`` without its start and end; a date-time zone it does not know.

On demand it answers 429 with ``Retry-After``, drops a send without answering, and expires a To Do change feed (410).
Ids carry "=", so a client that does not escape them breaks here as there.
"""

from __future__ import annotations

import base64
import html
import itertools
import json
import re
import socket
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from zoneinfo import ZoneInfo

from tests.conftest import stands_for

WORK_TENANT = "5f1c0a2e-0000-4000-8000-5157a0ff1ce5"
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
ALL_SCOPES = {"Mail.ReadWrite", "Mail.Send", "MailboxSettings.Read", "Calendars.ReadWrite", "Tasks.ReadWrite"}
TOO_COMPLEX = "The restriction or sort order is too complex for this operation."
FOUR_MB = 4 * 1024 * 1024
WINDOWS = {"Arabian Standard Time": "Asia/Dubai", "Arab Standard Time": "Asia/Riyadh", "UTC": "UTC",
           "GMT Standard Time": "Europe/London", "Pacific Standard Time": "America/Los_Angeles",
           "Eastern Standard Time": "America/New_York", "India Standard Time": "Asia/Kolkata"}


def zone_of(name: str) -> ZoneInfo:
    name = WINDOWS.get(name, name)
    try:
        return ZoneInfo(name)
    except Exception:
        raise ValueError(f"A valid TimeZone value must be specified. The following TimeZone value is not supported: "
                         f"'{name}'.") from None


def mailbox(text: str) -> dict:
    name, addr = parseaddr(text or "")
    return {"emailAddress": {"name": name or addr, "address": addr}}


def mailboxes(text: str) -> list:
    return [mailbox(p.strip()) for p in (text or "").split(",") if p.strip()]


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def graph_time(moment: datetime) -> dict:
    return {"dateTime": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".0000000", "timeZone": "UTC"}


def parse_time(text: str) -> datetime:
    text = str(text).strip()
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        text = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def from_graph(value: dict) -> datetime:
    """A {dateTime, timeZone} the agent wrote, as UTC."""
    zone = zone_of(str(value.get("timeZone") or "UTC"))
    moment = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", str(value["dateTime"])))
    return moment.replace(tzinfo=zone).astimezone(timezone.utc)


def text_of(content: str) -> str:
    content = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", content)
    content = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", content)
    return html.unescape(re.sub(r"<[^>]+>", "", content)).strip()


def jwt(tenant: str, name: str) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{part({'alg': 'none'})}.{part({'tid': tenant, 'name': name})}.c2ln"


class Refuse(Exception):
    def __init__(self, status: int, code: str, message: str, headers: Optional[dict] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


FILTER_TERM = re.compile(r"\s*([A-Za-z]+(?:/[A-Za-z]+)*)\s+(eq|ne|ge|gt|le|lt)\s+('(?:[^']|'')*'|[^\s']+)\s*")


def parse_filter(text: str) -> list[tuple[str, str, Any]]:
    """``a eq 'x' and b ge 2026-...``: the terms, or a refusal for anything this stub (and the agent) does not use."""
    terms, rest = [], text
    while rest.strip():
        found = FILTER_TERM.match(rest)
        if not found:
            raise Refuse(400, "BadRequest", f"Invalid filter clause: {text}")
        name, op, value = found.groups()
        value = value[1:-1].replace("''", "'") if value.startswith("'") else \
            {"true": True, "false": False}.get(value, value)
        terms.append((name, op, value))
        rest = rest[found.end():]
        if rest.strip():
            joined = re.match(r"\s*and\s+", rest)
            if not joined:
                raise Refuse(400, "BadRequest", f"Invalid filter clause: {text}")
            rest = rest[joined.end():]
    return terms


def check_order(filter_terms: list, orderby: str) -> None:
    """Graph's InefficientFilter rule: the ordered properties lead the filter, in the same order."""
    if not filter_terms or not orderby:
        return
    ordered = [part.split()[0] for part in orderby.split(",") if part.strip()]
    filtered = [name for name, _, _ in filter_terms]
    if filtered[:len(ordered)] != ordered:
        raise Refuse(400, "InefficientFilter", TOO_COMPLEX)


def compare(value: Any, op: str, wanted: Any) -> bool:
    if isinstance(wanted, str) and re.match(r"\d{4}-\d{2}-\d{2}T", wanted) and value:
        value, wanted = parse_time(str(value)), parse_time(wanted)
    return {"eq": value == wanted, "ne": value != wanted, "ge": value >= wanted, "gt": value > wanted,
            "le": value <= wanted, "lt": value < wanted}[op]


def dig(item: dict, name: str) -> Any:
    for part in name.split("/"):
        item = (item or {}).get(part) if isinstance(item, dict) else None
    return item.lower() if isinstance(item, str) and "address" in name else item


class GraphStub:
    WORK = "demo@sidra.example"
    WORK_UPN = "demo.k@sidra.onmicrosoft.example"
    WORK_ALIAS = "sales@sidra.example"
    PERSONAL = "sidra.demo@outlook.example"

    def __init__(self):
        self._ids = itertools.count(1)
        self.tokens: dict[str, dict] = {}
        self.work_token = self.issue("work")
        self.personal_token = self.issue("personal")
        self.requests: list[dict] = []
        self.throttled, self.retry_after, self.throttle_where = 0, "1", ""
        self.drop_send = False
        self.sends: list[str] = []
        # mail
        self.folders: dict[str, dict] = {}
        for well_known, name in (("inbox", "Inbox"), ("archive", "Archive"), ("drafts", "Drafts"),
                                 ("sentitems", "Sent Items"), ("deleteditems", "Deleted Items"),
                                 ("junkemail", "Junk Email")):
            self.folders[f"FOLDER-{well_known}="] = {"id": f"FOLDER-{well_known}=", "displayName": name,
                                                     "parentFolderId": None, "wellKnown": well_known}
        self.messages: dict[str, dict] = {}
        self.attachments: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        # calendar
        self.mailbox_zone = "Arabian Standard Time"
        self.working_days = ["sunday", "monday", "tuesday", "wednesday", "thursday"]
        self.calendars = {"CAL-main=": {"id": "CAL-main=", "name": "Calendar", "isDefaultCalendar": True,
                                        "canEdit": True},
                          "CAL-team=": {"id": "CAL-team=", "name": "Team Holidays", "isDefaultCalendar": False,
                                        "canEdit": True}}
        self.events: dict[str, dict] = {}
        self.colleagues: dict[str, list] = {}
        self.responses: list[dict] = []
        self.cancellations: list[str] = []
        # To Do
        self.lists = {"LIST-tasks=": {"id": "LIST-tasks=", "displayName": "Tasks", "wellknownListName": "defaultList",
                                      "isShared": False},
                      "LIST-flagged=": {"id": "LIST-flagged=", "displayName": "Flagged Emails",
                                        "wellknownListName": "flaggedEmails", "isShared": False}}
        self.tasks: dict[str, dict] = {}
        self.removed: list[dict] = []
        self.sequence = 0
        self.delta_expired = False
        self._server: Optional[ThreadingHTTPServer] = None

    # -- accounts --------------------------------------------------------------------------------------------------
    def issue(self, kind: str, scopes: Optional[set] = None) -> str:
        n = next(self._ids)
        token = jwt(WORK_TENANT, f"w{n}") if kind == "work" else f"EwB0A8l6BAAU-personal-{n}"
        self.tokens[token] = {"kind": kind, "scopes": set(ALL_SCOPES if scopes is None else scopes)}
        return token

    def secret(self, kind: str = "work", token: str = "", account: str = "") -> dict:
        return {"account": account or (self.WORK if kind == "work" else self.PERSONAL),
                "access_token": token or (self.work_token if kind == "work" else self.personal_token),
                "api_base_url": self.url + "/api"}

    def profile(self, kind: str) -> dict:
        if kind == "work":
            return {"id": "user-work", "mail": self.WORK, "userPrincipalName": self.WORK_UPN, "displayName": "Demo",
                    "otherMails": [], "proxyAddresses": [f"SMTP:{self.WORK}", f"smtp:{self.WORK_ALIAS}"]}
        return {"id": "user-personal", "mail": None, "userPrincipalName": self.PERSONAL, "displayName": "Demo"}

    def throttle(self, count: int = 1, retry_after: str = "1", where: str = "") -> None:
        """The next ``count`` requests (those whose path and query hold ``where``) answered 429 with Retry-After."""
        self.throttled, self.retry_after, self.throttle_where = count, retry_after, where

    def new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):04x}/x="

    # -- mail data -------------------------------------------------------------------------------------------------
    def folder(self, name: str) -> str:
        for folder in self.folders.values():
            if name in (folder["id"], folder.get("wellKnown")):
                return folder["id"]
        raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")

    def add_folder(self, name: str, parent: str = "") -> str:
        folder_id = self.new_id("FOLDER-")
        self.folders[folder_id] = {"id": folder_id, "displayName": name,
                                   "parentFolderId": self.folder(parent) if parent else None}
        return folder_id

    def add_message(self, conversation_id: str, sender: str, to: str, subject: str, body: str, date: str,
                    attachments=(), folder: str = "inbox", html_body: bool = False, unique: Optional[str] = None,
                    cc: str = "", is_draft: bool = False, read: bool = False) -> str:
        message_id = self.new_id("AAMkAD")
        moment = parse_time(date)
        self.messages[message_id] = {
            "id": message_id, "conversationId": conversation_id, "internetMessageId": f"<{message_id}@stub.example>",
            "subject": subject, "bodyPreview": text_of(body)[:255],
            "body": {"contentType": "html" if html_body else "text", "content": body},
            "uniqueBody": {"contentType": "html" if html_body else "text", "content": body if unique is None
                           else unique},
            "from": mailbox(sender), "toRecipients": mailboxes(to), "ccRecipients": mailboxes(cc),
            "bccRecipients": [], "receivedDateTime": stamp(moment), "sentDateTime": stamp(moment),
            "hasAttachments": False,
            "isDraft": is_draft, "isRead": read, "flag": {"flagStatus": "notFlagged"}, "importance": "normal",
            "webLink": f"https://outlook.office365.com/owa/?ItemID={urllib.parse.quote(message_id, safe='')}",
            "parentFolderId": self.folder("drafts" if is_draft else folder)}
        for entry in attachments:
            self.add_attachment(message_id, *entry)
        return message_id

    def add_attachment(self, message_id: str, name: str, mime: str, raw: bytes, kind: Any = "file",
                       size: int = 0) -> str:
        inline = kind is True
        kind = "file" if isinstance(kind, bool) else kind
        attachment_id = self.new_id("ATT-")
        self.attachments[attachment_id] = {
            "message_id": message_id, "id": attachment_id, "name": name, "contentType": mime,
            "size": size or len(raw), "isInline": inline, "raw": raw,
            "@odata.type": {"file": "#microsoft.graph.fileAttachment", "item": "#microsoft.graph.itemAttachment",
                            "reference": "#microsoft.graph.referenceAttachment"}[kind]}
        if not inline:
            self.messages[message_id]["hasAttachments"] = True
        return attachment_id

    def conversation(self, conversation_id: str) -> list[dict]:
        return sorted((m for m in self.messages.values() if m["conversationId"] == conversation_id
                       and not m["isDraft"]), key=lambda m: m["receivedDateTime"])

    @property
    def drafts(self) -> dict:
        return {k: m for k, m in self.messages.items() if m["isDraft"]}

    def attachments_of(self, message_id: str) -> list[dict]:
        return [a for a in self.attachments.values() if a["message_id"] == message_id]

    # -- calendar data ---------------------------------------------------------------------------------------------
    def add_event(self, subject: str, start: str, end: str, *, attendees=(), organizer: str = "", show_as="busy",
                  all_day: bool = False, calendar: str = "CAL-main=", weekly_on: Optional[list] = None,
                  until: str = "", response: str = "") -> str:
        event_id = self.new_id("AAMkEv")
        organizer = organizer or self.WORK
        mine = organizer in (self.WORK, self.PERSONAL)
        event = {"id": event_id, "calendar": calendar, "subject": subject, "isAllDay": all_day, "showAs": show_as,
                 "attendees": [{"emailAddress": {"address": a, "name": a.split("@")[0]}, "type": "required",
                                "status": {"response": "none"}} for a in attendees],
                 "organizer": {"emailAddress": {"address": organizer, "name": organizer.split("@")[0]}},
                 "isOrganizer": mine, "isCancelled": False, "type": "singleInstance",
                 "responseStatus": {"response": "organizer" if mine else (response or "notResponded")},
                 "location": {"displayName": ""}, "body": {"contentType": "html", "content": "<p>Agenda</p>"},
                 "webLink": f"https://outlook.office365.com/calendar/item/{urllib.parse.quote(event_id, safe='')}"}
        if all_day:
            event["first"], event["after"] = date.fromisoformat(start), date.fromisoformat(end)
        else:
            event["startAt"], event["endAt"] = parse_time(start), parse_time(end)
        if weekly_on:
            event["type"] = "seriesMaster"
            event["recurrence"] = {"pattern": {"type": "weekly", "interval": 1, "daysOfWeek": weekly_on},
                                   "range": {"type": "endDate" if until else "noEnd",
                                             "startDate": event["startAt"].date().isoformat(),
                                             **({"endDate": until} if until else {})}}
        self.events[event_id] = event
        return event_id

    def instances(self, first: datetime, last: datetime) -> list[dict]:
        """Every occurrence overlapping [first, last): series expanded, as calendarView answers."""
        found = []
        for event in self.events.values():
            if event["type"] == "seriesMaster":
                pattern, span = event["recurrence"]["pattern"], event["recurrence"]["range"]
                days = {d.lower() for d in pattern["daysOfWeek"]}
                length = event["endAt"] - event["startAt"]
                day = max(event["startAt"].date(), first.date() - timedelta(days=1))
                while day <= last.date():
                    if span.get("endDate") and day > date.fromisoformat(span["endDate"]):
                        break
                    if day.strftime("%A").lower() in days:
                        start = datetime.combine(day, event["startAt"].time(), timezone.utc)
                        if start < last and start + length > first:
                            found.append({**event, "id": f"{event['id']}_{day:%Y%m%d}", "type": "occurrence",
                                          "seriesMasterId": event["id"], "startAt": start,
                                          "endAt": start + length, "recurrence": None})
                    day += timedelta(days=1)
                continue
            start, end = self.span(event)
            if start < last and end > first:
                found.append(event)
        return sorted(found, key=lambda e: self.span(e)[0])

    def span(self, event: dict) -> tuple[datetime, datetime]:
        if event["isAllDay"]:
            zone = zone_of(self.mailbox_zone)
            return (datetime.combine(event["first"], datetime.min.time(), zone).astimezone(timezone.utc),
                    datetime.combine(event["after"], datetime.min.time(), zone).astimezone(timezone.utc))
        return event["startAt"], event["endAt"]

    def shown(self, event: dict) -> dict:
        found = {k: v for k, v in event.items() if k not in ("calendar", "first", "after", "startAt", "endAt",
                                                             "transactionId")}
        if event["isAllDay"]:
            found["start"] = {"dateTime": f"{event['first']}T00:00:00.0000000", "timeZone": "UTC"}
            found["end"] = {"dateTime": f"{event['after']}T00:00:00.0000000", "timeZone": "UTC"}
        else:
            found["start"], found["end"] = graph_time(event["startAt"]), graph_time(event["endAt"])
        return found

    def event(self, event_id: str) -> dict:
        if event_id in self.events:
            return self.events[event_id]
        master, _, day = event_id.rpartition("_")
        if master in self.events and day:
            moment = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc)
            for found in self.instances(moment, moment + timedelta(days=1)):
                if found["id"] == event_id:
                    return found
        raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")

    # -- To Do data ------------------------------------------------------------------------------------------------
    def add_list(self, name: str) -> str:
        list_id = self.new_id("LIST-")
        self.lists[list_id] = {"id": list_id, "displayName": name, "wellknownListName": "none", "isShared": False}
        return list_id

    def add_task(self, title: str, list_id: str = "LIST-tasks=", status: str = "notStarted", due: Any = None,
                 completed: Any = None, modified: str = "") -> str:
        task_id = self.new_id("AAkALgTask")
        now = datetime.now(timezone.utc)
        self.sequence += 1
        self.tasks[task_id] = {"id": task_id, "list": list_id, "title": title, "status": status,
                               "importance": "normal", "isReminderOn": False,
                               "createdDateTime": stamp(now), "lastModifiedDateTime": modified or stamp(now),
                               "body": {"content": "", "contentType": "text"}, "seq": self.sequence,
                               **({"dueDateTime": due} if due else {}),
                               **({"completedDateTime": completed} if completed else {})}
        return task_id

    def touch(self, task: dict) -> None:
        self.sequence += 1
        task["seq"] = self.sequence
        task["lastModifiedDateTime"] = stamp(datetime.now(timezone.utc))

    def shown_task(self, task: dict) -> dict:
        return {k: v for k, v in task.items() if k not in ("list", "seq")}

    # -- the server ------------------------------------------------------------------------------------------------
    def start(self) -> "GraphStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def answer(self, status: int, body: Any = None, headers: Optional[dict] = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode("utf-8")
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def handle_any(self, method: str) -> None:
                url = urllib.parse.urlsplit(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
                stub.requests.append({"method": method, "path": "/".join(parts), "query": query,
                                      "auth": self.headers.get("Authorization"),
                                      "prefer": self.headers.get("Prefer") or "", "size": len(raw)})
                try:
                    if stub.throttled > 0 and stub.throttle_where in f"{'/'.join(parts)}?{url.query}":
                        stub.throttled -= 1
                        raise Refuse(429, "TooManyRequests", "Too many requests.", {"Retry-After": stub.retry_after})
                    if parts and parts[0] == "upload":
                        status, body, headers = stub.upload(method, parts, self.headers, raw)
                        return self.answer(status, body, headers)
                    token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    account = stub.tokens.get(token)
                    if account is None:
                        raise Refuse(401, "InvalidAuthenticationToken", "Access token has expired or is not yet "
                                                                        "valid.")
                    if parts[:1] != ["api"]:
                        raise Refuse(404, "BadRequest", "no route")
                    body = json.loads(raw) if raw and method != "PUT" else {}
                    prefer = self.headers.get("Prefer") or ""
                    if method == "POST" and parts[-1] == "send" and stub.drop_send:
                        stub.sends.append(parts[-2])
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    status, answer = stub.route(method, parts[1:], query, body, account, prefer, len(raw))
                    if isinstance(answer, tuple) and answer[0] == "RAW":
                        self.send_response(status)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(answer[1])))
                        self.end_headers()
                        self.wfile.write(answer[1])
                        return
                    self.answer(status, answer)
                except Refuse as refused:
                    self.answer(refused.status, {"error": {"code": refused.code, "message": refused.message}},
                                refused.headers)
                except ValueError as wrong:
                    self.answer(400, {"error": {"code": "ErrorInvalidRequest", "message": str(wrong)}})

            def do_GET(self):  # noqa: N802
                self.handle_any("GET")

            def do_POST(self):  # noqa: N802
                self.handle_any("POST")

            def do_PATCH(self):  # noqa: N802
                self.handle_any("PATCH")

            def do_PUT(self):  # noqa: N802
                self.handle_any("PUT")

            def do_DELETE(self):  # noqa: N802
                self.handle_any("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("graph.microsoft.com", self._server)

    @property
    def local(self) -> str:
        """Where a test reaches it directly, not through an agent's way out."""
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- routes ----------------------------------------------------------------------------------------------------
    def route(self, method: str, p: list, q: dict, body: dict, account: dict, prefer: str, size: int):
        if p[:1] != ["me"]:
            raise Refuse(404, "BadRequest", f"no route {'/'.join(p)}")
        p = p[1:]
        if not p:
            return 200, self.selected(self.profile(account["kind"]), q)
        head = p[0]
        if head == "mailboxSettings":
            if "MailboxSettings.Read" not in account["scopes"]:
                raise Refuse(403, "ErrorAccessDenied", "Access is denied. Check credentials and try again.")
            return 200, {"timeZone": self.mailbox_zone, "workingHours": {
                "daysOfWeek": self.working_days, "startTime": "08:00:00.0000000", "endTime": "17:00:00.0000000",
                "timeZone": {"name": self.mailbox_zone}}}
        if head in ("messages", "mailFolders"):
            return self.mail(method, p, q, body, account, prefer, size)
        if head in ("calendar", "calendars", "calendarView", "events"):
            return self.calendar(method, p, q, body, account)
        if head == "todo":
            return self.todo(method, p[1:], q, body)
        raise Refuse(404, "BadRequest", f"no route {'/'.join(p)}")

    @staticmethod
    def selected(item: dict, q: dict) -> dict:
        wanted = [s.strip() for s in q.get("$select", "").split(",") if s.strip()]
        if not wanted:
            return item
        return {k: v for k, v in item.items() if k in wanted or k in ("id", "@odata.type")}

    def page(self, items: list, q: dict, path: str, default_top: int = 10) -> dict:
        top = int(q.get("$top") or default_top)
        skip = int(q.get("$skiptoken") or 0)
        chunk = items[skip:skip + top]
        found = {"value": chunk}
        if skip + top < len(items):
            following = {**q, "$skiptoken": str(skip + top)}
            found["@odata.nextLink"] = f"{self.url}/api/{path}?{urllib.parse.urlencode(following)}"
        return found

    # -- mail ------------------------------------------------------------------------------------------------------
    def message(self, message_id: str) -> dict:
        if message_id not in self.messages:
            raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")
        return self.messages[message_id]

    def shown_message(self, message: dict, q: dict, prefer: str) -> dict:
        found = dict(message)
        if 'outlook.body-content-type="text"' in prefer:
            for key in ("body", "uniqueBody"):
                if found[key]["contentType"] == "html":
                    found[key] = {"contentType": "text", "content": text_of(found[key]["content"])}
        if not q.get("$select"):
            found.pop("uniqueBody")
        return self.selected(found, q)

    def mail(self, method, p, q, body, account, prefer, size):
        if p[0] == "mailFolders":
            if len(p) == 1:
                tops = [f for f in self.folders.values() if f["parentFolderId"] is None]
                return 200, self.page([self.folder_row(f) for f in tops], q, "me/mailFolders")
            folder = self.folder(p[1])
            if len(p) == 3 and p[2] == "childFolders":
                kids = [f for f in self.folders.values() if f["parentFolderId"] == folder]
                return 200, self.page([self.folder_row(f) for f in kids], q,
                                      f"me/mailFolders/{urllib.parse.quote(p[1], safe='')}/childFolders")
            if len(p) == 3 and p[2] == "messages" and method == "GET":
                path = f"me/mailFolders/{urllib.parse.quote(p[1], safe='')}/messages"
                return 200, self.listing(q, prefer, folder, path)
            raise Refuse(404, "BadRequest", "no route")
        if len(p) == 1:
            if method == "GET":
                return 200, self.listing(q, prefer, None, "me/messages")
            if method == "POST":
                draft_id = self.add_message(f"conv-{next(self._ids)}", self.WORK, "", str(body.get("subject") or ""),
                                            str((body.get("body") or {}).get("content") or ""), stamp(datetime.now(
                                                timezone.utc)), is_draft=True,
                                            html_body=(body.get("body") or {}).get("contentType") == "html")
                draft = self.messages[draft_id]
                for key in ("toRecipients", "ccRecipients", "bccRecipients"):
                    draft[key] = body.get(key) or []
                return 201, draft
        message = self.message(p[1])
        if len(p) == 2:
            if method == "GET":
                return 200, self.shown_message(message, q, prefer)
            if method == "PATCH":
                for key in ("isRead", "flag", "toRecipients", "ccRecipients", "bccRecipients", "subject", "body"):
                    if key in body:
                        message[key] = body[key]
                return 200, message
            if method == "DELETE":
                del self.messages[p[1]]
                return 204, None
        action = p[2]
        if action in ("createReply", "createReplyAll", "createForward") and method == "POST":
            return 201, self.reply(message, action, body, account)
        if action == "send" and method == "POST":
            self.sends.append(p[1])
            if not message["isDraft"]:
                raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")
            if not message["toRecipients"] and not message["ccRecipients"] and not message["bccRecipients"]:
                raise Refuse(400, "ErrorInvalidRecipients", "At least one recipient isn't valid.")
            message.update(isDraft=False, parentFolderId=self.folder("sentitems"),
                           sentDateTime=stamp(datetime.now(timezone.utc)))
            return 202, None
        if action == "move" and method == "POST":
            message["parentFolderId"] = self.folder(str(body.get("destinationId") or ""))
            return 201, message
        if action == "attachments":
            return self.attachment_route(method, message, p[3:], q, body, size)
        raise Refuse(404, "BadRequest", "no route")

    def folder_row(self, folder: dict) -> dict:
        inside = [m for m in self.messages.values() if m["parentFolderId"] == folder["id"]]
        return {"id": folder["id"], "displayName": folder["displayName"], "parentFolderId": folder["parentFolderId"],
                "childFolderCount": sum(1 for f in self.folders.values() if f["parentFolderId"] == folder["id"]),
                "unreadItemCount": sum(1 for m in inside if not m["isRead"]), "totalItemCount": len(inside)}

    def listing(self, q: dict, prefer: str, folder: Optional[str], path: str) -> dict:
        pool = [m for m in self.messages.values() if folder is None or m["parentFolderId"] == folder]
        if "$search" in q:
            if "$filter" in q or "$orderby" in q:
                raise Refuse(400, "ErrorInvalidUrlQuery", "The query parameter '$filter' or '$orderby' is not "
                                                          "supported with '$search'.")
            if "$skip" in q:
                raise Refuse(400, "ErrorInvalidUrlQuery", "The query parameter '$skip' is not supported with "
                                                          "'$search'.")
            terms = q["$search"].strip('"').split()
            pool = [m for m in pool if not m["isDraft"] and self.matches(m, terms)]
            pool.sort(key=lambda m: m["sentDateTime"], reverse=True)
        else:
            terms = parse_filter(q.get("$filter", ""))
            check_order(terms, q.get("$orderby", ""))
            pool = [m for m in pool if all(compare(dig(m, n), op, v) for n, op, v in terms)]
            order = q.get("$orderby", "")
            if order:
                pool.sort(key=lambda m: m["receivedDateTime"], reverse=order.endswith("desc"))
        return self.page([self.shown_message(m, q, prefer) for m in pool], q, path)

    @staticmethod
    def matches(message: dict, terms: list) -> bool:
        sender = message["from"]["emailAddress"]["address"].lower()
        subject, text = message["subject"].lower(), text_of(message["body"]["content"]).lower()
        for term in terms:
            low = term.lower()
            if low in ("and", "or") or low.startswith(("received", "hasattachments:")):
                continue
            if low.startswith("from:"):
                if low[5:] not in sender:
                    return False
            elif low.startswith("subject:"):
                if low[8:] not in subject:
                    return False
            elif low not in subject and low not in text and low not in sender:
                return False
        return True

    def reply(self, source: dict, action: str, body: dict, account: dict) -> dict:
        me = self.WORK if account["kind"] == "work" else self.PERSONAL
        subject = source["subject"]
        prefix = "FW: " if action == "createForward" else "RE: "
        quoted = (f"<p>{html.escape(str(body.get('comment') or ''))}</p><hr><p>From: "
                  f"{source['from']['emailAddress']['address']}</p><blockquote>{source['body']['content']}"
                  f"</blockquote>")
        draft_id = self.add_message(source["conversationId"], me, "", prefix + subject, quoted,
                                    stamp(datetime.now(timezone.utc)), is_draft=True, html_body=True)
        draft = self.messages[draft_id]
        if action == "createForward":
            draft["toRecipients"] = body.get("toRecipients") or []
            for item in self.attachments_of(source["id"]):
                self.add_attachment(draft_id, item["name"], item["contentType"], item["raw"])
        else:
            draft["toRecipients"] = [source["from"]]
            if action == "createReplyAll":
                everyone = source["toRecipients"] + source["ccRecipients"]
                draft["ccRecipients"] = [r for r in everyone if r["emailAddress"]["address"].lower()
                                         not in (self.WORK, self.WORK_UPN, self.WORK_ALIAS, self.PERSONAL)]
        return draft

    def attachment_route(self, method, message, rest, q, body, size):
        if not rest:
            if method == "GET":
                items = [{k: v for k, v in a.items() if k not in ("raw", "message_id")}
                         for a in self.attachments_of(message["id"])]
                return 200, self.page([self.selected(i, q) for i in items], q, "x", default_top=100)
            if method == "POST":
                if size > FOUR_MB:
                    raise Refuse(413, "RequestBodyTooLarge", "The maximum request length supported is 4MB.")
                raw = base64.b64decode(str(body.get("contentBytes") or ""))
                attachment_id = self.add_attachment(message["id"], str(body["name"]), str(body["contentType"]), raw)
                return 201, {"id": attachment_id, "name": body["name"]}
        if rest == ["createUploadSession"] and method == "POST":
            item = body.get("AttachmentItem") or {}
            assert item.get("attachmentType") == "file" and item.get("name") and int(item.get("size") or 0) > 0, item
            session = f"s{next(self._ids)}"
            self.sessions[session] = {"message_id": message["id"], "name": item["name"], "size": int(item["size"]),
                                      "contentType": item.get("contentType") or "application/octet-stream",
                                      "data": bytearray(), "puts": 0}
            return 201, {"uploadUrl": f"{self.url}/upload/{session}?authtoken=stub", "nextExpectedRanges": ["0-"],
                         "expirationDateTime": "2030-01-01T00:00:00Z"}
        item = self.attachments.get(rest[0])
        if item is None or item["message_id"] != message["id"]:
            raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")
        if rest[1:] == ["$value"]:
            return 200, ("RAW", item["raw"])
        shown = {k: v for k, v in item.items() if k not in ("raw", "message_id")}
        if not q.get("$select") and shown["@odata.type"].endswith("fileAttachment"):
            shown["contentBytes"] = base64.b64encode(item["raw"]).decode()
        return 200, self.selected(shown, q)

    def upload(self, method, parts, headers, raw):
        session = self.sessions.get(parts[1])
        if session is None or method != "PUT":
            raise Refuse(404, "ErrorItemNotFound", "The upload session was not found.")
        if headers.get("Authorization"):
            raise Refuse(401, "InvalidAudience", "The upload URL is pre-authenticated: do not send Authorization.")
        found = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("Content-Range") or "")
        if not found:
            raise Refuse(400, "InvalidRange", "Content-Range is required.")
        first, last, total = map(int, found.groups())
        if first != len(session["data"]) or last - first + 1 != len(raw) or total != session["size"]:
            raise Refuse(416, "InvalidRange", "The range is not the next expected one.")
        if len(raw) > FOUR_MB:
            raise Refuse(413, "RequestBodyTooLarge", "Keep each byte range under 4 MB.")
        session["data"] += raw
        session["puts"] += 1
        if len(session["data"]) < session["size"]:
            return 200, {"nextExpectedRanges": [f"{len(session['data'])}-"]}, {}
        attachment_id = self.add_attachment(session["message_id"], session["name"], session["contentType"],
                                            bytes(session["data"]))
        return 201, None, {"Location": f"{self.url}/api/me/messages/x/attachments/{attachment_id}"}

    # -- calendar --------------------------------------------------------------------------------------------------
    def calendar(self, method, p, q, body, account):
        personal = account["kind"] == "personal"
        asked = "/".join(urllib.parse.quote(s, safe="") for s in p)
        if p == ["calendars"]:
            return 200, self.page([{**c, "owner": {"name": "Demo", "address": self.WORK}}
                                   for c in self.calendars.values()], q, "me/calendars")
        calendar = "CAL-main="
        if p[0] == "calendars":
            if p[1] not in self.calendars:
                raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")
            calendar, p = p[1], ["calendar"] + p[2:]
        if p == ["calendar", "getSchedule"] and method == "POST":
            if personal:
                raise Refuse(403, "ErrorAccessDenied", "Access is denied. Check credentials and try again.")
            return 200, self.schedule(body)
        if p[-1] == "calendarView" and method == "GET":
            if not q.get("startDateTime") or not q.get("endDateTime"):
                raise Refuse(400, "ErrorInvalidParameter", "This request requires a time window specified by the "
                                                           "query string parameters StartDateTime and EndDateTime.")
            if q.get("$filter") and q.get("$orderby"):
                raise Refuse(400, "InefficientFilter", TOO_COMPLEX)
            first, last = parse_time(q["startDateTime"]), parse_time(q["endDateTime"])
            found = [self.selected(self.shown(e), q) for e in self.instances(first, last)
                     if e["calendar"] == calendar]
            return 200, self.page(found, q, f"me/{asked}")
        if p[-1] == "events" and method == "POST":
            return 201, self.create_event(body, calendar, personal)
        if p[0] == "events" and len(p) >= 2:
            event = self.event(p[1])
            if len(p) == 2:
                if method == "GET":
                    return 200, self.selected(self.shown(event), q)
                if method == "PATCH":
                    if not event["isOrganizer"]:
                        raise Refuse(403, "ErrorAccessDenied", "Only the organizer can change this meeting.")
                    self.apply_event(event, body)
                    return 200, self.shown(event)
                if method == "DELETE":
                    self.events.pop(p[1], None)
                    return 204, None
            action = p[2]
            if action == "cancel":
                if not event["isOrganizer"]:
                    raise Refuse(400, "ErrorInvalidRequest", "Your request can't be completed. You need to be an "
                                                             "organizer to cancel a meeting.")
                self.cancellations.append(event["id"])
                self.events.pop(event["id"], None)
                return 202, None
            if action in ("accept", "tentativelyAccept", "decline"):
                if event["isOrganizer"]:
                    raise Refuse(400, "ErrorInvalidRequest", "Your request can't be completed. You can't respond to "
                                                             "a meeting you organized.")
                event["responseStatus"] = {"response": {"accept": "accepted", "decline": "declined"}.get(
                    action, "tentativelyAccepted")}
                self.responses.append({"event_id": event["id"], "action": action, **body})
                return 202, None
        raise Refuse(404, "BadRequest", f"no route {'/'.join(p)}")

    def create_event(self, body: dict, calendar: str, personal: bool) -> dict:
        if body.get("transactionId"):
            for event in self.events.values():
                if event.get("transactionId") == body["transactionId"]:
                    return self.shown(event)
        if body.get("isOnlineMeeting") and body.get("onlineMeetingProvider") == "teamsForBusiness" and personal:
            raise Refuse(400, "ErrorInvalidRequest", "Online meeting provider teamsForBusiness is not allowed.")
        attendees = [a["emailAddress"]["address"] for a in body.get("attendees") or []]
        start, end = body["start"], body["end"]
        if body.get("isAllDay"):
            first, after = from_graph(start), from_graph(end)
            for value in (start, end):
                if not str(value["dateTime"]).endswith("T00:00:00"):
                    raise Refuse(400, "ErrorInvalidRequest", "An all-day event must start and end at midnight.")
            event_id = self.add_event(body["subject"], str(start["dateTime"])[:10], str(end["dateTime"])[:10],
                                      attendees=attendees, all_day=True, calendar=calendar)
            del first, after
        else:
            event_id = self.add_event(body["subject"], stamp(from_graph(start)), stamp(from_graph(end)),
                                      attendees=attendees, calendar=calendar,
                                      organizer=self.PERSONAL if personal else self.WORK)
        event = self.events[event_id]
        event["transactionId"] = body.get("transactionId")
        event["location"] = body.get("location") or {"displayName": ""}
        event["body"] = body.get("body") or {"contentType": "text", "content": ""}
        for given, made in zip(body.get("attendees") or [], event["attendees"]):
            made["type"] = given.get("type", "required")
        if body.get("isOnlineMeeting"):
            event["isOnlineMeeting"] = True
            event["onlineMeeting"] = {"joinUrl": f"https://teams.microsoft.com/l/meetup-join/{next(self._ids)}"}
        return self.shown(event)

    def apply_event(self, event: dict, body: dict) -> None:
        for key in ("subject", "location", "body"):
            if key in body:
                event[key] = body[key]
        if "start" in body:
            if event["isAllDay"]:
                event["first"] = date.fromisoformat(str(body["start"]["dateTime"])[:10])
                event["after"] = date.fromisoformat(str(body["end"]["dateTime"])[:10])
            else:
                event["startAt"], event["endAt"] = from_graph(body["start"]), from_graph(body["end"])

    def schedule(self, body: dict) -> dict:
        first, last = from_graph(body["startTime"]), from_graph(body["endTime"])
        answer = []
        for address in body["schedules"]:
            if address.lower() == self.WORK:
                items = [{"status": e["showAs"], "start": graph_time(self.span(e)[0]),
                          "end": graph_time(self.span(e)[1])} for e in self.instances(first, last)
                         if e["calendar"] == "CAL-main=" and not e["isCancelled"]]
            elif address.lower() in self.colleagues:
                items = [{"status": status, "start": graph_time(parse_time(s)), "end": graph_time(parse_time(e))}
                         for s, e, status in self.colleagues[address.lower()]]
            else:
                answer.append({"scheduleId": address, "error": {
                    "message": "The specified recipient is not a user in this organization.",
                    "responseCode": "ErrorRecipientNotFound"}})
                continue
            answer.append({"scheduleId": address, "scheduleItems": items})
        return {"value": answer}

    # -- To Do -----------------------------------------------------------------------------------------------------
    def todo(self, method, p, q, body):
        if p == ["lists"]:
            if method == "GET":
                return 200, self.page(list(self.lists.values()), q, "me/todo/lists")
            if method == "POST":
                list_id = self.add_list(str(body["displayName"]))
                return 201, self.lists[list_id]
        if p[0] != "lists" or p[1] not in self.lists:
            raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")
        list_id = p[1]
        if len(p) == 2:
            return 200, self.lists[list_id]
        if p[2:] == ["tasks"]:
            if method == "POST":
                self.check_zones(body)
                task_id = self.add_task(str(body.get("title") or ""), list_id)
                task = self.tasks[task_id]
                task.update({k: v for k, v in body.items() if k != "title"})
                return 201, self.shown_task(task)
            terms = parse_filter(q.get("$filter", ""))
            check_order(terms, q.get("$orderby", ""))
            found = [t for t in self.tasks.values() if t["list"] == list_id
                     and all(compare(dig(t, n), op, v) for n, op, v in terms)]
            return 200, self.page([self.shown_task(t) for t in found], q,
                                  f"me/todo/lists/{urllib.parse.quote(list_id, safe='')}/tasks")
        if p[2:] == ["tasks", "delta"]:
            return 200, self.delta(list_id, q)
        task = self.tasks.get(p[3]) if len(p) > 3 else None
        if task is None or task["list"] != list_id:
            raise Refuse(404, "ErrorItemNotFound", "The specified object was not found in the store.")
        if len(p) == 5 and p[4] == "checklistItems":
            return 200, {"value": task.get("checklist") or []}
        if method == "GET":
            if q.get("$select"):
                # Real To Do refuses $select on one task (seen live 2026-11-20).
                raise Refuse(400, "invalidRequest", "Invalid request.")
            return 200, self.shown_task(task)
        if method == "PATCH":
            self.check_zones(body)
            for key, value in body.items():
                if value is None:
                    task.pop(key, None)
                else:
                    task[key] = value
            if body.get("status") == "completed":
                task["completedDateTime"] = graph_time(datetime.now(timezone.utc))
            elif "status" in body:
                task.pop("completedDateTime", None)
            self.touch(task)
            return 200, self.shown_task(task)
        if method == "DELETE":
            del self.tasks[task["id"]]
            self.sequence += 1
            self.removed.append({"id": task["id"], "list": list_id, "seq": self.sequence})
            return 204, None
        raise Refuse(404, "BadRequest", "no route")

    @staticmethod
    def check_zones(body: dict) -> None:
        for key in ("dueDateTime", "reminderDateTime", "completedDateTime"):
            if body.get(key):
                zone_of(str(body[key].get("timeZone") or ""))

    def delta(self, list_id: str, q: dict) -> dict:
        if self.delta_expired and q.get("$deltatoken"):
            raise Refuse(410, "SyncStateNotFound", "The sync state generation is not found.")
        since = int(q.get("$deltatoken") or -1)
        changed = [self.shown_task(t) for t in sorted(self.tasks.values(), key=lambda t: t["seq"])
                   if t["list"] == list_id and t["seq"] > since]
        if since >= 0:
            changed += [{"id": r["id"], "@removed": {"reason": "deleted"}} for r in self.removed
                        if r["list"] == list_id and r["seq"] > since]
        skip, top = int(q.get("$skiptoken") or 0), 50
        found = {"value": changed[skip:skip + top]}
        base = f"{self.url}/api/me/todo/lists/{urllib.parse.quote(list_id, safe='')}/tasks/delta"
        if skip + top < len(changed):
            found["@odata.nextLink"] = f"{base}?{urllib.parse.urlencode({**q, '$skiptoken': skip + top})}"
        else:
            found["@odata.deltaLink"] = f"{base}?$deltatoken={self.sequence}"
        return found
