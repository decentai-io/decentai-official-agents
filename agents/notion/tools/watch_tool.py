"""Watching a database for new and changed rows.

A WATCH remembers how far a database's data source has been read: a cursor in Notion's own clock (the newest
last_edited_time handed on) and the ids of the rows that sit exactly on that cursor's minute. changes() asks for rows
edited on or after the cursor, hands on the ones not already seen, and moves the cursor; a schedule runs it with no
model.

Why the ids, and not just a timestamp: Notion keeps last_edited_time to the MINUTE. Many rows share one value, and
asking "on or after" the cursor returns the cursor's own minute again every time. The honest limit is the same
granularity: a row edited again within the minute it was last reported still carries that minute, and looks
unchanged until an edit in a later minute.

The ids are kept in ``values`` (an object), not in a keys string, so a busy minute cannot overflow a field; they are
capped at SEEN_MOST, past which a row on that one minute may be handed on twice, never lost.

Every watch is read on its own (``standard.watch.check``): one that fails keeps its place and is named in
``problems``; one whose database is gone, unshared or in the trash closes itself and is named in ``closed``; cursors
are written only once every watch was read.
"""

import json
from datetime import datetime, timezone

from standard import ResourceDenied, ToolBase
from standard import watch as watching
from standard.http import HttpError

from .notion_api import PAGE_SIZE_MAX, client_for, not_connected
from .schema import PageRow
from .sources import Several, resolve
from .text import Text

#: How many pages of results one watch reads per check before it stops and says "more".
PAGES_PER_CHECK = 5
#: The most row ids remembered for the cursor's minute.
SEEN_MOST = 2000
#: Seconds a check spends before leaving the watches not reached for the next run (its timeout is 90).
BUDGET = 50.0
ACTIVE = ("active", "watching")


def is_active(fields) -> bool:
    return str(fields.get("status") or "active") in ACTIVE


def seen_of(fields):
    """The ids already handed on at the cursor's minute; read from an older watch's keys string too."""
    kept = fields.get("seen_rows")
    if isinstance(kept, dict):
        return [str(i) for i in kept.get("ids") or []]
    try:
        legacy = json.loads(str(fields.get("seen") or "{}"))
    except ValueError:
        return []
    return [str(i) for i in legacy] if isinstance(legacy, (dict, list)) else []


