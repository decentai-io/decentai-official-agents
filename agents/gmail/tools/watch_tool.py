"""Watches: what a schedule asks this mailbox, through ``standard.watch`` (each watch read alone, cursors written once
all were read, a gone target closing its watch).

A REPLY watch remembers where a thread stood (its last message and that message's time). A check asks whether anyone
other than the account, any of its send-as addresses included, has written since. A thread deleted, or moved to the
trash whole, closes the watch.

An INBOX watch follows Gmail's history (``history.list`` from a ``startHistoryId``): every message added to the inbox
since, oldest first, none skipped however busy the inbox. Gmail keeps history for about a week, sometimes hours; once
the id has expired (404) the watch reads the inbox again from the time of the last message it handed on, through every
page, and goes back to history from where that reading began. Mail from the account itself, its aliases included, is
never news.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from standard import ResourceDenied, ToolBase
from standard.http import HttpError
from standard.watch import Gone, Seen, check

from .gmail import Gmail, gathered, gmail_for, internal_ms, sender, summary
from .mime import header

LIVE_REPLY = {"active", "waiting"}      # "waiting": a 1.x watch
LIVE_INBOX = {"active", "watching"}     # "watching": a 1.x inbox watch
NOT_NEWS = {"DRAFT", "SENT", "SPAM", "TRASH"}
#: How long a check may spend before it leaves the rest of its watches to the next run.
BUDGET = 40.0
#: Ids handed on by a full reading that history may name again: kept to be skipped once, at most this many.
RECENT_MOST = 100


def iso(seconds: int) -> str:
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_of(text: Any) -> int | None:
    """An ISO 8601 date-time as epoch seconds; one without an offset is read as UTC. None when it is not one."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def ids_of(text: Any) -> list[str]:
    return [i for i in str(text or "").split(",") if i]


def senders(text: Any) -> str:
    """'Dana@x.example, @harbourline.example' as lower-case addresses and @domains."""
    return ",".join(p.strip().lower() for p in str(text or "").replace(";", ",").split(",") if p.strip())


def is_sender(address: str, pattern: str) -> bool:
    return address.endswith(pattern) if pattern.startswith("@") else address == pattern


def wanted(address: str, fields: dict[str, Any]) -> bool:
    only, skip = ids_of(fields.get("only_from")), ids_of(fields.get("skip_from"))
    if any(is_sender(address, p) for p in skip):
        return False
    return not only or any(is_sender(address, p) for p in only)


class Clients:
    """One client per account a check's watches were made on."""

    def __init__(self, call):
        self.call, self.held = call, {}

    async def __call__(self, account: str) -> Gmail:
        account = str(account or "").lower()
        if account not in self.held:
            self.held[account] = await gmail_for(self.call, account, ask=False)
        client, why = self.held[account]
        if client is None:
            raise HttpError("auth", why)
        return client


