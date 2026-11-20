"""The watch: what a schedule asks of a Slack account, with no model.

A watch keeps two places, because Slack has no single feed of "things for me" and its two sources behave apart:

- **Mentions** come from search.messages, whose index lags behind what was said. Each check looks back a window
  (``OVERLAP``) behind the newest mention it handed on, and leaves out the ts values it already handed on in that
  window, so a mention indexed late still comes, once.
- **Direct and group-direct messages** come from each such conversation's history, each conversation with a place
  of its own (``state.dms``: the ts read up to, and, while a burst is being read, the ts below which it is still
  unread). Every conversation is read in turn, those Slack marks as updated first, then the least recently read, so
  none is skipped while a place moves on. History pages run newest first: when one read cannot reach back to the
  place, the next read narrows to below what it saw, and nothing older is ever stepped over.

Slack's 2025 limits for apps distributed outside its Marketplace allow one history request a minute: the first
``rate`` stops the direct messages for this run, keeps every place, and says ``more``. Each watch is read on its own
(``standard.watch``): one that fails keeps its places and is listed in ``problems``; the others are handed on.
"""

import time
from decimal import Decimal

from standard import ResourceDenied, ToolBase
from standard.http import HttpError
from standard.watch import Seen, check

from .account_tool import client_for, failure, not_connected
from .messages_tool import search_row
from .slack_api import (PAGE, People, conversation_name, is_said, message_row, next_cursor, ts_number, ts_time)

WHICH = ("mentions", "dms", "both")
ACTIVE = ("active", "watching")          # "watching" is how 1.0.0 wrote an open watch
#: How far behind the newest mention handed on each check looks again, for mentions search indexed late.
OVERLAP = Decimal(900)
SEARCH_PAGES = 3
#: History requests one check may make in all, and pages for one conversation: well inside Slack's 50+ a minute
#: for an organization's own app. An app distributed outside the Marketplace is stopped by its first 429 instead.
HISTORY_CALLS = 30
PAGES_PER_CONVERSATION = 3
#: Seconds one check may spend reading, inside watch_new's 120.
BUDGET = 80.0
SEEN_MOST = 500


def ids(text):
    """'C0123, D0456' → ['C0123', 'D0456']; None when one is not an id."""
    found = [part.strip() for part in str(text or "").split(",") if part.strip()]
    if any(not part.isalnum() or not part.isupper() for part in found):
        return None
    return found


def status_of(record):
    status = str(record["fields"].get("status") or "active")
    return "active" if status in ACTIVE else status