class WatchTool(ToolBase):
    id = "watch"

    async def database(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        try:
            schema = await resolve(client, str(call.inputs["database_id"]))
            since, seen = await self._newest(client, schema.source_id)
        except Several as several:
            return {"error": str(several), "kind": "invalid"}, "error"
        except HttpError as failed:
            return failed.answer()
        watch_ref = await call.records.create("watch", {
            "database_id": schema.database_id or schema.source_id, "data_source_id": schema.source_id,
            "database": schema.title, "since": since, "seen_rows": {"minute": since, "ids": seen[-SEEN_MOST:]},
            "status": "active", "account": client.account, "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "database": schema.title, "since": since}, "success"

    @staticmethod
    async def _newest(client, source_id):
        """From now, in the database's clock: the newest edit marks the spot, and every row on that minute counts
        as already seen."""
        sorts = [{"timestamp": "last_edited_time", "direction": "descending"}]
        since, seen, cursor = "", [], ""
        for _ in range(PAGES_PER_CHECK):
            answer = await client.query(source_id, PAGE_SIZE_MAX, cursor, sorts=sorts)
            for page in answer.get("results") or []:
                stamp = Text.minute(page.get("last_edited_time"))
                since = since or stamp
                if stamp != since:
                    return since, seen
                seen.append(str(page.get("id") or ""))
            if not answer.get("has_more"):
                break
            cursor = str(answer.get("next_cursor") or "")
        if not since:
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00.000Z")
        return since, seen

    async def changes(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        account = str(call.inputs.get("account") or "").strip().lower()
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            records = [await call.records.read("watch", wanted)]
        else:
            listed = [r for r in await call.records.list("watch") if is_active(r["fields"])]
            records = [await call.records.read("watch", r["ref"]) for r in listed]
        watches = [w for w in records if is_active(w["fields"])
                   and (not account or account in str(w["fields"].get("account") or "").lower())]
        if not watches and not await call.records.list("notion"):
            _, why = await client_for(call, ask=False)
            return not_connected(why)
        clients = {}

        async def client_of(name):
            if name not in clients:
                clients[name] = await client_for(call, name or None)
            client, why = clients[name]
            if client is None:
                raise HttpError("auth", why)
            return client

        async def read(watch):
            fields = watch["fields"]
            client = await client_of(str(fields.get("account") or ""))
            source_id, cursor = str(fields.get("data_source_id") or ""), {}
            try:
                if not source_id:
                    # A watch kept before Notion's data sources names its database: found once, then kept.
                    source_id = (await resolve(client, str(fields.get("database_id") or ""))).source_id
                    cursor["data_source_id"] = source_id
                source = await client.data_source(source_id)
            except Several as several:
                raise HttpError("invalid", str(several)) from None
            if source.get("in_trash") or source.get("archived"):
                raise watching.Gone("The database is in the Notion trash.")
            since, seen = str(fields.get("since") or ""), seen_of(fields)
            found, cut = await self._unseen(client, source_id, since, set(seen), most)
            name = Text.rich(source.get("title")) or str(fields.get("database") or "")
            rows = [{**PageRow.row(page), "watch_ref": watch["ref"], "database": name,
                     "change": self._change(page, since, set(seen))} for page in found]
            if found:
                cursor.update(self._moved(since, seen, found))
                if fields.get("seen"):
                    # An older watch's keys string, now kept in seen_rows: emptied so it cannot overflow again.
                    cursor["seen"] = ""
            return watching.Seen(rows, cursor or None, more=cut)

        checked = await watching.check(call, "watch", watches, read, BUDGET,
                                       name=lambda w: str(w["fields"].get("database") or w["ref"]))
        return {"checked": checked["checked"], "rows": checked["rows"], "more": checked["more"],
                "problems": checked["problems"], "closed": checked["closed"]}, "success"

    @staticmethod
    async def _unseen(client, source_id, since, seen, most):
        """Rows edited on or after the cursor that were not already handed on at their current time, oldest first:
        at most ``most``, and whether there were more. One more than asked is looked for, so "more" is known
        without being handed on."""
        filter_ = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}}
        sorts = [{"timestamp": "last_edited_time", "direction": "ascending"}]
        found, cursor = [], ""
        for _ in range(PAGES_PER_CHECK):
            answer = await client.query(source_id, PAGE_SIZE_MAX, cursor, filter_, sorts)
            for page in answer.get("results") or []:
                stamp = Text.minute(page.get("last_edited_time"))
                if stamp < since or (stamp == since and str(page.get("id") or "") in seen):
                    continue
                found.append(page)
                if len(found) > most:
                    return found[:most], True
            if not answer.get("has_more"):
                return found, False
            cursor = str(answer.get("next_cursor") or "")
        return found, True

    @staticmethod
    def _change(page, since, seen):
        """created when the row came into being after the cursor, or on the cursor's own minute without having
        been seen there."""
        created = Text.minute(page.get("created_time"))
        if created > since or (created == since and str(page.get("id") or "") not in seen):
            return "created"
        return "edited"

    @staticmethod
    def _moved(since, seen, found):
        """The cursor after handing ``found`` on: the newest minute among them, and the rows on that minute."""
        newest = max(Text.minute(p.get("last_edited_time")) for p in found)
        kept = list(seen) if newest == since else []
        for page in found:
            page_id = str(page.get("id") or "")
            if Text.minute(page.get("last_edited_time")) == newest and page_id not in kept:
                kept.append(page_id)
        return {"since": newest, "seen_rows": {"minute": newest, "ids": kept[-SEEN_MOST:]}}


class WatchesTool(ToolBase):
    id = "watches"

    async def list(self, call):
        include_closed = bool(call.inputs.get("include_closed"))
        account = str(call.inputs.get("account") or "").strip().lower()
        rows = []
        for record in await call.records.list("watch"):
            if not include_closed and not is_active(record["fields"]):
                continue
            if account and account not in str(record["fields"].get("account") or "").lower():
                continue
            fields = (await call.records.read("watch", record["ref"]))["fields"]
            rows.append({"watch_ref": record["ref"], "status": "active" if is_active(fields) else "closed",
                         "database": str(fields.get("database") or ""),
                         "database_id": str(fields.get("database_id") or ""),
                         "since": str(fields.get("since") or ""), "account": str(fields.get("account") or ""),
                         "note": str(fields.get("note") or "")})
        return {"watches": rows}, "success"

    async def close(self, call):
        ref = str(call.inputs["watch_ref"])
        try:
            return await watching.close(call, "watch", ref), "success"
        except ResourceDenied as failed:
            return {"error": f"There is no watch {ref}: {failed}", "kind": "not_found"}, "error"
