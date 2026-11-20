"""The Slack Web API over the connected account, on the shared client (``standard.http``).

- **Slack's envelope.** Slack answers most failures with HTTP 200 and ``{"ok": false, "error": "…"}``, so every
  answer's ``ok`` is read, and its ``error`` becomes one of the shared kinds: ``auth`` (reconnect), ``scope`` (the
  account was connected without a permission this needs: reconnect it), ``not_found``, ``denied``, ``invalid``,
  ``rate``, ``http``.
- **Rate limits.** A 429 is waited out as its ``Retry-After`` says, within the shared client's budget; a longer wait
  is ``rate`` at once. Slack's 2025 terms put ``conversations.history`` and ``conversations.replies`` at one request
  a minute and 15 messages a page for apps distributed outside its Marketplace (an organization's own app, installed
  in its own workspace, keeps 50+ a minute and 999 a page). Every reader here asks for a full page and takes what
  Slack gives, and the watch stops reading direct messages for the run at the first ``rate``, keeping its place.
- **Reads are GET with query parameters; writes are POST with a JSON body** (Slack takes JSON only for writes).
- Message ``ts`` values are Slack's ids as well as its clock: passed on exactly as Slack wrote them, compared as
  exact decimals.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from standard.http import FILE_MOST, Http, HttpError, host_of, human_size

API_BASE_URL = "https://slack.com/api"
FILES_HOST = "files.slack.com"
TEXT_LIMIT = 1500
#: What one history or replies request asks for. Slack gives at most 15 to an app distributed outside its
#: Marketplace, and recommends no more than 200 to everyone else.
PAGE = 200

AUTH_ERRORS = {"invalid_auth", "token_revoked", "account_inactive", "not_authed", "token_expired",
               "no_permission_to_authorize", "two_factor_setup_required"}
NOT_FOUND_ERRORS = {"channel_not_found", "thread_not_found", "user_not_found", "users_not_found",
                    "message_not_found", "file_not_found", "file_deleted", "no_item_specified"}
DENIED_ERRORS = {"not_in_channel", "is_archived", "channel_is_archived", "restricted_action",
                 "restricted_action_read_only_channel", "restricted_action_thread_only_channel",
                 "restricted_action_non_threadable_channel", "cant_invite_self", "user_is_restricted",
                 "access_denied", "ekm_access_denied", "team_access_not_granted", "user_disabled",
                 "not_allowed_token_type", "method_deprecated", "too_many_reactions", "too_many_emoji"}
INVALID_ERRORS = {"invalid_arguments", "invalid_ts_latest", "invalid_ts_oldest", "invalid_cursor", "msg_too_long",
                  "no_text", "invalid_name", "invalid_blocks", "too_many_users", "invalid_limit"}
# Housekeeping in a conversation, not something anyone said.
NOTICE_SUBTYPES = {"channel_join", "channel_leave", "group_join", "group_leave", "channel_topic",
                   "channel_purpose", "channel_name", "channel_archive", "channel_unarchive"}


class SlackClient(Http):
    SERVICE = "Slack"
    BASE = API_BASE_URL

    def __init__(self, secret: dict[str, Any]):
        # A connected account: the platform ran the sign-in and holds a long-lived user token (rotation is off).
        super().__init__(token=str(secret.get("access_token") or ""), api_base_url=secret.get("api_base_url"))
        self.account = str(secret.get("account") or "")
        self._me: dict[str, Any] | None = None

    # -- one method ------------------------------------------------------------------------------------------------
    async def call(self, method: str, params: dict[str, Any] | None = None, *, body: dict[str, Any] | None = None,
                   write: bool = False) -> dict[str, Any]:
        if write:
            answer = await self.post(method, json=body or {})
        else:
            answer = await self.get(method, params=params or {})
        if isinstance(answer, dict) and answer.get("ok"):
            return answer
        raise self.failed(method, answer if isinstance(answer, dict) else {})

    @staticmethod
    def failed(method: str, answer: dict[str, Any]) -> HttpError:
        error = str(answer.get("error") or "unknown_error")
        if error in AUTH_ERRORS:
            return HttpError("auth", f"Slack no longer accepts this connection ({error}): reconnect the account from "
                                     f"the agent's Credentials.")
        if error == "missing_scope":
            needed = str(answer.get("needed") or "a permission")
            return HttpError("scope", f"Slack refused {method}: the account was connected without {needed}. "
                                      f"Reconnect it from the agent's Credentials to grant it.")
        if error in NOT_FOUND_ERRORS:
            return HttpError("not_found", f"Slack has no such conversation, message, file or person ({error}).")
        if error in DENIED_ERRORS:
            return HttpError("denied", f"Slack refused {method}: {error}.")
        if error in INVALID_ERRORS:
            return HttpError("invalid", f"Slack refused {method}: {error}.")
        if error == "ratelimited":
            return HttpError("rate", f"Slack is rate limiting {method}: try again in a minute.")
        return HttpError("http", f"Slack refused {method}: {error}.")

    # -- reads -----------------------------------------------------------------------------------------------------
    async def me(self) -> dict[str, Any]:
        """auth.test, once per client: who the token is, and the workspace address permalinks are built from."""
        if self._me is None:
            self._me = await self.call("auth.test")
        return self._me

    async def my_conversations(self, types: str) -> list[dict[str, Any]]:
        """Every conversation of these types the person is in, every page."""
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params = {"types": types, "exclude_archived": "true", "limit": 200, **({"cursor": cursor} if cursor else {})}
            page = await self.call("users.conversations", params)
            rows.extend(page.get("channels") or [])
            cursor = next_cursor(page)
            if not cursor:
                return rows

    async def conversation(self, channel: str) -> dict[str, Any]:
        return (await self.call("conversations.info", {"channel": channel})).get("channel") or {}

    async def members(self, channel: str) -> list[str]:
        found = await self.call("conversations.members", {"channel": channel, "limit": 50})
        return [str(m) for m in found.get("members") or []]

    async def history(self, channel: str, limit: int = PAGE, oldest: str = "", latest: str = "",
                      cursor: str = "") -> dict[str, Any]:
        """One page, newest first, as Slack pages them; ``oldest`` and ``latest`` are exclusive."""
        params: dict[str, Any] = {"channel": channel, "limit": max(1, min(limit, 999))}
        params.update({k: v for k, v in (("oldest", oldest), ("latest", latest), ("cursor", cursor)) if v})
        return await self.call("conversations.history", params)

    async def window(self, channel: str, oldest: str, latest: str = "", pages: int = 5) -> tuple[list, bool]:
        """Every message between ``oldest`` and ``latest`` (both exclusive) that ``pages`` pages hold, oldest first,
        and whether that was all of them. Pages run newest to oldest, so when it was not all, what is missing is the
        oldest part: the caller narrows ``latest`` to the oldest one read and asks again, never skipping it."""
        found: list[dict[str, Any]] = []
        cursor, done = "", False
        for _ in range(pages):
            page = await self.history(channel, PAGE, oldest=oldest, latest=latest, cursor=cursor)
            found.extend(page.get("messages") or [])
            cursor = next_cursor(page)
            if not (page.get("has_more") and cursor):
                done = True
                break
        return sorted(found, key=lambda m: ts_number(m.get("ts"))), done

    async def replies(self, channel: str, ts: str, limit: int) -> dict[str, Any]:
        """The thread's parent first, then its replies oldest first."""
        return await self.call("conversations.replies", {"channel": channel, "ts": ts, "limit": limit})

    async def search(self, query: str, count: int, page: int = 1) -> dict[str, Any]:
        return (await self.call("search.messages", {"query": query, "count": max(1, min(count, 100)),
                                                    "page": page, "sort": "timestamp", "sort_dir": "desc"})
                ).get("messages") or {}

    async def user(self, user_id: str) -> dict[str, Any]:
        return (await self.call("users.info", {"user": user_id})).get("user") or {}

    async def user_by_email(self, email: str) -> dict[str, Any]:
        return (await self.call("users.lookupByEmail", {"email": email})).get("user") or {}

    async def users(self) -> list[dict[str, Any]]:
        """Every member of the workspace, every page (users.list is Tier 2: a few pages a call)."""
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page = await self.call("users.list", {"limit": 200, **({"cursor": cursor} if cursor else {})})
            rows.extend(page.get("members") or [])
            cursor = next_cursor(page)
            if not cursor:
                return rows

    async def file_info(self, file_id: str) -> dict[str, Any]:
        return (await self.call("files.info", {"file": file_id, "count": 1})).get("file") or {}

    async def file_bytes(self, url: str, name: str) -> bytes:
        """A file's bytes from Slack's file host, with the token, held to what a chat file may be. The token goes
        to Slack's own hosts only."""
        if host_of(url) not in {FILES_HOST, host_of(self.base)}:
            raise HttpError("invalid", f"{name} is kept outside Slack ({host_of(url) or 'no host'}): open it from "
                                       f"its link instead.")
        return await self.download(absolute=url, most=FILE_MOST, what=name)

    # -- writes ----------------------------------------------------------------------------------------------------
    async def post_message(self, channel: str, text: str, thread_ts: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"channel": channel, "text": text, **({"thread_ts": thread_ts} if thread_ts else {})}
        return await self.call("chat.postMessage", body=body, write=True)

    async def open_direct(self, user_id: str) -> str:
        """The direct-message conversation with this person, opened if it wasn't (nothing is sent)."""
        found = await self.call("conversations.open", body={"users": user_id, "return_im": True}, write=True)
        return str((found.get("channel") or {}).get("id") or "")

    async def react(self, channel: str, ts: str, name: str, add: bool = True) -> bool:
        """True when it changed; False when the reaction was already there (or already gone)."""
        try:
            await self.call("reactions.add" if add else "reactions.remove",
                            body={"channel": channel, "timestamp": ts, "name": name}, write=True)
        except HttpError as refused:
            if ("already_reacted" if add else "no_reaction") in refused.message:
                return False
            raise
        return True


