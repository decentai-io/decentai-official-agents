"""A loopback Gmail, as strict as the real one where its rules are known: the REST surface the Gmail agent calls,
over real HTTP, at Gmail's own paths, holding fictional mail as raw RFC 5322 messages that are parsed the way Gmail
parses them.

What it holds the agent to (developers.google.com/workspace/gmail/api/reference/rest):

- A message's ``payload`` is a tree of parts; a leaf's ``body.data`` is its bytes **in the part's own charset**, in
  base64url. A part with a filename, a part that is not text, and a text part too long to come inline come with an
  ``attachmentId`` instead, a new one at every read (as Gmail does), fetched from ``messages/{id}/attachments/{id}``.
- ``history.list`` needs ``startHistoryId``; one older than what Gmail still keeps answers **404**. ``maxResults``
  is at most 500 for history and messages.
- A draft comes as JSON ``{"message": {"raw", "threadId"}}`` (``raw`` in base64url: ``+`` or ``/`` is refused; a JSON
  body over 5 MB is refused), or as an upload to ``/upload/gmail/v1/users/me/drafts`` (``uploadType`` media or
  multipart, ``message/rfc822``, at most 35 MB). A ``threadId`` is honoured only when the message's ``In-Reply-To`` or
  ``References`` is set and its subject matches the thread's; otherwise Gmail starts a new conversation.
- ``drafts/send`` takes the draft's id in the body. Busy: 429 with ``Retry-After``, or 403 ``userRateLimitExceeded``.
- Labels: an unknown label id is refused (400); DRAFT and SENT are not put on or taken off by hand; a label name that
  exists is refused (409).
"""

from __future__ import annotations

import base64
import email
import email.policy
import html
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from tests.conftest import stands_for

