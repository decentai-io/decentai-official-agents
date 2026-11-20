"""Google Tasks: lists, and tasks read, created, edited, completed, reopened, moved and deleted.

- **Pages are Google's own.** A list is read a page at a time in the person's own order, with Google's page token
  handed back, so a list of any length is read whole, never cut at 1,000.
- **A due date is a date.** Google keeps only the day (the time is dropped), so ``due`` is ``YYYY-MM-DD`` both ways.
- **The agent's own edits are remembered** on the watches that cover the task (its id and the ``updated`` time Google
  gave the edit), so a watch never hands an edit made here back as news, and a two-way copy with Tasks can't loop.
"""

from __future__ import annotations

import json
from typing import Any

from standard import ResourceDenied, ToolBase
from standard.google import Google, segment
from standard.watch import active

from .common import Refused, answers, client, clip, day

ROW_NOTE_CHARS = 300
OWN_MOST = 100


def task_row(task: dict[str, Any], list_id: str, note_chars: int = ROW_NOTE_CHARS) -> dict[str, Any]:
    row = {"task_id": str(task.get("id") or ""), "list_id": str(list_id), "title": str(task.get("title") or ""),
           "status": str(task.get("status") or ""), "updated": str(task.get("updated") or "")}
    for key, value in (("due", str(task.get("due") or "")[:10]), ("completed", str(task.get("completed") or "")),
                       ("parent", str(task.get("parent") or "")), ("link", str(task.get("webViewLink") or "")),
                       ("note", clip(task.get("notes"), note_chars))):
        if value:
            row[key] = value
    if task.get("deleted"):
        row["deleted"] = True
    return row


def due_value(text: Any) -> str | None:
    """Google's due for a date (midnight UTC; Google keeps the day only), or None to clear it."""
    raw = str(text or "").strip()
    return f"{day(raw, 'due').isoformat()}T00:00:00.000Z" if raw else None


def tasks_path(list_id: str, task_id: str = "") -> str:
    return f"lists/{segment(list_id)}/tasks" + (f"/{segment(task_id)}" if task_id else "")


async def real_list(google: Google, wanted: Any) -> str:
    """The list asked for, or the account's default one, by its real id: a row never carries "@default"."""
    wanted = str(wanted or "").strip()
    if wanted and wanted != "@default":
        return wanted
    return str((await google.get("users/@me/lists/@default")).get("id") or "")


async def remember(call: Any, google: Google, list_id: str, task: dict[str, Any] | None, task_id: str = "") -> None:
    """This agent's own edit, written on every active watch of this account that covers the list: its watch then
    passes over it. A watch it can't be written on reports the edit, which is twice, never lost."""
    task_id = str((task or {}).get("id") or task_id)
    stamp = str((task or {}).get("updated") or "deleted")
    try:
        watches = active(await call.records.list("watch", {"account": google.account}))
        for watch in watches:
            fields = watch["fields"]
            if fields.get("list_id") and fields["list_id"] != list_id:
                continue
            try:
                own = json.loads(str(fields.get("own") or "{}"))
            except ValueError:
                own = {}
            own.pop(task_id, None)
            own[task_id] = stamp
            own = dict(list(own.items())[-OWN_MOST:])
            await call.records.update("watch", watch["ref"], {"own": json.dumps(own, separators=(",", ":"))})
    except ResourceDenied:
        pass


class ListsTool(ToolBase):
    id = "lists"

    @answers
    async def list(self, call):
        google = await client(call, "tasks")
        found = await google.token_pages("users/@me/lists", params={"maxResults": 1000})
        default = str((await google.get("users/@me/lists/@default")).get("id") or "")
        rows = [{"list_id": str(r.get("id") or ""), "name": str(r.get("title") or ""),
                 "updated": str(r.get("updated") or ""), "default": str(r.get("id") or "") == default}
                for r in found]
        rows.sort(key=lambda r: not r["default"])
        return {"account": google.account, "lists": rows}, "success"

    @answers
    async def create(self, call):
        google = await client(call, "tasks")
        made = await google.post("users/@me/lists", json={"title": str(call.inputs["name"]).strip()})
        return {"list_id": str(made.get("id") or ""), "name": str(made.get("title") or ""),
                "updated": str(made.get("updated") or "")}, "success"