class WatchTool(ToolBase):
    id = "watch"

    async def start(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        which = str(call.inputs.get("which") or "both")
        skip = ids(call.inputs.get("skip_conversations"))
        if which not in WHICH:
            return {"error": "which is mentions, dms or both.", "kind": "invalid"}, "error"
        if skip is None:
            return {"error": "skip_conversations is Slack conversation ids such as C0123ABCD, comma-separated.",
                    "kind": "invalid"}, "error"
        try:
            # Confirm the connection before recording anything: a watch on a revoked token would only fail later.
            me = await client.me()
        except HttpError as exc:
            return failure(exc)
        # From now. A Slack ts is seconds since the epoch, so the local clock writes one in the same terms.
        since = f"{time.time():.6f}"
        fields = {"which": which, "skip_conversations": ",".join(skip), "since": since, "mention_after": since,
                  "account": client.account, "status": "active", "state": {"mentions_seen": [], "dms": {}}}
        if call.inputs.get("note"):
            fields["note"] = str(call.inputs["note"])
        watch_ref = await call.records.create("watch", fields)
        return {"watch_ref": watch_ref, "which": which, "cursor": since, "since": ts_time(since),
                "user": str(me.get("user") or ""), "account": client.account}, "success"

    async def new(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        try:
            if wanted:
                record = await call.records.read("watch", wanted)
                watches = [record] if status_of(record) == "active" else []
            else:
                watches = [r for r in await call.records.list("watch") if status_of(r) == "active"]
            named = str(call.inputs.get("account") or "").strip().lower()
            if named:
                watches = [w for w in watches if named in str(w["fields"].get("account") or "").lower()]
            # A listing holds keys only: each watch's places (values) come with the record itself.
            watches = [await call.records.read("watch", w["ref"]) for w in watches]
        except ResourceDenied as exc:
            return {"error": str(exc), "kind": exc.code or "not_found"}, "error"
        if not await call.records.list("slack"):
            return not_connected("No Slack account is connected to this agent: connect it from the agent's "
                                 "Credentials.")
        reader = Reader(call, most)
        found = await check(call, "watch", watches, reader.read, budget=BUDGET,
                            name=lambda w: f"{w['fields'].get('which') or 'both'} on "
                                           f"{w['fields'].get('account') or 'Slack'}")
        return {"checked": found["checked"], "messages": found["rows"], "more": found["more"],
                "unchecked": reader.unchecked, "problems": found["problems"], "closed": found["closed"]}, "success"


class Reader:
    """One check: a client per account, and each watch's two sources read with it."""

    def __init__(self, call, most):
        self.call, self.most = call, most
        self.clients, self.unchecked = {}, 0
        self.deadline = time.monotonic() + BUDGET

    async def client(self, account):
        if account not in self.clients:
            client, why = await client_for(self.call, account or None, ask=False)
            if client is None:
                raise HttpError("auth", why)
            me = await client.me()
            self.clients[account] = (client, str(me.get("user_id") or ""), str(me.get("url") or ""), People(client))
        return self.clients[account]

    async def read(self, watch):
        fields = watch["fields"]
        client, me_id, team_url, people = await self.client(str(fields.get("account") or ""))
        # A 1.0.0 watch kept one cursor for both: each of the two places starts from it.
        since = str(fields.get("since") or fields.get("cursor") or f"{time.time():.6f}")
        which = str(fields.get("which") or "both")
        skip = set(ids(fields.get("skip_conversations")) or [])
        state = dict(fields.get("state") or {}) if isinstance(fields.get("state"), dict) else {}
        rows, more = [], False
        after = str(fields.get("mention_after") or since)
        seen = [str(ts) for ts in state.get("mentions_seen") or []]
        if which in ("mentions", "both"):
            found, after, seen, partly = await self.mentions(client, people, me_id, since, after, seen, skip,
                                                             leave_dms=which == "both")
            rows += found
            more = more or partly
        dms = dict(state.get("dms") or {})
        if which in ("dms", "both"):
            found, dms, partly = await self.direct(client, people, me_id, team_url, since, dms, skip)
            rows += found
            more = more or partly
        rows.sort(key=lambda r: ts_number(r["ts"]))
        cursor = {"mention_after": after, "since": since, "status": "active",
                  "state": {"mentions_seen": seen[-SEEN_MOST:], "dms": dms}}
        return Seen([{**r, "watch_ref": watch["ref"]} for r in rows], cursor, more=more)

    async def mentions(self, client, people, me_id, since, after, seen, skip, leave_dms):
        """Mentions after the place, looking ``OVERLAP`` back for those search indexed late."""
        lower = max(ts_number(since), ts_number(after) - OVERLAP)
        handed = set(seen)
        found, reached = [], False
        for page in range(1, SEARCH_PAGES + 1):
            answer = await client.search(f"<@{me_id}>", 100, page)
            matches = answer.get("matches") or []
            for match in matches:
                channel = match.get("channel") or {}
                ts = str(match.get("ts") or "")
                if (ts_number(ts) <= lower or ts in handed or str(channel.get("id") or "") in skip
                        or str(match.get("user") or "") == me_id
                        or (leave_dms and (channel.get("is_im") or channel.get("is_mpim")))):
                    continue
                found.append(match)
            # Newest first: once a page reaches back to the window, older pages hold nothing new.
            reached = any(ts_number(m.get("ts")) <= lower for m in matches)
            if reached or page >= int((answer.get("paging") or {}).get("pages") or 1):
                reached = True
                break
        found.sort(key=lambda m: ts_number(m.get("ts")))
        given = found[:self.most]
        rows = [{**await search_row(m, people), "kind": "mention"} for m in given]
        if rows:
            after = max((str(after), *[r["ts"] for r in rows]), key=ts_number)
        keep = ts_number(after) - OVERLAP
        seen = [ts for ts in dict.fromkeys([*seen, *[r["ts"] for r in rows]]) if ts_number(ts) > keep]
        return rows, after, seen, len(found) > self.most or not reached

    async def direct(self, client, people, me_id, team_url, since, dms, skip):
        conversations = [c for c in await client.my_conversations("im,mpim") if str(c.get("id") or "") not in skip]
        now = int(time.time())
        listed = {str(c.get("id") or "") for c in conversations}
        dms = {channel: place for channel, place in dms.items() if channel in listed}

        def place(channel):
            found = dms.get(channel)
            return list(found) if isinstance(found, list) and len(found) == 3 else [since, "", 0]

        def order(conversation):
            # users.conversations has no "last message"; updated (milliseconds) is Slack's hint of activity.
            after, before, checked = place(str(conversation.get("id") or ""))
            hinted = Decimal(int(conversation.get("updated") or 0)) / 1000 > ts_number(after) or bool(before)
            return (not hinted, int(checked or 0))

        conversations.sort(key=order)
        calls, room, rows, more = HISTORY_CALLS, self.most, [], False
        for number, conversation in enumerate(conversations):
            if calls <= 0 or room <= 0 or time.monotonic() > self.deadline:
                left = conversations[number:]
                self.unchecked += len(left)
                # Those Slack marks as updated hold news: say a later check continues. The rest are read in turn.
                more = more or room <= 0 or any(not order(c)[0] for c in left)
                break
            channel = str(conversation.get("id") or "")
            after, before, _ = place(channel)
            try:
                messages, done, used, limited = await self.window(client, channel, after, before,
                                                                  min(calls, PAGES_PER_CONVERSATION))
            except HttpError as refused:
                if refused.kind in ("not_found", "denied"):
                    dms.pop(channel, None)
                    continue
                raise
            calls -= used
            if limited and not messages:
                # Slack's limit for this run: every place kept, the rest next time.
                self.unchecked += len(conversations) - number
                more = True
                break
            if not done:
                # Pages run newest first and did not reach back to the place: next time, read below what was seen.
                dms[channel] = [after, str(messages[0].get("ts")) if messages else before, now]
                more = True
                if limited:
                    self.unchecked += len(conversations) - number - 1
                    break
                continue
            said = [m for m in messages if is_said(m) and str(m.get("user") or "") != me_id]
            given = said[:room]
            if len(said) > room:
                last, more = str(given[-1].get("ts")), True
            elif messages:
                last = str(messages[-1].get("ts"))
            else:
                last = after
            dms[channel] = [max(after, last, key=ts_number), "", now]
            if given:
                room -= len(given)
                name = await conversation_name(conversation, people, client, me_id)
                for message in given:
                    row = await message_row(message, channel, people, me_id, team_url)
                    row.pop("mine", None)
                    row.pop("reply_count", None)
                    rows.append({**row, "conversation_id": channel, "conversation": name, "kind": "dm"})
            if limited:
                self.unchecked += len(conversations) - number - 1
                more = True
                break
        return rows, dms, more

    @staticmethod
    async def window(client, channel, after, before, pages):
        """Messages between ``after`` and ``before`` (or now), oldest first; whether that was all; requests made;
        and whether Slack's rate limit stopped the reading (what was read before it still counts)."""
        found, cursor, used = [], "", 0
        while used < pages:
            try:
                page = await client.history(channel, PAGE, oldest=after, latest=before, cursor=cursor)
            except HttpError as refused:
                if refused.kind != "rate":
                    raise
                return sorted(found, key=lambda m: ts_number(m.get("ts"))), False, used, True
            used += 1
            found.extend(page.get("messages") or [])
            cursor = next_cursor(page)
            if not (page.get("has_more") and cursor):
                return sorted(found, key=lambda m: ts_number(m.get("ts"))), True, used, False
        return sorted(found, key=lambda m: ts_number(m.get("ts"))), False, used, False