class People:
    """User ids to display names, each asked of Slack at most once per call: twenty messages usually have three
    authors."""

    def __init__(self, client: SlackClient):
        self.client = client
        self._names: dict[str, str] = {}

    async def name(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id not in self._names:
            try:
                user = await self.client.user(user_id)
            except HttpError as refused:
                # A deleted or foreign user keeps their id rather than failing the whole answer.
                if refused.kind != "not_found":
                    raise
                user = {}
            self._names[user_id] = display(user) or user_id
        return self._names[user_id]

    async def author(self, message: dict[str, Any]) -> str:
        if message.get("user"):
            return await self.name(str(message["user"]))
        bot = message.get("bot_profile") or {}
        return str(message.get("username") or bot.get("name") or message.get("bot_id") or "")

    async def readable(self, text: Any) -> str:
        """Slack's markup as text: <@U…> as @name, <#C…|name> as #name, <url|label> as label (url), and its three
        escapes undone. Clipped, so one long paste cannot crowd out a whole answer."""
        text = str(text or "")
        for user_id in dict.fromkeys(re.findall(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", text)):
            name = await self.name(user_id)
            text = re.sub(rf"<@{user_id}(?:\|[^>]*)?>", lambda _m, n=name: "@" + n, text)
        text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
        text = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        return clip(html.unescape(text))


def display(user: dict[str, Any]) -> str:
    profile = user.get("profile") or {}
    return str(profile.get("display_name") or profile.get("real_name") or user.get("real_name")
               or user.get("name") or "")


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def next_cursor(page: dict[str, Any]) -> str:
    return str((page.get("response_metadata") or {}).get("next_cursor") or "")


def ts_number(ts: Any) -> Decimal:
    """Slack's "1712345678.123456" as an exact number: a float would round away the microseconds that tell two
    messages apart."""
    try:
        return Decimal(str(ts))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def is_ts(text: str) -> bool:
    return bool(re.fullmatch(r"\d{9,11}(\.\d{1,6})?", text or ""))


def ts_time(ts: Any) -> str:
    """When a ts was, in UTC, for people to read; the ts stays the id."""
    try:
        moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def permalink(team_url: str, channel: str, ts: str, thread_ts: str = "") -> str:
    """Slack's archive link, built rather than fetched: chat.getPermalink would be one more call per message."""
    if not (team_url and channel and ts):
        return ""
    link = f"{team_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        link += f"?thread_ts={thread_ts}&cid={channel}"
    return link


def kind_of(conversation: dict[str, Any]) -> str:
    if conversation.get("is_im"):
        return "dm"
    if conversation.get("is_mpim"):
        return "group_dm"
    if conversation.get("is_private") or conversation.get("is_group"):
        return "private_channel"
    return "channel"


def is_said(message: dict[str, Any]) -> bool:
    """Something a person (or bot) said, not a join or topic notice."""
    return str(message.get("subtype") or "") not in NOTICE_SUBTYPES


def files_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The files shared with a message, as files_read takes them."""
    rows = []
    for shared in message.get("files") or []:
        if not shared.get("id"):
            continue
        rows.append({"file_id": str(shared["id"]), "name": str(shared.get("name") or shared.get("title") or ""),
                     "size": int(shared.get("size") or 0), "type": str(shared.get("mimetype") or "")})
    return rows


async def message_row(message: dict[str, Any], channel: str, people: People, me_id: str,
                      team_url: str) -> dict[str, Any]:
    ts = str(message.get("ts") or "")
    thread_ts = str(message.get("thread_ts") or "")
    user_id = str(message.get("user") or "")
    row = {"ts": ts, "thread_ts": thread_ts, "sent": ts_time(ts), "author": await people.author(message),
           "author_id": user_id, "mine": bool(me_id) and user_id == me_id,
           "text": await people.readable(message.get("text")), "reply_count": int(message.get("reply_count") or 0),
           "link": permalink(team_url, channel, ts, thread_ts)}
    shared = files_of(message)
    return {**row, "files": shared} if shared else row


async def conversation_name(conversation: dict[str, Any], people: People, client: SlackClient, me_id: str) -> str:
    """A channel by its name; a DM by the other person; a group DM by everyone else in it."""
    kind = kind_of(conversation)
    if kind == "dm":
        return await people.name(str(conversation.get("user") or ""))
    if kind == "group_dm":
        others = [u for u in await client.members(str(conversation.get("id") or "")) if u != me_id]
        return ", ".join([await people.name(u) for u in others])
    return str(conversation.get("name") or conversation.get("id") or "")


def too_large(name: str, size: int, link: str) -> HttpError:
    return HttpError("too_large", f"{name} is {human_size(size)}; at most {human_size(FILE_MOST)} can be brought "
                                  f"into the chat. Open it in Slack instead{f': {link}' if link else ''}.")
