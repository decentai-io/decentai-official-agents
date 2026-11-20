"""Microsoft To Do: the lists, a list's tasks, one task whole, and changing tasks.

A due date is a day in the person's zone, written as that day's midnight with the zone named
(``dueDateTime: {dateTime, timeZone}``); a completion is dated by the day it was in the person's zone, not cut from a
UTC time. Graph is asked to filter only, never to filter and order at once; the order is made here."""

from __future__ import annotations

from typing import Any

from standard import ToolBase
from standard.http import segment

from .common import (Refused, body_text, client, clip, day, graph_day, graph_moment, guarded, local, local_text,
                     moment, told, zone_for)

ROW_NOTE = 300


def list_row(found: dict[str, Any]) -> dict[str, Any]:
    return {"list_id": str(found.get("id") or ""), "name": str(found.get("displayName") or ""),
            # "defaultList" takes tasks no list was named for; "flaggedEmails" is Outlook's flagged mail.
            "kind": str(found.get("wellknownListName") or "none"), "shared": bool(found.get("isShared"))}


def task_row(task: dict[str, Any], list_id: str, zone, note: int = ROW_NOTE) -> dict[str, Any]:
    found = {"task_id": str(task.get("id") or ""), "list_id": list_id, "title": str(task.get("title") or ""),
             "status": str(task.get("status") or ""), "importance": str(task.get("importance") or "normal"),
             "due": graph_day(task.get("dueDateTime"), zone),
             "completed": graph_day(task.get("completedDateTime"), zone)}
    if task.get("isReminderOn") and task.get("reminderDateTime"):
        found["reminder"] = local(graph_moment(task["reminderDateTime"]), zone)
    text = clip((task.get("body") or {}).get("content"), note)
    if text:
        found["note"] = text
    return {k: v for k, v in found.items() if v != "" or k in ("title", "status")}


async def lists(graph) -> list[dict[str, Any]]:
    return await graph.pages("me/todo/lists", params={"$top": 100}, most=500)


async def list_of(graph, wanted: Any) -> tuple[str, str]:
    """(id, name) of the list named (by id or name), or the account's default list."""
    wanted = str(wanted or "").strip()
    found = await lists(graph)
    if not wanted:
        hits = [f for f in found if f.get("wellknownListName") == "defaultList"]
        if not hits:
            raise Refused("This account has no default To Do list: name one with list.", "not_found")
    else:
        hits = [f for f in found if str(f.get("id") or "") == wanted] or \
            [f for f in found if str(f.get("displayName") or "").lower() == wanted.lower()]
    if not hits:
        names = ", ".join(str(f.get("displayName") or "") for f in found)
        raise Refused(f"There is no To Do list {wanted!r}. Lists: {names}.", "not_found")
    return str(hits[0]["id"]), str(hits[0].get("displayName") or "")


def task_path(list_id: str, task_id: str = "") -> str:
    return f"me/todo/lists/{segment(list_id)}/tasks" + (f"/{segment(task_id)}" if task_id else "")


