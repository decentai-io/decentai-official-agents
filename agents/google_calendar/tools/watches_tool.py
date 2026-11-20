"""Watches on Google Tasks: what was completed, or what changed, since the last look, handed on once.

A watch follows one list or every list of one account, and one kind:

- ``completed``: tasks ticked off, by their ``completed`` time (``completedMin``), so editing a task already done
  does not report it again;
- ``changed``: tasks created, edited, completed, reopened or deleted, by their ``updated`` time (``updatedMin``).

The cursor is Google's own time, kept as Google wrote it (milliseconds and all), with the ids that sit exactly on it,
so two tasks in the same instant are neither lost nor shown twice. Edits this agent made itself are passed over
(``tasks_tool.remember``). ``standard.watch.check`` reads each watch on its own, closes one whose list is gone, and
writes every cursor only once every watch was read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from standard import ToolBase
from standard.google import Google, HttpError, segment
from standard.watch import Seen, check, close, listing

from .common import Refused, answers, client, google_time, moment, utc_text
from .tasks_tool import task_row, tasks_path

KINDS = {"completed": ("completed", "completedMin"), "changed": ("updated", "updatedMin")}
BUDGET = 80.0


def describe(record: dict[str, Any]) -> dict[str, Any]:
    fields = record["fields"]
    return {"account": str(fields.get("account") or ""), "kind": str(fields.get("kind") or ""),
            "list_id": str(fields.get("list_id") or ""), "list_name": str(fields.get("list_name") or "(every list)"),
            "since": str(fields.get("cursor") or "")}


class WatchesTool(ToolBase):
    id = "watches"

    @answers
    async def create(self, call):
        google = await client(call, "tasks")
        inputs = call.inputs
        kind = str(inputs.get("kind") or "completed")
        list_id = str(inputs.get("list_id") or "").strip()
        list_name = ""
        if list_id:
            found = await google.get(f"users/@me/lists/{segment(list_id)}")
            list_id, list_name = str(found.get("id") or list_id), str(found.get("title") or "")
        if inputs.get("since"):
            since = utc_text(moment(inputs["since"], timezone.utc, "since"))
        else:
            since = await self.now(google)
        label = f"{kind} in {list_name or 'every list'} ({google.account})"
        ref = await call.records.create("watch", {
            "account": google.account, "kind": kind, "list_id": list_id, "list_name": list_name, "cursor": since,
            "cursor_ids": "[]", "own": "{}", "status": "active", "label": label})
        return {"watch_ref": ref, "account": google.account, "kind": kind, "list_id": list_id,
                "list_name": list_name or "(every list)", "since": since}, "success"

    @staticmethod
    async def now(google: Google) -> str:
        """This moment in Google's clock (its answer's Date), not this machine's."""
        answer = await google.request("GET", "users/@me/lists", params={"maxResults": 1}, raw=True)
        try:
            said = parsedate_to_datetime(answer.headers.get("Date") or "")
        except (TypeError, ValueError):
            said = None
        return utc_text(said or datetime.now(timezone.utc))

    async def check(self, call):
        """Every active watch (or one), its news handed on once. Schedulable: no model, no question."""
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 50)
        try:
            if wanted:
                watches = [await call.records.read("watch", wanted)]
            else:
                watches = await call.records.list("watch", {"status": "active"})
        except Exception as failed:  # ResourceDenied: the platform's words
            return {"error": f"The watches could not be read: {failed}", "kind": "refused"}, "error"
        named = str(call.inputs.get("account") or "").strip().lower()
        watches = [w for w in watches if str(w["fields"].get("status") or "active") == "active"
                   and (not named or str(w["fields"].get("account") or "").lower() == named)]
        clients: dict[str, Google] = {}
        handed: list[dict[str, Any]] = []

        async def read(watch: dict[str, Any]) -> Seen:
            room = most - len(handed)
            if room <= 0:
                return Seen(more=True)
            fields = watch["fields"]
            name = str(fields.get("account") or "")
            if name not in clients:
                try:
                    clients[name] = await client(call, "tasks", name or None, ask=False)
                except Refused as refused:
                    raise HttpError("account", refused.message) from None
            seen = await self.read(clients[name], watch, room)
            handed.extend(seen.rows)
            return seen

        result = await check(call, "watch", watches, read, budget=BUDGET)
        return {"checked": result["checked"], "rows": result["rows"], "problems": result["problems"],
                "closed": result["closed"], "more": result["more"]}, "success"

    async def read(self, google: Google, watch: dict[str, Any], room: int) -> Seen:
        fields = watch["fields"]
        kind = str(fields.get("kind") or "completed")
        field, parameter = KINDS[kind]
        cursor_text = str(fields.get("cursor") or "")
        cursor = google_time(cursor_text) or datetime.now(timezone.utc)
        at_cursor = set(json.loads(str(fields.get("cursor_ids") or "[]")))
        own = json.loads(str(fields.get("own") or "{}"))
        if fields.get("list_id"):
            # A list deleted since raises not_found, and the watch closes.
            found = await google.get(f"users/@me/lists/{segment(fields['list_id'])}")
            lists = [(str(found.get("id") or fields["list_id"]), str(found.get("title") or ""))]
        else:
            lists = [(str(r.get("id") or ""), str(r.get("title") or ""))
                     for r in await google.token_pages("users/@me/lists", params={"maxResults": 1000})]
        fetched = []
        for list_id, list_name in lists:
            params = {parameter: cursor_text or utc_text(cursor), "showCompleted": "true", "showHidden": "true",
                      "maxResults": 100}
            if kind == "changed":
                params["showDeleted"] = "true"
            for task in await google.token_pages(tasks_path(list_id), params=params):
                when = google_time(task.get(field))
                if when is None or when < cursor or (when == cursor and str(task.get("id")) in at_cursor):
                    continue
                if kind == "completed" and (task.get("status") != "completed" or task.get("deleted")):
                    continue
                fetched.append((when, str(task.get("id") or ""), list_id, list_name, task))
        fetched.sort(key=lambda entry: (entry[0], entry[1]))
        rows, more, last, last_text, on_last, passed = [], False, None, "", set(), set()
        for when, task_id, list_id, list_name, task in fetched:
            mine = own.get(task_id)
            is_mine = mine is not None and (mine == str(task.get("updated") or "") or (
                mine == "deleted" and task.get("deleted")))
            if not is_mine and len(rows) >= room:
                more = True
                break
            # An own edit seen, or one overtaken by a later edit of the person's, is done with.
            passed.add(task_id)
            if not is_mine:
                rows.append({"watch_ref": watch["ref"], "kind": kind, "account": google.account,
                             "list_name": list_name, **task_row(task, list_id)})
            if when != last:
                last, last_text, on_last = when, str(task.get(field) or ""), set()
            on_last.add(task_id)
        if last is None:
            return Seen(rows=[], cursor=None)
        ids = on_last | (at_cursor if last == cursor else set())
        left = {k: v for k, v in own.items() if k not in passed}
        return Seen(rows=rows, more=more, cursor={
            "cursor": last_text, "cursor_ids": json.dumps(sorted(ids)),
            "own": json.dumps(left, separators=(",", ":"))})

    async def list(self, call):
        try:
            found = await listing(call, "watch", describe, include_closed=bool(call.inputs.get("include_closed")))
            named = str(call.inputs.get("account") or "").strip().lower()
            return {"watches": [w for w in found if not named or w["account"].lower() == named]}, "success"
        except Exception as failed:
            return {"error": f"The watches could not be read: {failed}", "kind": "refused"}, "error"

    async def close(self, call):
        try:
            return await close(call, "watch", str(call.inputs["watch_ref"])), "success"
        except Exception as failed:
            return {"error": f"That watch could not be closed: {failed}", "kind": "not_found"}, "error"