class TasksTool(ToolBase):
    id = "tasks"

    @answers
    async def list(self, call):
        google = await client(call, "tasks")
        inputs = call.inputs
        list_id = await real_list(google, inputs.get("list_id"))
        status = str(inputs.get("status") or "open")
        params: dict[str, Any] = {"maxResults": int(inputs.get("max_results") or 50)}
        if status == "open":
            params.update(showCompleted="false")
        else:
            # A task ticked off in Google's own apps is hidden as well as completed.
            params.update(showCompleted="true", showHidden="true")
            if status == "completed":
                params["completedMin"] = "1970-01-01T00:00:00.000Z"
        due_before = str(inputs.get("due_before") or "").strip()
        if due_before:
            params["dueMax"] = due_value(due_before)
        if inputs.get("page_token"):
            params["pageToken"] = str(inputs["page_token"])
        page = await google.get(tasks_path(list_id), params=params)
        rows = [t for t in page.get("items") or [] if not t.get("deleted")]
        if status == "open":
            rows = [t for t in rows if t.get("status") != "completed"]
        elif status == "completed":
            rows = [t for t in rows if t.get("status") == "completed"]
        if due_before:
            rows = [t for t in rows if t.get("due") and str(t["due"])[:10] < due_before]
        result = {"account": google.account, "list_id": list_id, "tasks": [task_row(t, list_id) for t in rows]}
        if page.get("nextPageToken"):
            result["next_page_token"] = str(page["nextPageToken"])
        return result, "success"

    @answers
    async def get(self, call):
        google = await client(call, "tasks")
        list_id = await real_list(google, call.inputs.get("list_id"))
        task_id = str(call.inputs["task_id"])
        most = int(call.inputs.get("max_note_chars") or 2000)
        task = await google.get(tasks_path(list_id, task_id))
        every = await google.token_pages(tasks_path(list_id), params={
            "maxResults": 100, "showCompleted": "true", "showHidden": "true"})
        children = sorted((t for t in every if t.get("parent") == task_id and not t.get("deleted")),
                          key=lambda t: str(t.get("position") or ""))
        row = task_row(task, list_id, note_chars=most)
        row["note"] = row.get("note", "")
        row["note_truncated"] = len(str(task.get("notes") or "").strip()) > most
        row["subtasks"] = [{"task_id": str(t.get("id") or ""), "title": str(t.get("title") or ""),
                            "status": str(t.get("status") or "")} for t in children]
        return row, "success"

    # -- changes -------------------------------------------------------------------------------------------------
    @staticmethod
    def fields(inputs: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if inputs.get("title") is not None:
            if not str(inputs["title"]).strip():
                raise Refused("A task's title can't be empty.")
            body["title"] = str(inputs["title"]).strip()
        if "due" in inputs:
            body["due"] = due_value(inputs["due"])
        if inputs.get("note") is not None:
            body["notes"] = str(inputs["note"])
        return body

    async def changed(self, call, google: Google, list_id: str, task: dict[str, Any]):
        await remember(call, google, list_id, task)
        return {"account": google.account, **task_row(task, list_id)}, "success"

    @answers
    async def create(self, call):
        google = await client(call, "tasks")
        inputs = call.inputs
        body = {k: v for k, v in self.fields(inputs).items() if v is not None}
        list_id = await real_list(google, inputs.get("list_id"))
        params = {k: str(inputs[k]) for k in ("parent", "previous") if inputs.get(k)}
        made = await google.post(tasks_path(list_id), params=params or None, json=body)
        return await self.changed(call, google, list_id, made)

    async def patched(self, call, body: dict[str, Any]):
        google = await client(call, "tasks")
        list_id = await real_list(google, call.inputs.get("list_id"))
        task = await google.patch(tasks_path(list_id, str(call.inputs["task_id"])), json=body)
        return await self.changed(call, google, list_id, task)

    @answers
    async def update(self, call):
        body = self.fields(call.inputs)
        if not body:
            raise Refused("Nothing to change: give a title, due or note.")
        return await self.patched(call, body)

    @answers
    async def complete(self, call):
        # Google stamps "completed" itself when the status turns.
        return await self.patched(call, {"status": "completed"})

    @answers
    async def reopen(self, call):
        # The completion time is cleared with the status, or the task would read as open and completed at once.
        return await self.patched(call, {"status": "needsAction", "completed": None})

    @answers
    async def move(self, call):
        google = await client(call, "tasks")
        inputs = call.inputs
        list_id = await real_list(google, inputs.get("list_id"))
        params = {k: str(inputs[v]) for k, v in (("parent", "parent"), ("previous", "previous"),
                                                 ("destinationTasklist", "to_list_id")) if inputs.get(v)}
        if not params and not inputs.get("to_top"):
            raise Refused("Say where: to_list_id, parent, previous, or to_top to make it a top-level task first.")
        task = await google.post(f"{tasks_path(list_id, str(inputs['task_id']))}/move", params=params or None)
        landed = str(inputs.get("to_list_id") or list_id)
        if landed != list_id:
            await remember(call, google, list_id, None, str(inputs["task_id"]))
        return await self.changed(call, google, landed, task)

    @answers
    async def delete(self, call):
        google = await client(call, "tasks")
        list_id = await real_list(google, call.inputs.get("list_id"))
        task_id = str(call.inputs["task_id"])
        task = await google.get(tasks_path(list_id, task_id))
        await google.delete(tasks_path(list_id, task_id))
        await remember(call, google, list_id, None, task_id)
        return {"deleted": True, "task_id": task_id, "title": str(task.get("title") or "")}, "success"
