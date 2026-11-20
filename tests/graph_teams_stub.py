"""A loopback Microsoft Graph: the Teams subset the Teams agent calls, over real HTTP, holding fictional chats for
Sidra Office Supplies.

It is as strict as Graph where Microsoft documents a rule (Microsoft Learn, "List chats", "List messages in a chat",
"Create chat", "Send chatMessage in a channel or a chat", "List channel messages"):

- ``me/chats``: ``$top`` at most 50; ``$orderby`` only ``lastMessagePreview/createdDateTime desc``; ``$expand`` only
  ``members`` and ``lastMessagePreview`` (at most 25 members each); pages by ``@odata.nextLink``.
- ``chats/{id}/messages``: ``$top`` at most 50; ``$orderby`` only ``lastModifiedDateTime desc`` or ``createdDateTime
  desc``; ``$filter`` on ``lastModifiedDateTime`` (gt, lt) or ``createdDateTime`` (lt only), and ignored unless it is
  on the ordered property.
- An unknown query option is refused (400).
- Creating a one-to-one chat takes exactly two members bound by ``https://graph.microsoft.com/v1.0/users('…')``,
  each an owner; an existing one is returned rather than made twice.
- A message's ``<at id="n">`` tags and its ``mentions`` must match, as HTML, naming members of the chat.
- Channel messages are read only with ``ChannelMessage.Read.All``, which this account was not granted (403), unless a
  test grants it.
- A personal account's token (Microsoft's consumer tenant) is refused for everything but ``me``.
- ``throttle(n)`` answers the next n requests 429 with ``Retry-After``.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from tests.conftest import stands_for

WORK_TENANT = "3c9a1f52-6a3e-4d9b-9a51-0d6a8c7e2b11"
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
PEOPLE = {
    "demo@sidra.example": ("u-demo", "Demo Rahman"),
    "dana@sidra.example": ("u-dana", "Dana Harbour"),
    "omar@sidra.example": ("u-omar", "Omar Nasser"),
    "priya@sidra.example": ("u-priya", "Priya Menon"),
    "layla@sidra.example": ("u-layla", "ليلى حداد"),
}
BY_ID = {uid: (address, name) for address, (uid, name) in PEOPLE.items()}
DM_DANA = "19:u-demo_u-dana@unq.gbl.spaces"
OPS = "19:ops-group@thread.v2"
TEAM = "team-sidra"
GENERAL = "19:general@thread.tacv2"
SHOWROOM = "19:showroom@thread.tacv2"
BIND = re.compile(r"^https://graph\.microsoft\.com/v1\.0/users\('([^']+)'\)$")
AT = re.compile(r'<at id="(\d+)">')


def token(tenant: str = WORK_TENANT, oid: str = "u-demo") -> str:
    """An access token shaped as Microsoft's: a JWT naming its tenant and the user's object id."""
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part({'tid': tenant, 'oid': oid})}.c2lnbmF0dXJl"


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def ago(minutes: float) -> str:
    return stamp(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def parsed(text: str) -> datetime:
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00")))


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class GraphTeamsStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.token = token()
        self.chats: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}
        self.channel_messages: Dict[str, List[Dict[str, Any]]] = {GENERAL: [], SHOWROOM: []}
        self.replies: Dict[str, List[Dict[str, Any]]] = {}
        self.requests: List[tuple] = []
        self.sends: List[Dict[str, Any]] = []
        #: The message arrives and no answer ever comes; with ``drop_after_storing`` False it never arrives.
        self.drop_send = False
        self.drop_after_storing = False
        self.channel_read_granted = False
        self.refuse_both_expands = False
        self.failing_chats: set = set()
        self._throttled, self._retry_after = 0, "0"
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self.add_chat(DM_DANA, "oneOnOne", None, ["demo@sidra.example", "dana@sidra.example"], read=ago(1000))
        self.add_chat(OPS, "group", "Ops", ["demo@sidra.example", "omar@sidra.example", "priya@sidra.example"],
                      read=ago(1))
        self.add_message(DM_DANA, "dana@sidra.example",
                         "<p>Can you send the showroom drawings by <b>Thursday</b>?</p>"
                         "<p>Harbourline &amp; I want to sign off the layout.</p>", ago(120))
        self.system(DM_DANA, ago(121))
        self.add_message(OPS, "omar@sidra.example", "<p>Northlight moved the chairs to the 24th.</p>", ago(30))
        self.teams = [{"id": TEAM, "displayName": "Sidra Office", "description": "Everyone at Sidra"}]
        self.channels = {TEAM: [{"id": GENERAL, "displayName": "General", "membershipType": "standard"},
                                {"id": SHOWROOM, "displayName": "Showroom", "membershipType": "standard"}]}

    # -- fictional data ------------------------------------------------------------------------------------------
    def add_chat(self, chat_id: str, kind: str, topic: Optional[str], people: List[str], read: str = "") -> None:
        self.chats[chat_id] = {
            "id": chat_id, "chatType": kind, "topic": topic, "createdDateTime": ago(10000),
            "lastUpdatedDateTime": ago(10000), "tenantId": WORK_TENANT,
            "viewpoint": {"isHidden": False, "lastMessageReadDateTime": read or "0001-01-01T00:00:00Z"},
            "webUrl": f"https://teams.microsoft.com/l/chat/{urllib.parse.quote(chat_id, safe='')}/0",
            "members": [{"@odata.type": "#microsoft.graph.aadUserConversationMember",
                         "id": f"m-{PEOPLE[p][0]}", "roles": ["owner"], "userId": PEOPLE[p][0], "email": p,
                         "displayName": PEOPLE[p][1]} for p in people]}
        self.messages.setdefault(chat_id, [])

    def made(self, sender: str, content: str, at: str, content_type: str = "html", **extra) -> Dict[str, Any]:
        self._n += 1
        user_id, name = PEOPLE[sender]
        return {"id": f"{1757400000000 + self._n}", "replyToId": None, "messageType": "message",
                "createdDateTime": at, "lastModifiedDateTime": at, "deletedDateTime": None,
                "from": {"application": None, "device": None,
                         "user": {"id": user_id, "displayName": name, "userIdentityType": "aadUser"}},
                "body": {"contentType": content_type, "content": content}, "attachments": [], "mentions": [],
                **extra}

    def add_message(self, chat_id: str, sender: str, content: str, at: str = "",
                    content_type: str = "html") -> Dict[str, Any]:
        message = self.made(sender, content, at or stamp(datetime.now(timezone.utc)), content_type, chatId=chat_id,
                            webUrl=f"https://teams.microsoft.com/l/message/{urllib.parse.quote(chat_id, safe='')}/"
                                   f"{self._n + 1}")
        self.messages.setdefault(chat_id, []).append(message)
        self.chats[chat_id]["lastUpdatedDateTime"] = message["createdDateTime"]
        return message

    def reply(self, chat_id: str, sender: str, text: str) -> None:
        """Someone answers, a moment after the latest message."""
        latest = max(parsed(m["createdDateTime"]) for m in self.messages[chat_id])
        self.add_message(chat_id, sender, f"<p>{text}</p>", stamp(latest + timedelta(seconds=5)))

    def system(self, chat_id: str, at: str) -> None:
        self._n += 1
        self.messages[chat_id].append({
            "id": f"{1757400000000 + self._n}", "messageType": "systemEventMessage", "createdDateTime": at,
            "lastModifiedDateTime": at, "from": None, "deletedDateTime": None,
            "body": {"contentType": "html", "content": "<systemEventMessage/>"}})

    def add_channel_message(self, channel_id: str, sender: str, text: str, at: str = "") -> Dict[str, Any]:
        message = self.made(sender, f"<p>{text}</p>", at or ago(5),
                            channelIdentity={"teamId": TEAM, "channelId": channel_id})
        self.channel_messages[channel_id].append(message)
        return message

    def add_reply(self, parent_id: str, sender: str, text: str, at: str = "") -> Dict[str, Any]:
        message = self.made(sender, f"<p>{text}</p>", at or stamp(datetime.now(timezone.utc)), replyToId=parent_id)
        self.replies.setdefault(parent_id, []).append(message)
        return message

    def throttle(self, count: int, retry_after: str = "0") -> None:
        self._throttled, self._retry_after = count, retry_after

    def preview(self, chat_id: str) -> Optional[Dict[str, Any]]:
        found = [m for m in self.messages.get(chat_id, [])]
        if not found:
            return None
        last = max(found, key=lambda m: m["createdDateTime"])
        return {"id": last["id"], "createdDateTime": last["createdDateTime"],
                "isDeleted": bool(last.get("deletedDateTime")), "messageType": last["messageType"],
                "body": last["body"], "from": last["from"]}

    def writes(self) -> List[tuple]:
        return [r for r in self.requests if r[0] != "GET"]

    # -- the rules ----------------------------------------------------------------------------------------------
    @staticmethod
    def allowed(query: Dict[str, str], names: set) -> None:
        for name in query:
            if name not in names:
                raise Refused(400, "BadRequest", f"Query option '{name}' is not allowed on this resource.")

    @staticmethod
    def top(query: Dict[str, str], default: int = 20) -> int:
        try:
            value = int(query.get("$top", default))
        except ValueError:
            raise Refused(400, "BadRequest", "Invalid $top.") from None
        if not 1 <= value <= 50:
            raise Refused(400, "BadRequest", f"Invalid page size specified: '{value}'. Must be between 1 and 50 "
                                             f"inclusive.")
        return value

    def page(self, rows: List[Any], query: Dict[str, str], path: str, default: int = 20) -> Dict[str, Any]:
        size, start = self.top(query, default), int(query.get("$skiptoken", "0") or 0)
        answer: Dict[str, Any] = {"value": rows[start:start + size]}
        if start + size < len(rows):
            kept = {k: v for k, v in query.items() if k != "$skiptoken"}
            answer["@odata.nextLink"] = (f"{self.url}/api/{path}?"
                                         + urllib.parse.urlencode({**kept, "$skiptoken": str(start + size)}))
        return answer

    def chat_messages(self, chat_id: str, query: Dict[str, str]) -> List[Dict[str, Any]]:
        self.allowed(query, {"$top", "$orderby", "$filter", "$skiptoken"})
        order = query.get("$orderby", "lastModifiedDateTime desc")
        if order not in ("lastModifiedDateTime desc", "createdDateTime desc"):
            raise Refused(400, "BadRequest", f"$orderby '{order}' is not supported: only lastModifiedDateTime desc "
                                             f"and createdDateTime desc.")
        ordered_by = order.split()[0]
        rows = list(self.messages[chat_id])
        for clause in [c.strip() for c in query.get("$filter", "").split(" and ") if c.strip()]:
            found = re.fullmatch(r"(\w+) (gt|lt) (\S+)", clause)
            if not found or found.group(1) not in ("lastModifiedDateTime", "createdDateTime"):
                raise Refused(400, "BadRequest", f"Unsupported $filter: {clause}")
            prop, op, value = found.groups()
            if prop == "createdDateTime" and op != "lt":
                raise Refused(400, "BadRequest", "createdDateTime supports only the lt operator.")
            if prop != ordered_by:
                continue  # Graph ignores a filter not on the ordered property
            when = parsed(value)
            rows = [m for m in rows if (parsed(m[prop]) > when if op == "gt" else parsed(m[prop]) < when)]
        return sorted(rows, key=lambda m: m[ordered_by], reverse=True)

    def chat_view(self, chat: Dict[str, Any], expand: List[str]) -> Dict[str, Any]:
        view = {k: v for k, v in chat.items() if k != "members"}
        if "members" in expand:
            view["members"] = chat["members"][:25]
        if "lastMessagePreview" in expand:
            view["lastMessagePreview"] = self.preview(chat["id"])
        return view

    def check_message(self, body: Dict[str, Any], members: List[Dict[str, Any]]) -> None:
        content = body.get("body") or {}
        if content.get("contentType") not in ("text", "html") or not str(content.get("content") or "").strip():
            raise Refused(400, "BadRequest", "A message needs a body with contentType text or html and content.")
        tags = sorted(int(n) for n in AT.findall(content["content"]))
        mentions = body.get("mentions") or []
        if tags or mentions:
            if content["contentType"] != "html":
                raise Refused(400, "BadRequest", "Mentions need an html body.")
            if sorted(int(m.get("id")) for m in mentions) != sorted(set(tags)):
                raise Refused(400, "BadRequest", "Each <at id> must match one mention.")
            known = {m["userId"] for m in members}
            for mention in mentions:
                user = ((mention.get("mentioned") or {}).get("user") or {})
                if user.get("userIdentityType") != "aadUser" or (members and user.get("id") not in known):
                    raise Refused(400, "BadRequest", f"Mentioned user {user.get('id')} is not valid here.")

    # -- the server ---------------------------------------------------------------------------------------------
    def start(self) -> "GraphTeamsStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                raw = url.path[4:] if url.path.startswith("/api") else url.path
                parts = [urllib.parse.unquote(p) for p in raw.strip("/").split("/")]
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                return parts, query

            def _serve(self, method: str):
                parts, query = self._route()
                stub.requests.append((method, "/".join(parts), query))
                if stub._throttled > 0:
                    stub._throttled -= 1
                    return self._json(429, {"error": {"code": "TooManyRequests", "message": "Too many requests."}},
                                      {"Retry-After": stub._retry_after})
                auth = self.headers.get("Authorization") or ""
                personal = auth == f"Bearer {token(PERSONAL_TENANT)}"
                if auth != f"Bearer {stub.token}" and not personal:
                    return self._json(401, {"error": {"code": "InvalidAuthenticationToken",
                                                      "message": "Access token has expired or is not yet valid."}})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if method == "POST" and "application/json" not in (self.headers.get("Content-Type") or ""):
                    return self._json(415, {"error": {"code": "UnsupportedMediaType", "message": "JSON only."}})
                try:
                    if personal and parts != ["me"]:
                        raise Refused(403, "Forbidden", "Requested API is not supported in consumer accounts.")
                    status, answer = stub.handle(method, parts, query, body)
                except Refused as refused:
                    return self._json(refused.status, {"error": {"code": refused.code, "message": refused.message}})
                if status == "drop":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self._json(status, answer)

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self._url = stands_for("graph.microsoft.com", self._server)
        return self

    def handle(self, method: str, parts: List[str], query: Dict[str, str], body: Dict[str, Any]):
        missing = Refused(404, "NotFound", "The requested resource does not exist.")
        if method == "GET":
            if parts == ["me"]:
                self.allowed(query, {"$select"})
                return 200, {"id": "u-demo", "mail": self.ACCOUNT, "userPrincipalName": self.ACCOUNT,
                             "displayName": PEOPLE[self.ACCOUNT][1]}
            if parts == ["me", "chats"]:
                self.allowed(query, {"$expand", "$orderby", "$top", "$skiptoken"})
                expand = [e for e in query.get("$expand", "").split(",") if e]
                if any(e not in ("members", "lastMessagePreview") for e in expand):
                    raise Refused(400, "BadRequest", "Only members and lastMessagePreview can be expanded.")
                if self.refuse_both_expands and len(expand) == 2:
                    raise Refused(400, "BadRequest", "Only one navigation property can be expanded.")
                order = query.get("$orderby")
                if order and order != "lastMessagePreview/createdDateTime desc":
                    raise Refused(400, "BadRequest", "Only lastMessagePreview/createdDateTime desc is supported.")
                chats = list(self.chats.values())
                if order:
                    chats.sort(key=lambda c: (self.preview(c["id"]) or {}).get("createdDateTime") or "", reverse=True)
                return 200, self.page([self.chat_view(c, expand) for c in chats], query, "me/chats")
            if len(parts) == 2 and parts[0] == "chats":
                self.allowed(query, {"$expand"})
                chat = self.chats.get(parts[1])
                if chat is None:
                    raise missing
                return 200, self.chat_view(chat, [e for e in query.get("$expand", "").split(",") if e])
            if len(parts) == 3 and parts[0] == "chats" and parts[2] == "members":
                if parts[1] not in self.chats:
                    raise missing
                return 200, {"value": self.chats[parts[1]]["members"]}
            if len(parts) == 3 and parts[0] == "chats" and parts[2] == "messages":
                if parts[1] in self.failing_chats:
                    raise Refused(500, "InternalServerError", "Something went wrong.")
                if parts[1] not in self.chats:
                    raise missing
                return 200, self.page(self.chat_messages(parts[1], query), query, "/".join(parts))
            if parts == ["me", "joinedTeams"]:
                return 200, {"value": self.teams}
            if len(parts) == 3 and parts[0] == "teams" and parts[2] == "channels":
                if parts[1] not in self.channels:
                    raise missing
                return 200, {"value": self.channels[parts[1]]}
            if len(parts) == 4 and parts[0] == "teams" and parts[2] == "channels":
                found = next((c for c in self.channels.get(parts[1], []) if c["id"] == parts[3]), None)
                if found is None:
                    raise missing
                return 200, found
            if len(parts) in (5, 7) and parts[0] == "teams" and parts[2] == "channels" and parts[4] == "messages":
                if not self.channel_read_granted:
                    raise Refused(403, "Forbidden", "Missing scope permissions on the request. API requires one of "
                                                    "'ChannelMessage.Read.All, ChannelMessage.ReadWrite, "
                                                    "Group.Read.All, Group.ReadWrite.All'.")
                self.allowed(query, {"$top", "$skiptoken", "$expand"})
                if parts[3] not in self.channel_messages:
                    raise missing
                if len(parts) == 7 and parts[6] == "replies":
                    rows = self.replies.get(parts[5], [])
                else:
                    rows = sorted(self.channel_messages[parts[3]], key=lambda m: m["createdDateTime"], reverse=True)
                return 200, self.page(rows, query, "/".join(parts))
            raise missing
        # -- POST
        if parts == ["chats"]:
            members = body.get("members") or []
            if body.get("chatType") != "oneOnOne" or len(members) != 2:
                raise Refused(400, "BadRequest", "A one-on-one chat has exactly two members.")
            people = []
            for member in members:
                bound = BIND.match(str(member.get("user@odata.bind") or ""))
                if member.get("@odata.type") != "#microsoft.graph.aadUserConversationMember" or not bound \
                        or member.get("roles") != ["owner"]:
                    raise Refused(400, "BadRequest", "Each member is an aadUserConversationMember, an owner, bound "
                                                     "as https://graph.microsoft.com/v1.0/users('id').")
                who = bound.group(1).lower()
                address = who if who in PEOPLE else BY_ID.get(bound.group(1), ("",))[0]
                if not address:
                    raise Refused(404, "NotFound", f"User {who} not found.")
                people.append(address)
            ids = {PEOPLE[p][0] for p in people}
            for chat in self.chats.values():
                if chat["chatType"] == "oneOnOne" and {m["userId"] for m in chat["members"]} == ids:
                    return 201, self.chat_view(chat, [])
            chat_id = "19:" + "_".join(sorted(ids)) + "@unq.gbl.spaces"
            self.add_chat(chat_id, "oneOnOne", None, people)
            return 201, self.chat_view(self.chats[chat_id], [])
        if len(parts) == 3 and parts[0] == "chats" and parts[2] == "messages":
            if parts[1] not in self.chats:
                raise missing
            self.check_message(body, self.chats[parts[1]]["members"])
            self.sends.append({"chat_id": parts[1], **body})
            if self.drop_send and not self.drop_after_storing:
                return "drop", None
            made = self.add_message(parts[1], self.ACCOUNT, body["body"]["content"], content_type=body["body"]
                                    ["contentType"])
            made["mentions"] = body.get("mentions") or []
            if self.drop_send:
                return "drop", None
            return 201, made
        if len(parts) in (5, 7) and parts[0] == "teams" and parts[2] == "channels" and parts[4] == "messages":
            if parts[3] not in {c["id"] for c in self.channels.get(parts[1], [])}:
                raise missing
            self.check_message(body, [])
            self.sends.append({"team_id": parts[1], "channel_id": parts[3], **body})
            if len(parts) == 7:
                if parts[6] != "replies":
                    raise missing
                made = self.made(self.ACCOUNT, body["body"]["content"], stamp(datetime.now(timezone.utc)),
                                 body["body"]["contentType"], replyToId=parts[5])
                self.replies.setdefault(parts[5], []).append(made)
            else:
                made = self.made(self.ACCOUNT, body["body"]["content"], stamp(datetime.now(timezone.utc)),
                                 body["body"]["contentType"], subject=body.get("subject"),
                                 webUrl=f"https://teams.microsoft.com/l/message/{parts[3]}/{self._n + 1}")
                self.channel_messages[parts[3]].append(made)
            return 201, made
        raise missing

    @property
    def url(self) -> str:
        assert self._server is not None
        return self._url

    def secret(self, access_token: str = "", account: str = "") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": account or self.ACCOUNT, "access_token": access_token or self.token,
                "api_base_url": self.url + "/api"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
