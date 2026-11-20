"""Watches: what a schedule can ask of this account, each on ``standard.watch`` (one failing watch never loses the
others' news, a gone target closes its watch, cursors move only with the rows handed on).

- A **reply** watch remembers when a conversation last moved; a check says whether anyone other than the account
  has written since, and closes once someone has. Every address of the account counts as its own.
- An **inbox** watch remembers how far a folder has been read: a time in Graph's clock and the ids exactly on it.
- A **task** watch follows To Do's own change feed (delta) per list: a completion is handed on once, an edit to a
  task already done is not a completion again.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from standard import ToolBase
from standard.graph import address, graph_for, utc
from standard.http import HttpError
from standard.watch import Gone, Seen, active, check

from .common import Refused, client, guarded, local_text, moment, senders, utc_z, zone_for
from .mail import FIELDS, conversation, folder_id, folder_path, mine, received, row, thread_of
from .tasks_tool import list_of, lists, task_path, task_row

#: Inside the checks' 90-second timeout, with room for the cursors to be written.
BUDGET = 50.0
#: Completions remembered per task watch, so an edit to a done task is not a completion again.
SEEN_MOST = 1000
#: Pages of one list's change feed read in one run; the rest is the next run's.
DELTA_PAGES = 20


def now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hashed(task_id: str) -> str:
    return hashlib.sha1(task_id.encode()).hexdigest()[:12]


async def watched_graph(call, fields, cache):
    """The client of the account a watch was made on, one per account; nobody is asked to connect one."""
    account = str(fields.get("account") or "").lower()
    if account not in cache:
        graph, why = await graph_for(call, account, ask=False)
        if graph is not None:
            call.clients.append(graph)
        cache[account] = (graph, why)
    graph, why = cache[account]
    if graph is None:
        raise HttpError("auth", why)
    return graph


async def watches(call, resource: str) -> list[dict[str, Any]]:
    wanted = str(call.inputs.get("watch_ref") or "")
    if wanted:
        record = await call.records.read(resource, wanted)
        return active([record])
    return active(await call.records.list(resource))


async def delta(graph, list_id: str, link: str) -> tuple[str, list[dict[str, Any]], bool]:
    """(the link to go on from, the tasks changed, whether more is left) from one list's change feed."""
    answer = await (graph.get("", absolute=link) if link else graph.get(task_path(list_id) + "/delta"))
    items, pages = [], 1
    while True:
        items.extend(answer.get("value") or [])
        if answer.get("@odata.deltaLink"):
            return str(answer["@odata.deltaLink"]), items, False
        following = str(answer.get("@odata.nextLink") or "")
        if not following:
            return link, items, False
        if pages >= DELTA_PAGES:
            return following, items, True
        answer = await graph.get("", absolute=following)
        pages += 1


