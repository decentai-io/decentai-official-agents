"""A loopback Slack Web API: the methods the Slack agent calls, over real HTTP, holding a fictional Sidra Office
Supplies workspace.

It behaves the way Slack does where the agent depends on it: every answer is HTTP 200 with an ``ok`` envelope,
failures included (a revoked token is ``{"ok": false, "error": "token_revoked"}``); only a rate limit is HTTP 429
with Retry-After. Lists page by ``response_metadata.next_cursor``; search pages by page number. History comes newest
first and ``oldest`` and ``latest`` are exclusive; a thread's replies come parent first, oldest first. Message ts
values are strings of seconds with six decimals, unique within a conversation.

Held to Slack's own rules where the agent could get them wrong:

- a write method answers only POST, and a JSON body is taken only by write methods (a read method given one sees no
  arguments);
- a token missing a scope the method needs is ``missing_scope``, naming what is ``needed``;
- history and replies give at most 999 a page, search at most 100, users.list at most 1000;
- files are served from files.slack.com, to the token only;
- ``restricted`` plays Slack's 2025 limits for an app distributed outside its Marketplace: history and replies give
  at most 15 a page and answer one request a minute, then 429 with Retry-After 60;
- ``unindexed`` holds ts values search has not indexed yet (search lags behind what was said).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from tests.conftest import stands_for

TEAM_URL = "https://sidra-office.slack.com/"
ME = "U0DEMO"
DANA = "U0DANA"
OMAR = "U0OMAR"
PRIYA = "U0PRIYA"
PEOPLE = {
    ME: ("demo", "Demo", "demo@sidra.example"),
    DANA: ("dana", "Dana Harbour", "dana@sidra.example"),
    OMAR: ("omar", "Omar Nasser", "omar@sidra.example"),
    PRIYA: ("priya", "Priya Menon", "priya@sidra.example"),
}
GENERAL = "C0GENERAL"
SHOWROOM = "C0SHOWROOM"
FINANCE = "G0FINANCE"
DM_DANA = "D0DANA"
DM_OMAR = "D0OMAR"
GROUP = "G0MPIMOPS"

GOOD_TOKEN = "xoxp-demo"
REVOKED_TOKEN = "xoxp-revoked"
#: A connection made through 1.0.0: without the scopes 1.1.0 added.
OLD_TOKEN = "xoxp-old"
ADDED_SCOPES = {"files:read", "reactions:write", "im:write"}
#: The scope each method needs where the agent's tokens may differ.
NEEDS = {"files.info": "files:read", "reactions.add": "reactions:write", "reactions.remove": "reactions:write",
         "conversations.open": "im:write", "users.lookupByEmail": "users:read.email"}
WRITES = {"chat.postMessage", "conversations.open", "reactions.add", "reactions.remove"}


class SlackStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}
        self.members: Dict[str, List[str]] = {}
        self.posts: List[Dict[str, Any]] = []
        self.calls: List[str] = []
        self.drop_send = False
        self.rate_limit = 0          # answer this many calls with 429 ...
        self.rate_wait = "30"        # ... asking to wait this many seconds
        self.rate_only = ""          # ... of this method alone, when named
        self.restricted = False      # Slack's 2025 limits for an app outside the Marketplace
        self._last_history = 0.0
        self.unindexed: set = set()
        self.gone: set = set()       # listed, but its history is not there any more
        self.reactions: Dict[tuple, List[str]] = {}
        self.files: Dict[str, Dict[str, Any]] = {}
        self.downloads: List[str] = []
        self.users: Dict[str, Dict[str, Any]] = {}
        for user_id, (handle, name, email) in PEOPLE.items():
            self.add_user(user_id, handle, name, email)
        self._clock = Decimal(int(time.time()) - 3600)
        self._server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        self.add_conversation(GENERAL, "general", is_channel=True)
        self.add_conversation(SHOWROOM, "showroom", is_channel=True)
        self.add_conversation(FINANCE, "finance", is_private=True, is_group=True)
        self.add_conversation(DM_DANA, DANA, is_im=True, user=DANA)
        self.add_conversation(DM_OMAR, OMAR, is_im=True, user=OMAR)
        self.add_conversation(GROUP, "mpdm-demo--omar--priya-1", is_mpim=True, members=[ME, OMAR, PRIYA])
        chairs = self.add_message(SHOWROOM, ME, "Has Northlight confirmed the chair delivery?")
        self.add_message(SHOWROOM, OMAR, "Not yet, chasing them today.", thread_ts=chairs)
        self.add_message(SHOWROOM, DANA, "Harbourline want the layout signed off &amp; the drawings by Thursday.")
        self.add_message(GENERAL, PRIYA, "Office closed on the 24th for the move.")
        self.add_message(GENERAL, PRIYA, "", subtype="channel_join")
        self.add_message(DM_DANA, DANA, "Can you send the showroom drawings?")
        self.add_message(GROUP, OMAR, f"<@{ME}> can you check the Cloudledger export?")
        self.chairs_ts = chairs

    # -- fictional data ------------------------------------------------------------------------------------------------
    def add_user(self, user_id: str, handle: str, name: str, email: str, **flags: Any) -> None:
        self.users[user_id] = {"id": user_id, "name": handle, "real_name": name, "deleted": False, "is_bot": False,
                               "profile": {"display_name": name, "real_name": name, "email": email}, **flags}

    def add_conversation(self, channel: str, name: str, members: Optional[List[str]] = None, **flags: Any) -> None:
        self.conversations[channel] = {
            "id": channel, "name": name, "created": 1700000000, "updated": int(self._clock * 1000),
            "is_channel": False, "is_group": False, "is_im": False, "is_mpim": False, "is_private": False, **flags}
        self.messages.setdefault(channel, [])
        self.members[channel] = members or ([ME, flags["user"]] if flags.get("user") else list(PEOPLE))

    def add_file(self, file_id: str, name: str, content: bytes, size: int = 0, **extra: Any) -> Dict[str, Any]:
        """A file Slack keeps; ``size`` may claim more than ``content`` holds, as a large one would."""
        self.files[file_id] = {"id": file_id, "name": name, "title": name, "mimetype": "application/octet-stream",
                               "size": size or len(content), "content": content,
                               "permalink": f"{TEAM_URL}files/{ME}/{file_id}/x", **extra}
        return self.files[file_id]

    def add_message(self, channel: str, user: str, text: str, thread_ts: str = "", subtype: str = "",
                    now: bool = False, files: Optional[List[str]] = None) -> str:
        with self._lock:
            # Seeded messages a second apart an hour ago; "now" messages on the real clock, so a watch started now
            # sees them, and never earlier than anything before.
            if now:
                # A millisecond apart at least: Slack's "updated" counts milliseconds.
                self._clock = max(self._clock + Decimal("0.001000"), Decimal(f"{time.time():.6f}"))
            else:
                self._clock += Decimal("1.000100")
            ts = f"{self._clock:.6f}"
        message: Dict[str, Any] = {"type": "message", "user": user, "text": text, "ts": ts}
        if subtype:
            message["subtype"] = subtype
        if files:
            message["files"] = [{k: v for k, v in self.files[f].items() if k != "content"} for f in files]
        if thread_ts:
            message["thread_ts"] = thread_ts
            parent = self.find(channel, thread_ts)
            parent["thread_ts"] = thread_ts
            parent["reply_count"] = int(parent.get("reply_count") or 0) + 1
        self.messages[channel].append(message)
        self.conversations[channel]["updated"] = int(Decimal(ts) * 1000)
        return ts

    def say(self, channel: str, user: str, text: str, thread_ts: str = "", files: Optional[List[str]] = None) -> str:
        """Someone writes now, in real time."""
        return self.add_message(channel, user, text, thread_ts=thread_ts, now=True, files=files)

    def find(self, channel: str, ts: str) -> Dict[str, Any]:
        return next(m for m in self.messages[channel] if m["ts"] == ts)

    def minute_passed(self) -> None:
        """As if a minute went by: a restricted app may read history once more."""
        self._last_history = 0.0

    # -- the server ----------------------------------------------------------------------------------------------------
    def start(self) -> "SlackStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, body: Any, status: int = 200, headers=None) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _fail(self, error: str, **extra: Any) -> None:
                self._json({"ok": False, "error": error, **extra})

            def _args(self, method: str) -> Dict[str, Any]:
                url = urllib.parse.urlparse(self.path)
                args: Dict[str, Any] = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    raw = self.rfile.read(length)
                    if "json" in (self.headers.get("Content-Type") or ""):
                        # Slack reads a JSON body for write methods only.
                        if method in WRITES:
                            args.update(json.loads(raw or b"{}"))
                    else:
                        args.update({k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()})
                return args

            def do_GET(self):  # noqa: N802
                self._serve()

            def do_POST(self):  # noqa: N802
                self._serve()

            def _serve(self) -> None:
                path = urllib.parse.urlparse(self.path).path
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if path.startswith("/files-pri/"):
                    return self._download(path, token)
                method = path.rsplit("/", 1)[-1]
                args = self._args(method)
                stub.calls.append(method)
                if method in WRITES and self.command != "POST":
                    return self._fail("invalid_request")
                if stub.rate_limit > 0 and stub.rate_only in ("", method):
                    stub.rate_limit -= 1
                    return self._json({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": stub.rate_wait})
                if stub.restricted and method in ("conversations.history", "conversations.replies"):
                    now = time.monotonic()
                    if now - stub._last_history < 60:
                        return self._json({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "60"})
                    stub._last_history = now
                if not token:
                    return self._fail("not_authed")
                if token == REVOKED_TOKEN:
                    return self._fail("token_revoked")
                if token not in (GOOD_TOKEN, OLD_TOKEN):
                    return self._fail("invalid_auth")
                if token == OLD_TOKEN and NEEDS.get(method) in ADDED_SCOPES:
                    return self._fail("missing_scope", needed=NEEDS[method], provided="channels:read,chat:write")
                handler = getattr(self, "m_" + method.replace(".", "_"), None)
                if handler is None:
                    return self._fail("unknown_method")
                handler(args)

            def _download(self, path, token):
                file_id = path.split("/")[2]
                found = stub.files.get(file_id)
                if found is None or token not in (GOOD_TOKEN, OLD_TOKEN):
                    # Slack sends a request without the token to its sign-in page.
                    self.send_response(404 if found is None else 302)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                stub.downloads.append(file_id)
                self.send_response(200)
                self.send_header("Content-Type", found["mimetype"])
                self.send_header("Content-Length", str(len(found["content"])))
                self.end_headers()
                self.wfile.write(found["content"])

            # -- methods -----------------------------------------------------------------------------------------------
            def m_auth_test(self, args):
                self._json({"ok": True, "url": TEAM_URL, "team": "Sidra Office Supplies", "user": "demo",
                            "team_id": "T0SIDRA", "user_id": ME})

            def _page(self, rows, args, key, most=1000, **extra):
                limit = min(int(args.get("limit") or 100), most)
                start = int(args.get("cursor") or 0)
                chunk = rows[start:start + limit]
                cursor = str(start + limit) if start + limit < len(rows) else ""
                self._json({"ok": True, key: chunk, "has_more": bool(cursor),
                            "response_metadata": {"next_cursor": cursor}, **extra})

            def m_users_conversations(self, args):
                wanted = set(str(args.get("types") or "public_channel").split(","))

                def kind(c):
                    if c["is_im"]:
                        return "im"
                    if c["is_mpim"]:
                        return "mpim"
                    return "private_channel" if c["is_private"] else "public_channel"
                rows = [c for c in stub.conversations.values() if kind(c) in wanted and ME in stub.members[c["id"]]]
                self._page(rows, args, "channels", most=999)

            def m_conversations_info(self, args):
                conversation = stub.conversations.get(str(args.get("channel")))
                if conversation is None:
                    return self._fail("channel_not_found")
                self._json({"ok": True, "channel": conversation})

            def m_conversations_members(self, args):
                if args.get("channel") not in stub.conversations:
                    return self._fail("channel_not_found")
                self._page(stub.members[args["channel"]], args, "members")

            def m_conversations_history(self, args):
                channel = str(args.get("channel"))
                if channel not in stub.conversations or channel in stub.gone:
                    return self._fail("channel_not_found")
                oldest = Decimal(str(args.get("oldest") or "0"))
                latest = Decimal(str(args.get("latest") or "99999999999"))
                rows = [m for m in stub.messages[channel] if oldest < Decimal(m["ts"]) < latest
                        and (not m.get("thread_ts") or m["thread_ts"] == m["ts"])]
                rows.sort(key=lambda m: Decimal(m["ts"]), reverse=True)
                self._page(rows, args, "messages", most=15 if stub.restricted else 999)

            def m_conversations_replies(self, args):
                channel = str(args.get("channel"))
                if channel not in stub.conversations:
                    return self._fail("channel_not_found")
                ts = str(args.get("ts"))
                target = next((m for m in stub.messages[channel] if m["ts"] == ts), None)
                if target is None:
                    return self._fail("thread_not_found")
                root = target.get("thread_ts") or ts
                if root != ts:
                    # Slack answers a reply's ts with that reply alone.
                    return self._json({"ok": True, "messages": [target], "has_more": False})
                rows = [m for m in stub.messages[channel] if m["ts"] == ts or m.get("thread_ts") == ts]
                rows.sort(key=lambda m: Decimal(m["ts"]))
                self._page(rows, args, "messages", most=15 if stub.restricted else 999)

            def m_search_messages(self, args):
                query = str(args.get("query") or "").lower()
                count = int(args.get("count") or 20)
                page = int(args.get("page") or 1)
                if count > 100 or page > 100:
                    return self._fail("invalid_arguments")
                matches = []
                for channel, messages in stub.messages.items():
                    if ME not in stub.members[channel]:
                        continue
                    conversation = stub.conversations[channel]
                    for m in messages:
                        if m.get("subtype") or query not in m["text"].lower() or m["ts"] in stub.unindexed:
                            continue
                        link = f"{TEAM_URL}archives/{channel}/p{m['ts'].replace('.', '')}"
                        if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
                            link += f"?thread_ts={m['thread_ts']}&cid={channel}"
                        matches.append({
                            "type": "message", "user": m["user"], "username": stub.users[m["user"]]["name"],
                            "text": m["text"], "ts": m["ts"], "permalink": link,
                            "channel": {"id": channel, "name": conversation["name"], "is_im": conversation["is_im"],
                                        "is_mpim": conversation["is_mpim"],
                                        "is_private": conversation["is_private"]}})
                matches.sort(key=lambda m: Decimal(m["ts"]), reverse=True)
                pages = max(1, -(-len(matches) // count))
                chunk = matches[(page - 1) * count: page * count]
                self._json({"ok": True, "query": args.get("query"), "messages": {
                    "total": len(matches), "matches": chunk,
                    "paging": {"count": count, "total": len(matches), "page": page, "pages": pages}}})

            def m_users_info(self, args):
                user = stub.users.get(str(args.get("user")))
                if user is None:
                    return self._fail("user_not_found")
                self._json({"ok": True, "user": user})

            def m_users_lookupByEmail(self, args):  # noqa: N802
                wanted = str(args.get("email") or "").lower()
                user = next((u for u in stub.users.values() if u["profile"]["email"].lower() == wanted), None)
                if user is None:
                    return self._fail("users_not_found")
                self._json({"ok": True, "user": user})

            def m_users_list(self, args):
                if int(args.get("limit") or 0) > 1000:
                    return self._fail("invalid_limit")
                self._page(list(stub.users.values()), args, "members")

            def m_conversations_open(self, args):
                users = [u for u in str(args.get("users") or "").split(",") if u]
                if len(users) != 1 or users[0] not in stub.users:
                    return self._fail("user_not_found")
                user = users[0]
                found = next((c for c in stub.conversations.values() if c["is_im"] and c.get("user") == user), None)
                if found is None:
                    channel = "D0" + user[2:]
                    stub.add_conversation(channel, user, is_im=True, user=user)
                    found = stub.conversations[channel]
                self._json({"ok": True, "channel": found if args.get("return_im") else {"id": found["id"]}})

            def _reacted(self, args, add):
                channel, ts = str(args.get("channel")), str(args.get("timestamp"))
                if channel not in stub.conversations:
                    return self._fail("channel_not_found")
                if not any(m["ts"] == ts for m in stub.messages[channel]):
                    return self._fail("message_not_found")
                kept = stub.reactions.setdefault((channel, ts), [])
                name = str(args.get("name") or "")
                if add and name in kept:
                    return self._fail("already_reacted")
                if not add and name not in kept:
                    return self._fail("no_reaction")
                kept.append(name) if add else kept.remove(name)
                self._json({"ok": True})

            def m_reactions_add(self, args):
                self._reacted(args, True)

            def m_reactions_remove(self, args):
                self._reacted(args, False)

            def m_files_info(self, args):
                found = stub.files.get(str(args.get("file")))
                if found is None:
                    return self._fail("file_not_found")
                shown = {k: v for k, v in found.items() if k != "content"}
                shown.setdefault("url_private_download", f"{stub.files_url}/files-pri/{found['id']}/download")
                self._json({"ok": True, "file": shown})

            def m_chat_postMessage(self, args):  # noqa: N802
                channel = str(args.get("channel"))
                if channel not in stub.conversations:
                    return self._fail("channel_not_found")
                if not str(args.get("text") or "").strip():
                    return self._fail("no_text")
                stub.posts.append(dict(args))
                ts = stub.add_message(channel, ME, str(args.get("text")), thread_ts=str(args.get("thread_ts") or ""),
                                      now=True)
                if stub.drop_send:
                    # The message was posted; no answer ever comes.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self._json({"ok": True, "channel": channel, "ts": ts, "message": stub.find(channel, ts)})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("slack.com", self._server)

    @property
    def files_url(self) -> str:
        assert self._server is not None
        return stands_for("files.slack.com", self._server)

    def secret(self, access_token: str = GOOD_TOKEN, account: str = "demo") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": account, "access_token": access_token, "api_base_url": self.url + "/api"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