class WatchTool(ToolBase):
    id = "watch"

    # -- a reply awaited ---------------------------------------------------------------------------------------
    async def await_reply(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        thread_id = str(call.inputs["thread_id"])
        try:
            thread = await client.thread(thread_id, "metadata")
        except HttpError as failed:
            return failed.answer()
        live = [m for m in thread.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
        if not live:
            return {"error": f"Thread {thread_id} has no messages.", "kind": "not_found"}, "error"
        last = live[-1]
        subject = next((header(m, "Subject") for m in live if header(m, "Subject")), "")
        watch_ref = await call.records.create("watch", {
            "thread_id": str(thread.get("id") or thread_id), "since_message_id": str(last.get("id") or ""),
            "since_time": str(internal_ms(last)), "status": "active", "account": client.account.lower(),
            "subject": subject, "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "subject": subject, "since_message_id": str(last.get("id") or "")}, "success"

    async def check(self, call):
        wanted_ref = str(call.inputs.get("watch_ref") or "")
        try:
            rows = [await call.records.read("watch", wanted_ref)] if wanted_ref else await call.records.list("watch")
        except ResourceDenied as refused:
            return {"error": f"No reply watch {wanted_ref}: {refused}", "kind": "not_found"}, "error"
        watches = [r for r in rows if str(r["fields"].get("status") or "active") in LIVE_REPLY]
        clients = Clients(call)

        async def read(watch: dict[str, Any]) -> Seen:
            fields = (await call.records.read("watch", watch["ref"]))["fields"]
            client = await clients(fields.get("account"))
            thread = await client.thread(str(fields.get("thread_id") or ""), "metadata")
            me = await client.me()
            live = [m for m in thread.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
            if not live or all("TRASH" in (m.get("labelIds") or []) for m in live):
                raise Gone("The conversation was deleted or moved to the trash.")
            anchor, since = str(fields.get("since_message_id") or ""), int(str(fields.get("since_time") or "0") or 0)
            ids = [str(m.get("id")) for m in live]
            after = live[ids.index(anchor) + 1:] if anchor in ids else [m for m in live if internal_ms(m) > since]
            reply = next((m for m in after if sender(m) not in me and not {"SENT", "TRASH", "SPAM"}
                          & set(m.get("labelIds") or [])), None)
            entry = {"watch_ref": watch["ref"], "thread_id": str(fields.get("thread_id") or ""),
                     "subject": str(fields.get("subject") or "")}
            if reply is None:
                return Seen([{**entry, "note": str(fields.get("note") or ""), "_answered": False}])
            return Seen([{**entry, "reply_from": header(reply, "From"), "reply_date": header(reply, "Date"),
                          "message_id": str(reply.get("id") or ""), "_answered": True}], {"status": "replied"})

        result = await check(call, "watch", watches, read, BUDGET)
        answered = [{k: v for k, v in r.items() if k != "_answered"} for r in result["rows"] if r["_answered"]]
        unanswered = [{k: v for k, v in r.items() if k != "_answered"} for r in result["rows"] if not r["_answered"]]
        return {"checked": result["checked"], "unanswered": unanswered, "answered": answered,
                "closed": result["closed"], "problems": result["problems"], "more": result["more"]}, "success"

    # -- the inbox ---------------------------------------------------------------------------------------------
    async def inbox(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        asked = str(call.inputs.get("since") or "").strip()
        start, note, history_id = None, "", ""
        if asked:
            start = seconds_of(asked)
            if start is None:
                return {"error": f"since must be an ISO 8601 date-time with its offset, not {asked!r}.",
                        "kind": "invalid"}, "error"
            if start > time.time():
                # A time in the person's zone read as UTC lands hours ahead: nobody means "start later tonight".
                note = (f"since {asked!r} is in the future by the mailbox's clock (UTC), so the watch starts from now. "
                        f"Give a time with its offset, like 2026-09-20T17:51:00+04:00.")
                start = None
        if start is None:
            try:
                history_id = str((await client.profile(fresh=True)).get("historyId") or "")
            except HttpError as failed:
                return failed.answer()
            start = int(time.time())
        inbox_ref = await call.records.create("inbox", {
            "history_id": history_id, "since": iso(start), "cursor_ids": "", "recent_ids": "", "status": "active",
            "account": client.account.lower(), "only_from": senders(call.inputs.get("only_from")),
            "skip_from": senders(call.inputs.get("skip_from")), "note": str(call.inputs.get("note") or "")})
        started = {"inbox_ref": inbox_ref, "since": iso(start)}
        if note:
            started["note"] = note
        return started, "success"

    async def new_mail(self, call):
        wanted_ref = str(call.inputs.get("inbox_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        try:
            rows = [await call.records.read("inbox", wanted_ref)] if wanted_ref else await call.records.list("inbox")
        except ResourceDenied as refused:
            return {"error": f"No inbox watch {wanted_ref}: {refused}", "kind": "not_found"}, "error"
        watches = [r for r in rows if str(r["fields"].get("status") or "active") in LIVE_INBOX]
        clients, resynced = Clients(call), []

        async def read(watch: dict[str, Any]) -> Seen:
            fields = (await call.records.read("inbox", watch["ref"]))["fields"]
            client = await clients(fields.get("account"))
            if str(fields.get("history_id") or ""):
                try:
                    return await self._by_history(client, watch["ref"], fields, most)
                except HttpError as failed:
                    if failed.kind != "not_found":
                        raise
                    resynced.append(watch["ref"])   # the history id expired: read the inbox again
            return await self._resync(client, watch["ref"], fields, most)

        result = await check(call, "inbox", watches, read, BUDGET)
        answer = {"checked": result["checked"], "messages": result["rows"], "more": result["more"],
                  "closed": result["closed"], "problems": result["problems"]}
        if resynced:
            answer["resynced"] = resynced
        return answer, "success"

    @staticmethod
    def _keep(fields: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
        """The time of the newest message read, and the ids on that second: where a full reading would start."""
        since = seconds_of(fields.get("since")) or 0
        at = set(ids_of(fields.get("cursor_ids")))
        for message in messages:
            second = internal_ms(message) // 1000
            if second > since:
                since, at = second, {str(message.get("id"))}
            elif second == since:
                at.add(str(message.get("id")))
        return {"since": iso(since), "cursor_ids": ",".join(sorted(at))}

    async def _rows(self, client: Gmail, ref: str, fields: dict[str, Any], messages: list[dict[str, Any]]):
        me, address, rows = await client.me(), await client.address(), []
        for message in messages:
            if NOT_NEWS & set(message.get("labelIds") or []):
                continue
            who = sender(message)
            if who in me or not wanted(who, fields):
                continue
            rows.append({**summary(message, address), "inbox_ref": ref})
        return rows

    @staticmethod
    async def _fetch(client: Gmail, ids: list[str]) -> list[dict[str, Any]]:
        found = []
        for got in await gathered(client.message(i, "metadata") for i in ids):
            if isinstance(got, HttpError):
                if got.kind == "not_found":   # deleted since it arrived
                    continue
                raise got
            found.append(got)
        return found

    async def _by_history(self, client: Gmail, ref: str, fields: dict[str, Any], most: int) -> Seen:
        params = {"startHistoryId": str(fields["history_id"]), "historyTypes": "messageAdded", "labelId": "INBOX",
                  "maxResults": 500}
        recent = set(ids_of(fields.get("recent_ids")))
        rows, read, cursor, more = [], [], str(fields["history_id"]), False
        page = await client.get("history", params=params)
        while True:
            for record in page.get("history") or []:
                if len(rows) >= most:
                    more = True
                    break
                ids = [str((m.get("message") or {}).get("id") or "") for m in record.get("messagesAdded") or []]
                fetched = await self._fetch(client, [i for i in dict.fromkeys(ids) if i and i not in recent])
                rows += await self._rows(client, ref, fields, fetched)
                read += fetched
                cursor = str(record.get("id") or cursor)
            if more:
                break
            token = str(page.get("nextPageToken") or "")
            if not token:
                cursor = str(page.get("historyId") or cursor)
                break
            page = await client.get("history", params={**params, "pageToken": token})
        return Seen(rows, {"history_id": cursor, "recent_ids": "", **self._keep(fields, read)}, more=more)

    async def _resync(self, client: Gmail, ref: str, fields: dict[str, Any], most: int) -> Seen:
        """Every inbox message since the last one handed on, oldest first, through every page: no cap, so a long gap
        skips nothing. History resumes from the id the mailbox had before the reading began."""
        start = str((await client.profile(fresh=True)).get("historyId") or "")
        since = seconds_of(fields.get("since")) or 0
        seen = set(ids_of(fields.get("cursor_ids")))
        listed = await client.token_pages("messages", params={"q": f"in:inbox after:{max(since - 1, 0)}",
                                                              "maxResults": 500}, items="messages")
        ids = list(dict.fromkeys(str(m.get("id") or "") for m in reversed(listed) if m.get("id")))  # oldest first
        rows, read, more = [], [], False
        for at in range(0, len(ids), 10):
            batch = sorted(await self._fetch(client, ids[at:at + 10]), key=lambda m: (internal_ms(m), str(m["id"])))
            for message in batch:
                second = internal_ms(message) // 1000
                if second < since or (second == since and str(message.get("id")) in seen):
                    continue
                if len(rows) >= most:
                    more = True
                    break
                rows += await self._rows(client, ref, fields, [message])
                read.append(message)
            if more:
                break
        cursor = self._keep(fields, read)
        if more:
            return Seen(rows, {"history_id": "", **cursor}, more=True)
        newest = max((internal_ms(m) for m in read), default=0)
        recent = [str(m["id"]) for m in read if internal_ms(m) >= newest - 15 * 60 * 1000][-RECENT_MOST:]
        return Seen(rows, {"history_id": start, "recent_ids": ",".join(recent), **cursor})