class WatchTool(ToolBase):
    id = "watch"

    # -- replies -------------------------------------------------------------------------------------------------
    @guarded
    async def reply(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs, soft=True)
        thread_id = await thread_of(graph, call.inputs)
        found = await conversation(graph, thread_id)
        if not found:
            raise Refused(f"Outlook has no conversation {thread_id} in this mailbox.", "not_found")
        last = max(found, key=received)
        subject = str(found[0].get("subject") or "")
        ref = await call.records.create("watch", {
            "thread_id": thread_id, "since": utc(received(last)), "anchor_id": str(last.get("id") or ""),
            "status": "active", "account": graph.account.lower(), "subject": subject,
            "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": ref, "subject": subject, "since": local_text(received(last), zone)}, "success"

    @guarded
    async def check(self, call):
        cache: dict[str, Any] = {}

        async def read(watch):
            fields = (await call.records.read("watch", watch["ref"]))["fields"]
            graph = await watched_graph(call, fields, cache)
            zone = await zone_for(graph, {}, soft=True)
            me = await graph.me()
            found = await conversation(graph, str(fields.get("thread_id") or ""))
            if not found:
                raise Gone("The conversation is no longer in the mailbox.")
            since, anchor = str(fields.get("since") or ""), str(fields.get("anchor_id") or "")
            # A reply is any later message not from the account, by time: a deleted anchor stops nothing.
            replies = [m for m in found if str(m.get("id") or "") != anchor and utc(received(m)) >= since
                       and not mine(m, me)]
            entry = {"watch_ref": watch["ref"], "thread_id": str(fields.get("thread_id") or ""),
                     "subject": str(fields.get("subject") or "")}
            if fields.get("note"):
                entry["note"] = str(fields["note"])
            if not replies:
                return Seen(rows=[{**entry, "replied": False}])
            first = replies[0]
            return Seen(rows=[{**entry, "replied": True, "reply_from": address(first.get("from")),
                               "reply_date": local_text(received(first), zone),
                               "message_id": str(first.get("id") or "")}],
                        close=f"Answered by {address(first.get('from'))}.")

        found = await check(call, "watch", await watches(call, "watch"), read, BUDGET)
        rows = found.pop("rows")
        return {**found, "unanswered": [{k: v for k, v in r.items() if k != "replied"} for r in rows
                                        if not r["replied"]],
                "answered": [{k: v for k, v in r.items() if k != "replied"} for r in rows if r["replied"]]}, "success"

    # -- the inbox -----------------------------------------------------------------------------------------------
    @guarded
    async def inbox(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs, soft=True)
        folder, name = await folder_id(graph, call.inputs.get("folder"))
        asked, since, ids, note = str(call.inputs.get("since") or "").strip(), "", "", ""
        if asked:
            since = utc_z(moment(asked, zone, "since"))
            if since > now_z():
                note = f"since {asked!r} is in the future, so the watch starts from now."
                since = ""
        if not since:
            # From now, and "now" is the mailbox's: its newest message marks the spot and is not news itself.
            page = await graph.get(folder_path(folder), params={"$orderby": "receivedDateTime desc", "$top": 1,
                                                                "$select": "id,receivedDateTime"})
            newest = (page.get("value") or [None])[0]
            since, ids = (utc(newest.get("receivedDateTime")), str(newest.get("id") or "")) if newest else \
                (now_z(), "")
        ref = await call.records.create("inbox", {
            "since": since, "cursor_ids": ids, "status": "active", "account": graph.account.lower(),
            "folder": folder, "folder_name": name, "only_from": senders(call.inputs.get("only_from")),
            "skip_from": senders(call.inputs.get("skip_from")), "note": str(call.inputs.get("note") or "")})
        started = {"watch_ref": ref, "folder": name, "since": local_text(since, zone)}
        if note:
            started["note"] = note
        return started, "success"

    @guarded
    async def new_mail(self, call):
        cache: dict[str, Any] = {}
        most = int(call.inputs.get("max_results") or 20)

        async def read(watch):
            fields = (await call.records.read("inbox", watch["ref"]))["fields"]
            graph = await watched_graph(call, fields, cache)
            zone = await zone_for(graph, {}, soft=True)
            me = await graph.me()
            since = str(fields.get("since") or "")
            seen = {i for i in str(fields.get("cursor_ids") or "").split(",") if i}
            # Filtered and ordered on the one property, as Graph requires; the ids already handed on at the cursor's
            # second come back too, so that many more are asked for.
            page = await graph.get(folder_path(str(fields.get("folder") or "inbox")), params={
                "$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime asc",
                "$top": most + 1 + len(seen), "$select": FIELDS})
            fetched = [m for m in page.get("value") or [] if str(m.get("id") or "") not in seen]
            more = len(fetched) > most or bool(page.get("@odata.nextLink"))
            fetched = fetched[:most]
            rows = [{**row(m, zone), "watch_ref": watch["ref"]} for m in fetched
                    if not mine(m, me) and self._wanted(m, fields)]
            if not fetched:
                return Seen(more=more)
            newest = utc(received(fetched[-1]))
            at_newest = {str(m.get("id") or "") for m in fetched if utc(received(m)) == newest}
            if newest == since:
                at_newest |= seen
            return Seen(rows=rows, cursor={"since": newest, "cursor_ids": ",".join(sorted(at_newest))}, more=more)

        found = await check(call, "inbox", await watches(call, "inbox"), read, BUDGET)
        return {**found, "messages": found.pop("rows")}, "success"

    @staticmethod
    def _wanted(message, fields) -> bool:
        sender = str(((message.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        only = [p for p in str(fields.get("only_from") or "").split(",") if p]
        skip = [p for p in str(fields.get("skip_from") or "").split(",") if p]
        matches = lambda p: sender.endswith(p) if p.startswith("@") else sender == p  # noqa: E731
        return not any(matches(p) for p in skip) and (not only or any(matches(p) for p in only))

    # -- To Do -----------------------------------------------------------------------------------------------------
    @guarded
    async def tasks(self, call):
        graph = await client(call)
        wanted = str(call.inputs.get("list") or "").strip()
        targets = [await list_of(graph, wanted)] if wanted else await self._every_list(graph)
        links, seen = {}, []
        for list_id, _ in targets:
            link, items, _ = await delta(graph, list_id, "")
            links[list_id] = link
            seen += [hashed(str(t.get("id"))) for t in items if t.get("status") == "completed"]
        ref = await call.records.create("taskwatch", {
            "list_id": targets[0][0] if wanted else "", "status": "active", "account": graph.account.lower(),
            "report": str(call.inputs.get("report") or "completed"), "started": now_z(), "checked_at": now_z(),
            "list_name": targets[0][1] if wanted else "", "links": links, "seen": {"ids": seen[-SEEN_MOST:]},
            "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": ref, "lists": [name for _, name in targets],
                "report": str(call.inputs.get("report") or "completed")}, "success"

    @guarded
    async def task_changes(self, call):
        cache: dict[str, Any] = {}

        async def read(watch):
            fields = (await call.records.read("taskwatch", watch["ref"]))["fields"]
            graph = await watched_graph(call, fields, cache)
            zone = await zone_for(graph, {}, soft=True)
            everything = fields.get("report") == "all"
            links = dict(fields.get("links") or {})
            seen = list((fields.get("seen") or {}).get("ids") or [])
            known = set(seen)
            started = str(fields.get("started") or "")
            # A completion older than the watch (less a day: To Do may round it to a date) was never news here.
            oldest = utc_z(datetime.fromisoformat(started.replace("Z", "+00:00")) - timedelta(days=1)) if started \
                else ""
            if fields.get("list_id"):
                targets = [(str(fields["list_id"]), str(fields.get("list_name") or ""))]
            else:
                targets = await self._every_list(graph)
                links = {k: v for k, v in links.items() if k in {t[0] for t in targets}}
            rows, more = [], False
            for list_id, name in targets:
                link = links.get(list_id, "")
                try:
                    following, items, left = await delta(graph, list_id, link)
                except HttpError as failed:
                    if failed.kind == "not_found" and fields.get("list_id"):
                        raise Gone(f"The To Do list {name or list_id} no longer exists.") from None
                    if failed.status != 410 and failed.kind != "not_found":
                        raise
                    # The feed's place expired: start again from now; nothing from before is handed on.
                    following, items, left, link = *(await delta(graph, list_id, "")), ""
                links[list_id] = following
                more = more or left
                if not link:
                    # A list met for the first time (or a feed started again): where it stands is not news.
                    for task in items:
                        if task.get("status") == "completed" and hashed(str(task.get("id"))) not in known:
                            known.add(hashed(str(task.get("id"))))
                            seen.append(hashed(str(task.get("id"))))
                    continue
                for task in items:
                    key = hashed(str(task.get("id") or ""))
                    base = {"watch_ref": watch["ref"], "task_id": str(task.get("id") or ""), "list_id": list_id,
                            "list": name}
                    if "@removed" in task:
                        if key in known:
                            known.discard(key)
                            seen.remove(key)
                        if everything:
                            rows.append({**base, "change": "removed"})
                        continue
                    shown = {**task_row(task, list_id, zone), **base}
                    completed = task.get("status") == "completed"
                    if completed and key not in known:
                        known.add(key)
                        seen.append(key)
                        done_at = utc(str((task.get("completedDateTime") or {}).get("dateTime") or ""))
                        if not done_at or not oldest or done_at >= oldest:
                            rows.append({**shown, "change": "completed"})
                    elif not completed and key in known:
                        known.discard(key)
                        seen.remove(key)
                        if everything:
                            rows.append({**shown, "change": "reopened"})
                    elif everything:
                        made = utc(task.get("createdDateTime"))
                        rows.append({**shown, "change": "added" if made and made >= str(fields.get("checked_at") or "")
                                     else "changed"})
            return Seen(rows=rows, more=more, cursor={"links": links, "seen": {"ids": seen[-SEEN_MOST:]},
                                                      "checked_at": now_z()})

        found = await check(call, "taskwatch", await watches(call, "taskwatch"), read, BUDGET)
        return {**found, "changes": found.pop("rows")}, "success"

    @staticmethod
    async def _every_list(graph):
        """Every list a person keeps tasks in: Outlook's flagged mail is not one."""
        return [(str(f.get("id") or ""), str(f.get("displayName") or "")) for f in await lists(graph)
                if f.get("wellknownListName") != "flaggedEmails"]