JSON_MOST = 5 * 1024 * 1024
UPLOAD_MOST = 35 * 1024 * 1024
SYSTEM = ["INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "CATEGORY_PERSONAL",
          "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "CHAT"]
NOT_BY_HAND = {"DRAFT", "SENT", "CHAT"}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64url(data: str) -> bytes:
    if re.search(r"[^A-Za-z0-9_\-=]", data or ""):
        raise ValueError("Invalid value for ByteString: not base64url")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def plain_subject(subject: str) -> str:
    subject = str(subject or "").strip().lower()
    while True:
        cut = re.sub(r"^(re|fwd?|aw|sv|wg)\s*:\s*", "", subject)
        if cut == subject:
            return " ".join(subject.split())
        subject = cut


def compose(sender: str, to: str, subject: str, body: Optional[str], date: str, *, cc: str = "", charset: str = "utf-8",
            rich: Optional[str] = None, attachments=(), inline_images=(), message_id: str = "",
            in_reply_to: str = "", references: str = "", reply_to: str = "") -> bytes:
    """A message as a mail program writes one: text and/or HTML in ``charset``, pictures shown inline in a
    multipart/related, attachments (name, mime type, bytes) in a multipart/mixed."""
    text = MIMEText(body, "plain", charset) if body is not None else None
    page = MIMEText(rich, "html", charset) if rich is not None else None
    if page is not None and inline_images:
        related = MIMEMultipart("related")
        related.attach(page)
        for cid, name, data in inline_images:
            image = MIMEImage(data, "png")
            image["Content-ID"] = f"<{cid}>"
            image.add_header("Content-Disposition", "inline", filename=name)
            related.attach(image)
        page = related
    if text is not None and page is not None:
        content = MIMEMultipart("alternative")
        content.attach(text)
        content.attach(page)
    else:
        content = text or page
    if attachments:
        mixed = MIMEMultipart("mixed")
        mixed.attach(content)
        for name, mime, data in attachments:
            main, _, sub = mime.partition("/")
            part = MIMEText(data.decode(), sub) if main == "text" else MIMEApplication(data, sub)
            part.add_header("Content-Disposition", "attachment",
                            filename=name if name.isascii() else ("utf-8", "", name))
            mixed.attach(part)
        content = mixed
    content["From"] = sender
    content["To"] = to
    if cc:
        content["Cc"] = cc
    if reply_to:
        content["Reply-To"] = reply_to
    content["Subject"] = subject if subject.isascii() else Header(subject, "utf-8").encode()
    content["Date"] = date
    content["Message-ID"] = message_id or f"<{secrets.token_hex(6)}@mail.example>"
    if in_reply_to:
        content["In-Reply-To"] = in_reply_to
    if references:
        content["References"] = references
    return content.as_bytes()


class GmailStub:
    ACCOUNT = "demo@sidra.example"
    ALIAS = "sales@sidra.example"
    SIGNATURE = '<div dir="ltr"><b>Sidra Office Supplies</b><br>Dubai &amp; Abu Dhabi</div>'

    def __init__(self, account: str = ACCOUNT, token: str = "at-1"):
        self.account, self.token = account, token
        self.messages: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.drafts: dict[str, str] = {}
        self.labels: list[dict[str, Any]] = [{"id": l, "name": l, "type": "system"} for l in SYSTEM]
        self.history: list[dict[str, Any]] = []
        self.history_id = 1000
        self.history_floor = 0
        self.issued: dict[str, tuple[str, str]] = {}
        self.send_as = [
            {"sendAsEmail": account, "displayName": "Sidra Office", "isPrimary": True, "isDefault": True,
             "signature": self.SIGNATURE, "verificationStatus": "accepted"},
            {"sendAsEmail": self.ALIAS, "displayName": "Sidra Sales", "isPrimary": False, "isDefault": False,
             "signature": "", "treatAsAlias": True, "verificationStatus": "accepted"},
        ]
        self.sends: list[str] = []
        self.uploads: list[str] = []
        self.requests: list[tuple[str, str]] = []
        #: The answers the next requests get instead: 429 or "403" (userRateLimitExceeded), one each.
        self.busy: list[Any] = []
        self.drop_send = False
        self.encoded_headers = False
        #: Text parts longer than this come as an attachmentId, as Gmail does with long bodies.
        self.inline_text_most = 50_000
        self.token_calls = 0
        self._n = 0
        self.lock = threading.RLock()
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional mail --------------------------------------------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04x}"

    def record(self, **change: Any) -> int:
        self.history_id += 1
        self.history.append({"id": self.history_id, **change})
        return self.history_id

    def put(self, raw: bytes, thread_id: str, labels: list[str], internal_ms: Optional[int] = None) -> str:
        with self.lock:
            message_id = self.next_id("m")
            if internal_ms is None:
                try:
                    internal_ms = int(parsedate_to_datetime(email.message_from_bytes(raw)["Date"]).timestamp() * 1000)
                except (TypeError, ValueError):
                    internal_ms = int(time.time() * 1000)
            self.messages[message_id] = {"id": message_id, "threadId": thread_id, "labelIds": list(labels),
                                         "internalDate": str(internal_ms), "raw": raw}
            self.order.append(message_id)
            self.messages[message_id]["historyId"] = self.record(
                messagesAdded=[{"message": {"id": message_id, "threadId": thread_id, "labelIds": list(labels)}}])
            return message_id

    def add_message(self, thread_id: str, sender: str, to: str, subject: str, body: Optional[str], date: str,
                    attachments=None, labels=("INBOX", "UNREAD"), raw: Optional[bytes] = None, **kw: Any) -> str:
        """Mail arriving (or, from the account, already sent: SENT instead of INBOX)."""
        if sender == self.account and labels == ("INBOX", "UNREAD"):
            labels = ("SENT",)
        raw = raw or compose(sender, to, subject, body, date, attachments=attachments or (), **kw)
        return self.put(raw, thread_id, list(labels))

    def thread(self, thread_id: str) -> list[dict[str, Any]]:
        return [self.messages[m] for m in self.order if m in self.messages and self.messages[m]["threadId"] == thread_id]

    def parsed(self, message_id: str) -> email.message.EmailMessage:
        """The message parsed once (a message's bytes never change); read only."""
        held = self.messages[message_id]
        if held.get("_parsed") is None:
            held["_parsed"] = email.message_from_bytes(held["raw"], policy=email.policy.default)
        return held["_parsed"]

    def draft(self, draft_id: str) -> email.message.EmailMessage:
        return self.parsed(self.drafts[draft_id])

    def delete_thread(self, thread_id: str) -> None:
        with self.lock:
            for message in self.thread(thread_id):
                del self.messages[message["id"]]
                self.record(messagesDeleted=[{"message": {"id": message["id"], "threadId": thread_id}}])

    def expire_history(self) -> None:
        """Every history id older than the mailbox's current one is now too old: history.list answers 404 for it."""
        self.history_floor = self.history_id

    def label_id(self, name: str) -> str:
        return next(l["id"] for l in self.labels if l["name"].lower() == name.lower())

    def add_label(self, name: str) -> str:
        label = {"id": f"Label_{len(self.labels)}", "name": name, "type": "user"}
        self.labels.append(label)
        return label["id"]

    # -- a message as Gmail answers it ------------------------------------------------------------------------------
    def part_json(self, message_id: str, part: Any, part_id: str) -> dict[str, Any]:
        headers = [{"name": k, "value": (v if self.encoded_headers else str(v))} for k, v in (
            part._headers if self.encoded_headers else part.items())]
        out: dict[str, Any] = {"partId": part_id, "mimeType": part.get_content_type(),
                               "filename": part.get_filename() or "", "headers": headers}
        if part.is_multipart():
            out["body"] = {"size": 0}
            out["parts"] = [self.part_json(message_id, child, f"{part_id}.{n}" if part_id else str(n))
                            for n, child in enumerate(part.iter_parts())]
            return out
        data = part.get_payload(decode=True) or b""
        out["body"] = {"size": len(data)}
        if out["filename"] or part.get_content_maintype() != "text" or len(data) > self.inline_text_most:
            issued = "ANGjdJ" + secrets.token_urlsafe(48)
            self.issued[issued] = (message_id, part_id)
            out["body"]["attachmentId"] = issued
        else:
            out["body"]["data"] = b64url(data)
        return out

    def leaf(self, message_id: str, part_id: str) -> Any:
        part = self.parsed(message_id)
        for step in [s for s in part_id.split(".") if s != ""]:
            part = list(part.iter_parts())[int(step)]
        return part

    def snippet(self, message_id: str) -> str:
        parsed = self.parsed(message_id)
        found = parsed.get_body(("plain", "html"))
        text = found.get_content() if found is not None else ""
        if found is not None and found.get_content_subtype() == "html":
            text = re.sub(r"<[^>]+>", " ", text)
        return html.escape(" ".join(text.split())[:100])

    def message_json(self, message_id: str, fmt: str = "full", wanted: Optional[list[str]] = None) -> dict[str, Any]:
        held = self.messages[message_id]
        out = {k: held[k] for k in ("id", "threadId", "labelIds", "internalDate")}
        out.update(historyId=str(held["historyId"]), sizeEstimate=len(held["raw"]), snippet=self.snippet(message_id))
        if fmt == "raw":
            out["raw"] = b64url(held["raw"])
        elif fmt == "metadata":
            parsed = self.parsed(message_id)
            names = {w.lower() for w in wanted or []}
            out["payload"] = {"partId": "", "mimeType": parsed.get_content_type(), "headers": [
                {"name": k, "value": str(v)} for k, v in parsed.items() if not names or k.lower() in names]}
        elif fmt == "full":
            parsed = email.message_from_bytes(held["raw"], policy=email.policy.default)
            out["payload"] = self.part_json(message_id, parsed, "")
        return out

    # -- search --------------------------------------------------------------------------------------------------
    def text_of(self, message_id: str) -> str:
        words = []
        for part in self.parsed(message_id).walk():
            if part.get_content_maintype() == "text" and not part.get_filename():
                try:
                    words.append(part.get_content())
                except (LookupError, UnicodeError):
                    pass
        return " ".join(words).lower()

    def matches(self, message_id: str, query: str) -> bool:
        held, parsed = self.messages[message_id], self.parsed(message_id)
        labels = set(held["labelIds"])
        if held.get("_heads") is None:
            held["_heads"] = {k: str(parsed.get(k) or "").lower() for k in ("from", "to", "cc", "subject")}
        heads = held["_heads"]
        anywhere = False
        for negated, key, value in re.findall(r'(-?)(?:(\w+):)?("[^"]*"|\S+)', query):
            value = value.strip('"').lower()
            key = key.lower()
            if key == "in" and value in ("anywhere", "all"):
                anywhere = True
                continue
            if key in ("from", "to", "cc", "subject"):
                hit = value in heads[key]
            elif key == "in":
                hit = value.upper() in labels or (value == "drafts" and "DRAFT" in labels)
            elif key == "label":
                ids = {l["id"] for l in self.labels if l["name"].lower().replace(" ", "-") == value.replace(" ", "-")}
                hit = bool(ids & labels)
            elif key == "is":
                hit = {"unread": "UNREAD" in labels, "read": "UNREAD" not in labels,
                       "starred": "STARRED" in labels, "important": "IMPORTANT" in labels}.get(value, False)
            elif key == "has":
                hit = value == "attachment" and any(p.get_filename() for p in parsed.walk())
            elif key == "filename":
                hit = any(value in (p.get_filename() or "").lower() for p in parsed.walk())
            elif key in ("after", "before"):
                if "/" in value:
                    y, m, d = (int(x) for x in value.split("/"))
                    import datetime as _dt
                    bound = int(_dt.datetime(y, m, d, tzinfo=_dt.timezone.utc).timestamp())
                else:
                    bound = int(value)
                seconds = int(held["internalDate"]) // 1000
                hit = seconds > bound if key == "after" else seconds < bound
            elif key in ("newer_than", "older_than"):
                hit = True
            else:
                term = f"{key}:{value}" if key else value
                hit = term in heads["subject"] or term in heads["from"] or term in heads["to"] or \
                    term in self.text_of(message_id)
            if hit == bool(negated):
                return False
        return anywhere or not ({"SPAM", "TRASH"} & labels) or "in:trash" in query or "in:spam" in query

    # -- the server ------------------------------------------------------------------------------------------------
    def start(self) -> "GmailStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def setup(self):
                super().setup()
                # Head and body go out as two writes: without this, delayed ACKs cost 40 ms a request.
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def answer(self, status: int, body: Any = None, headers: Optional[dict] = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode("utf-8")
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                if body is not None:
                    self.send_header("Content-Type", "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(raw)))
                # Head and body in one write, so no proxy between holds the body back for an ACK.
                self._headers_buffer.append(b"\r\n" + raw)
                self.flush_headers()

            def refuse(self, status: int, message: str, reason: str = "", state: str = "") -> None:
                error = {"code": status, "message": message,
                         "status": state or {400: "INVALID_ARGUMENT", 404: "NOT_FOUND", 409: "ALREADY_EXISTS",
                                             413: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED"}.get(status, "UNKNOWN")}
                if reason:
                    error["errors"] = [{"message": message, "domain": "global", "reason": reason}]
                self.answer(status, {"error": error})

            def received(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def handle_one(self, method: str) -> None:
                url = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(url.query)
                body = self.received() if method in ("POST", "PUT", "PATCH") else b""
                if self.headers.get("Authorization") != f"Bearer {stub.token}":
                    return self.refuse(401, "Request had invalid authentication credentials.", "authError")
                stub.requests.append((method, url.path))
                if stub.busy:
                    kind = stub.busy.pop(0)
                    if kind == 429:
                        return self.answer(429, {"error": {"code": 429, "message": "Too many concurrent requests "
                                                           "for user.", "status": "RESOURCE_EXHAUSTED"}},
                                           {"Retry-After": "1"})
                    if kind == "scope":
                        return self.refuse(403, "Request had insufficient authentication scopes.",
                                           "insufficientPermissions", "PERMISSION_DENIED")
                    if kind == 500:
                        return self.refuse(500, "Backend Error", "backendError", "INTERNAL")
                    return self.refuse(403, "User-rate limit exceeded.", "userRateLimitExceeded", "PERMISSION_DENIED")
                with stub.lock:
                    if url.path.startswith("/upload/gmail/v1/users/me/"):
                        return stub.upload(self, url.path[len("/upload/gmail/v1/users/me/"):], query, body)
                    if not url.path.startswith("/gmail/v1/users/me/"):
                        return self.refuse(404, f"No route {url.path}")
                    stub.route(self, method, url.path[len("/gmail/v1/users/me/"):], query, body)

            def do_GET(self):  # noqa: N802
                self.handle_one("GET")

            def do_POST(self):  # noqa: N802
                self.handle_one("POST")

            def do_DELETE(self):  # noqa: N802
                self.handle_one("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def route(self, h: Any, method: str, path: str, query: dict[str, list[str]], body: bytes) -> None:
        one = lambda name, default="": (query.get(name) or [default])[0]  # noqa: E731
        parts = [urllib.parse.unquote(p) for p in path.split("/")]
        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                return h.refuse(400, "Invalid JSON payload received.")
        if method == "GET" and parts == ["profile"]:
            return h.answer(200, {"emailAddress": self.account, "messagesTotal": len(self.messages),
                                  "threadsTotal": len({m["threadId"] for m in self.messages.values()}),
                                  "historyId": str(self.history_id)})
        if method == "GET" and parts == ["settings", "sendAs"]:
            return h.answer(200, {"sendAs": self.send_as})
        if method == "GET" and parts == ["labels"]:
            return h.answer(200, {"labels": self.labels})
        if method == "POST" and parts == ["labels"]:
            name = str(payload.get("name") or "").strip()
            if not name:
                return h.refuse(400, "Invalid label name")
            if any(l["name"].lower() == name.lower() for l in self.labels):
                return h.refuse(409, "Label name exists or conflicts")
            label_id = self.add_label(name)
            return h.answer(200, {"id": label_id, "name": name, "type": "user"})
        if method == "GET" and parts == ["messages"]:
            return self.listing(h, query)
        if method == "GET" and parts == ["history"]:
            return self.history_list(h, query)
        if parts[0] == "messages" and len(parts) >= 2 and parts[1] == "batchModify" and method == "POST":
            ids = [str(i) for i in payload.get("ids") or []]
            if not ids or len(ids) > 1000:
                return h.refuse(400, "ids must hold 1 to 1000 message ids.")
            wrong = self.label_problem(payload)
            if wrong:
                return h.refuse(400, wrong)
            for message_id in ids:
                if message_id in self.messages:
                    self.relabel(message_id, payload)
            return h.answer(204)
        if parts[0] == "messages" and len(parts) >= 2:
            message_id = parts[1]
            if message_id not in self.messages:
                return h.refuse(404, "Requested entity was not found.", "notFound")
            if method == "GET" and len(parts) == 2:
                fmt = one("format", "full")
                if fmt not in ("full", "metadata", "minimal", "raw"):
                    return h.refuse(400, f"Invalid format {fmt}")
                return h.answer(200, self.message_json(message_id, fmt, query.get("metadataHeaders")))
            if method == "GET" and len(parts) == 4 and parts[2] == "attachments":
                issued = self.issued.get(parts[3])
                if issued is None or issued[0] != message_id:
                    return h.refuse(400, "Invalid attachment token")
                data = self.leaf(message_id, issued[1]).get_payload(decode=True) or b""
                return h.answer(200, {"attachmentId": parts[3], "size": len(data), "data": b64url(data)})
            if method == "POST" and len(parts) == 3 and parts[2] == "modify":
                wrong = self.label_problem(payload)
                if wrong:
                    return h.refuse(400, wrong)
                self.relabel(message_id, payload)
                return h.answer(200, self.message_json(message_id, "minimal"))
            if method == "POST" and len(parts) == 3 and parts[2] in ("trash", "untrash"):
                change = {"addLabelIds": ["TRASH"]} if parts[2] == "trash" else {"removeLabelIds": ["TRASH"]}
                self.relabel(message_id, change)
                return h.answer(200, self.message_json(message_id, "minimal"))
        if parts[0] == "threads" and len(parts) >= 2:
            messages = self.thread(parts[1])
            if not messages:
                return h.refuse(404, "Requested entity was not found.", "notFound")
            if method == "GET" and len(parts) == 2:
                fmt = one("format", "full")
                return h.answer(200, {"id": parts[1], "historyId": str(self.history_id), "messages": [
                    self.message_json(m["id"], fmt, query.get("metadataHeaders")) for m in messages]})
            if method == "POST" and len(parts) == 3 and parts[2] == "modify":
                wrong = self.label_problem(payload)
                if wrong:
                    return h.refuse(400, wrong)
                for message in messages:
                    self.relabel(message["id"], payload)
                return h.answer(200, {"id": parts[1]})
            if method == "POST" and len(parts) == 3 and parts[2] in ("trash", "untrash"):
                change = {"addLabelIds": ["TRASH"]} if parts[2] == "trash" else {"removeLabelIds": ["TRASH"]}
                for message in messages:
                    self.relabel(message["id"], change)
                return h.answer(200, {"id": parts[1]})
        if parts == ["drafts"] and method == "POST":
            if len(body) > JSON_MOST:
                return h.refuse(413, f"Request payload size exceeds the limit: {JSON_MOST} bytes.")
            message = payload.get("message") or {}
            try:
                raw = unb64url(str(message.get("raw") or ""))
            except ValueError as wrong:
                return h.refuse(400, str(wrong))
            return self.make_draft(h, raw, str(message.get("threadId") or ""))
        if parts == ["drafts", "send"] and method == "POST":
            return self.send(h, str(payload.get("id") or ""))
        if parts[0] == "drafts" and len(parts) == 2 and method == "DELETE":
            message_id = self.drafts.pop(parts[1], None)
            if message_id is None:
                return h.refuse(404, "Requested entity was not found.", "notFound")
            self.messages.pop(message_id, None)
            return h.answer(204)
        h.refuse(404, f"No route {method} {path}")

    def upload(self, h: Any, path: str, query: dict[str, list[str]], body: bytes) -> None:
        if path != "drafts":
            return h.refuse(404, f"No upload route {path}")
        kind = (query.get("uploadType") or [""])[0]
        self.uploads.append(kind)
        if len(body) > UPLOAD_MOST + 64 * 1024:
            return h.refuse(413, f"Maximum upload size is {UPLOAD_MOST} bytes.")
        content_type = h.headers.get("Content-Type") or ""
        if kind == "media":
            if not content_type.startswith("message/"):
                return h.refuse(400, "Media type must be message/*.")
            return self.make_draft(h, body, "")
        if kind != "multipart":
            return h.refuse(400, f"Invalid upload type {kind!r}")
        found = re.search(r'boundary="?([^";]+)"?', content_type)
        if not content_type.startswith("multipart/related") or not found:
            return h.refuse(400, "A multipart upload is multipart/related with a boundary.")
        mark = b"--" + found.group(1).encode()
        pieces = body.split(mark)
        if len(pieces) != 4 or not pieces[3].startswith(b"--"):
            return h.refuse(400, "A multipart upload holds the metadata, then the media.")
        sections = []
        for piece in pieces[1:3]:
            head, _, content = piece.lstrip(b"\r\n").partition(b"\r\n\r\n")
            sections.append((head.decode("latin-1").lower(), content[:-2] if content.endswith(b"\r\n") else content))
        (meta_head, meta), (media_head, raw) = sections
        if "application/json" not in meta_head or "message/rfc822" not in media_head:
            return h.refuse(400, "The metadata must be application/json and the media message/rfc822.")
        if len(raw) > UPLOAD_MOST:
            return h.refuse(413, f"Maximum upload size is {UPLOAD_MOST} bytes.")
        thread_id = str((json.loads(meta or b"{}").get("message") or {}).get("threadId") or "")
        return self.make_draft(h, raw, thread_id)

    def make_draft(self, h: Any, raw: bytes, thread_id: str) -> None:
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        if not parsed["From"] and not parsed["To"]:
            return h.refuse(400, "Invalid raw message.")
        aliases = {a["sendAsEmail"].lower() for a in self.send_as}
        if getaddresses([str(parsed["From"] or "")])[0][1].lower() not in aliases:
            return h.refuse(400, "Invalid From: not a send-as address of the account.")
        threaded = self.thread(thread_id) if thread_id else []
        honoured = threaded and (parsed["In-Reply-To"] or parsed["References"]) and plain_subject(
            str(parsed["Subject"] or "")) == plain_subject(str(email.message_from_bytes(
                threaded[0]["raw"], policy=email.policy.default)["Subject"] or ""))
        thread_id = thread_id if honoured else self.next_id("t")
        message_id = self.put(raw, thread_id, ["DRAFT"], int(time.time() * 1000))
        draft_id = self.next_id("r")
        self.drafts[draft_id] = message_id
        h.answer(200, {"id": draft_id, "message": {"id": message_id, "threadId": thread_id, "labelIds": ["DRAFT"]}})

    def send(self, h: Any, draft_id: str) -> None:
        self.sends.append(draft_id)
        message_id = self.drafts.get(draft_id)
        if message_id is None:
            return h.refuse(404, "Requested entity was not found.", "notFound")
        parsed = self.parsed(message_id)
        if not any(parsed[k] for k in ("To", "Cc", "Bcc")):
            return h.refuse(400, "Recipient address required", "invalidArgument")
        if self.drop_send:
            # Received, and no answer ever comes.
            h.connection.shutdown(socket.SHUT_RDWR)
            h.close_connection = True
            return
        held = self.messages.pop(self.drafts.pop(draft_id))
        sent = self.put(held["raw"], held["threadId"], ["SENT"], int(time.time() * 1000))
        h.answer(200, {"id": sent, "threadId": held["threadId"], "labelIds": ["SENT"]})

    def label_problem(self, change: dict[str, Any]) -> str:
        known = {l["id"] for l in self.labels}
        for label in list(change.get("addLabelIds") or []) + list(change.get("removeLabelIds") or []):
            if label not in known:
                return f"Invalid label: {label}"
            if label in NOT_BY_HAND:
                return f"Invalid label: {label} cannot be changed by hand"
        return ""

    def relabel(self, message_id: str, change: dict[str, Any]) -> None:
        held = self.messages[message_id]
        added = [l for l in change.get("addLabelIds") or [] if l not in held["labelIds"]]
        removed = [l for l in change.get("removeLabelIds") or [] if l in held["labelIds"]]
        held["labelIds"] = [l for l in held["labelIds"] if l not in removed] + added
        stub = {"message": {"id": message_id, "threadId": held["threadId"]}}
        if added:
            held["historyId"] = self.record(labelsAdded=[{**stub, "labelIds": added}])
        if removed:
            held["historyId"] = self.record(labelsRemoved=[{**stub, "labelIds": removed}])

    def listing(self, h: Any, query: dict[str, list[str]]) -> None:
        most = int((query.get("maxResults") or ["100"])[0])
        if not 1 <= most <= 500:
            return h.refuse(400, "maxResults must be at most 500.")
        q = (query.get("q") or [""])[0]
        spam_trash = (query.get("includeSpamTrash") or ["false"])[0] == "true"
        position = {m: n for n, m in enumerate(self.order)}
        newest = sorted((m for m in self.order if m in self.messages),
                        key=lambda m: (int(self.messages[m]["internalDate"]), position[m]), reverse=True)
        hits = [m for m in newest if self.matches(m, q + (" in:anywhere" if spam_trash else ""))]
        start = int((query.get("pageToken") or ["0"])[0] or 0)
        page = hits[start:start + most]
        answer: dict[str, Any] = {"resultSizeEstimate": len(hits)}
        if page:
            answer["messages"] = [{"id": m, "threadId": self.messages[m]["threadId"]} for m in page]
        if start + most < len(hits):
            answer["nextPageToken"] = str(start + most)
        h.answer(200, answer)

    def history_list(self, h: Any, query: dict[str, list[str]]) -> None:
        given = (query.get("startHistoryId") or [""])[0]
        if not given.isdigit():
            return h.refuse(400, "startHistoryId is required.")
        start = int(given)
        if start < self.history_floor or start > self.history_id:
            return h.refuse(404, "Requested entity was not found.", "notFound")
        most = int((query.get("maxResults") or ["100"])[0])
        if not 1 <= most <= 500:
            return h.refuse(400, "maxResults must be at most 500.")
        types = set(query.get("historyTypes") or [])
        label = (query.get("labelId") or [""])[0]
        found = []
        for change in self.history:
            if change["id"] <= start:
                continue
            kept = {"id": str(change["id"])}
            for kind, entries in change.items():
                if kind == "id" or (types and _kind(kind) not in types):
                    continue
                if label:
                    entries = [e for e in entries if label in (e["message"].get("labelIds") or
                                                               e.get("labelIds") or [])]
                if entries:
                    kept[kind] = [{"message": {"id": e["message"]["id"], "threadId": e["message"]["threadId"]}}
                                  for e in entries]
            if len(kept) > 1:
                found.append(kept)
        offset = int((query.get("pageToken") or ["0"])[0] or 0)
        page = found[offset:offset + most]
        answer: dict[str, Any] = {"historyId": str(self.history_id)}
        if page:
            answer["history"] = page
        if offset + most < len(found):
            answer["nextPageToken"] = str(offset + most)
        h.answer(200, answer)

    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("gmail.googleapis.com", self._server)

    def secret(self, access_token: Optional[str] = None) -> dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": self.account, "access_token": access_token or self.token,
                "api_base_url": self.url + "/gmail/v1/users/me"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def _kind(change: str) -> str:
    return {"messagesAdded": "messageAdded", "messagesDeleted": "messageDeleted", "labelsAdded": "labelAdded",
            "labelsRemoved": "labelRemoved"}.get(change, change)


def rfc_date(seconds: int) -> str:
    import datetime as _dt
    return format_datetime(_dt.datetime.fromtimestamp(seconds, _dt.timezone.utc))