class TasksTool(ToolBase):
    id = "tasks"

    @guarded
    async def lists(self, call):
        graph = await client(call)
        return {"lists": [list_row(f) for f in await lists(graph)]}, "success"

    @guarded
    async def list(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        list_id, name = await list_of(graph, call.inputs.get("list"))
        status = str(call.inputs.get("status") or "open")
        params: dict[str, Any] = {"$top": 100}
        if status != "all":
            params["$filter"] = "status ne 'completed'" if status == "open" else "status eq 'completed'"
        found = await graph.pages(task_path(list_id), params=params, most=2000)
        rows = [task_row(t, list_id, zone) for t in found]
        if call.inputs.get("due_before"):
            limit = day(call.inputs["due_before"], "due_before").isoformat()
            rows = [r for r in rows if r.get("due") and r["due"] < limit]
        # Open first by due date (none last), then title; completed newest first.
        rows.sort(key=lambda r: (r["status"] == "completed", r.get("due") or "9999-99-99", r["title"].lower()))
        done = sorted((r for r in rows if r["status"] == "completed"), key=lambda r: r.get("completed") or "",
                      reverse=True)
        rows = [r for r in rows if r["status"] != "completed"] + done
        start = int(str(call.inputs.get("page_token") or "0") or 0)
        most = int(call.inputs.get("max_results") or 25)
        page = rows[start:start + most]
        result = {"list_id": list_id, "list": name, "tasks": page, "total": len(rows),
                  "more": start + most < len(rows), **told(zone)}
        if result["more"]:
            result["next_page_token"] = str(start + most)
        return result, "success"

    @guarded
    async def get(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        list_id, task_id = await self._ids(graph, call.inputs)
        task = await graph.get(task_path(list_id, task_id))
        items = await graph.pages(task_path(list_id, task_id) + "/checklistItems", most=100)
        note = await body_text(task.get("body"))
        found = task_row(task, list_id, zone, note=20000)
        found.update(note=note[:20000], note_chars=len(note), created=local_text(task.get("createdDateTime"), zone),
                     checklist=[{"item_id": str(i.get("id") or ""), "title": str(i.get("displayName") or ""),
                                 "checked": bool(i.get("isChecked"))} for i in items])
        return {**found, **told(zone)}, "success"

    @guarded
    async def create(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        list_id, _ = await list_of(graph, call.inputs.get("list"))
        body = {k: v for k, v in self._fields(call.inputs, zone).items() if v is not None}
        if not body.get("title"):
            raise Refused("A task needs a title.")
        created = await graph.post(task_path(list_id), json=body)
        return {**task_row(created, list_id, zone), **told(zone)}, "success"

    @guarded
    async def update(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        body = self._fields({k: v for k, v in call.inputs.items() if k not in ("list", "task_id")}, zone)
        if not body:
            raise Refused("Nothing to change: give a title, due, importance, note or reminder.")
        return await self._patched(call, graph, zone, body)

    @guarded
    async def complete(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        return await self._patched(call, graph, zone, {"status": "completed"})

    @guarded
    async def reopen(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs)
        return await self._patched(call, graph, zone, {"status": "notStarted"})

    @guarded
    async def delete(self, call):
        graph = await client(call)
        list_id, task_id = await self._ids(graph, call.inputs)
        # To Do refuses $select on a single task (invalidRequest, seen live 2026-11-20): read it whole.
        task = await graph.get(task_path(list_id, task_id))
        await graph.delete(task_path(list_id, task_id))
        return {"deleted": True, "task_id": task_id, "title": str(task.get("title") or "")}, "success"

    @guarded
    async def create_list(self, call):
        graph = await client(call)
        created = await graph.post("me/todo/lists", json={"displayName": str(call.inputs["name"]).strip()})
        return list_row(created), "success"

    # -- helpers -------------------------------------------------------------------------------------------------
    @staticmethod
    async def _ids(graph, inputs):
        list_id, _ = await list_of(graph, inputs.get("list"))
        return list_id, str(inputs["task_id"])

    async def _patched(self, call, graph, zone, body):
        list_id, task_id = await self._ids(graph, call.inputs)
        updated = await graph.patch(task_path(list_id, task_id), json=body)
        if not updated.get("id"):
            updated = await graph.get(task_path(list_id, task_id))
        return {**task_row(updated, list_id, zone), **told(zone)}, "success"

    @staticmethod
    def _fields(inputs: dict[str, Any], zone) -> dict[str, Any]:
        """What a create or update sets. A due date is its midnight in the person's zone, named; an empty due or
        reminder clears it."""
        body: dict[str, Any] = {}
        if "title" in inputs:
            body["title"] = str(inputs["title"]).strip()
        if "due" in inputs:
            body["dueDateTime"] = ({"dateTime": f"{day(inputs['due'], 'due').isoformat()}T00:00:00",
                                    "timeZone": zone.label} if str(inputs["due"] or "").strip() else None)
        if inputs.get("importance"):
            body["importance"] = str(inputs["importance"])
        if "note" in inputs:
            body["body"] = {"content": str(inputs["note"] or ""), "contentType": "text"}
        if "reminder" in inputs:
            if str(inputs["reminder"] or "").strip():
                when = moment(inputs["reminder"], zone, "reminder").astimezone(zone.info)
                body["reminderDateTime"] = {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": zone.label}
                body["isReminderOn"] = True
            else:
                body["isReminderOn"] = False
        return body
