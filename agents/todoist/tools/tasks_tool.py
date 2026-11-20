"""Tasks in the person's Todoist: read, filed, changed, moved, ticked off; and their sub-tasks."""

from datetime import date

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, failure, not_connected
from .catalog import API_PRIORITY, Catalog, task_row


def refused(words):
    return {"error": words, "kind": "invalid"}, "error"


class TasksTool(ToolBase):
    id = "tasks"

    # -- reads -----------------------------------------------------------------------------------------------------
    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        inputs = call.inputs
        query = str(inputs.get("query") or "").strip()
        parent_id = str(inputs.get("parent_id") or "").strip()
        narrowed = any(inputs.get(k) for k in ("project", "section", "label"))
        if query and (narrowed or parent_id):
            # Todoist's filter endpoint takes nothing but the query; say it in the query (#Project, /Section,
            # @label) instead.
            return refused("Give either a filter query or project/section/label/parent_id, not both: a query can "
                           "say '#Office Move & @calls'.")
        if parent_id and narrowed:
            return refused("parent_id lists one task's sub-tasks: give it alone.")
        most = int(inputs.get("max_results") or 20)
        cursor = str(inputs.get("cursor") or "")
        catalog = Catalog(client)
        try:
            if query:
                page = await client.filter_page(query, most, cursor)
            elif parent_id:
                await client.task(parent_id)  # an unknown parent is not_found, not an empty list
                page = await client.tasks_page(parent_id=parent_id, limit=most, cursor=cursor)
            else:
                project_id = section_id = label = ""
                if inputs.get("project"):
                    project, problem = await catalog.project(inputs["project"])
                    if problem:
                        return problem, "error"
                    project_id = str(project["id"])
                if inputs.get("section"):
                    section, problem = await catalog.section(inputs["section"], project_id)
                    if problem:
                        return problem, "error"
                    section_id = str(section["id"])
                if inputs.get("label"):
                    labels, problem = await catalog.label_names([inputs["label"]])
                    if problem:
                        return problem, "error"
                    label = labels[0]
                page = await client.tasks_page(project_id=project_id, section_id=section_id, label=label,
                                               limit=most, cursor=cursor)
            names = await catalog.project_names()
        except HttpError as exc:
            return failure(exc)
        rows = [task_row(t, names) for t in (page.get("results") or [])[:most]]
        next_cursor = str(page.get("next_cursor") or "")
        return {"tasks": rows, "more": bool(next_cursor), "next_cursor": next_cursor}, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        task_id = str(call.inputs["task_id"])
        try:
            task = await client.task(task_id)
            comments = await client.comment_count(task_id)
            subtasks = await client.all("tasks", {"parent_id": task_id})
            names = await Catalog(client).project_names()
        except HttpError as exc:
            return failure(exc)
        return {**task_row(task, names),
                "description": str(task.get("description") or ""),
                "completed": bool(task.get("checked")),
                "added_at": str(task.get("added_at") or ""),
                "comment_count": comments,
                "subtasks": [{"task_id": str(s.get("id") or ""), "content": str(s.get("content") or "")}
                             for s in subtasks]}, "success"

    # -- writes ----------------------------------------------------------------------------------------------------
    @staticmethod
    async def _fields(inputs, catalog):
        """The fields create and update share, checked before anything is sent: a due date that is a date, a
        priority the app knows, labels that exist."""
        body = {}
        for field in ("content", "description"):
            if inputs.get(field) is not None and str(inputs.get(field)) != "":
                body[field] = str(inputs[field])
        if inputs.get("due_string") and inputs.get("due_date"):
            return None, {"error": "Give due_string or due_date, not both.", "kind": "invalid"}
        if inputs.get("due_string"):
            body["due_string"] = str(inputs["due_string"])
        if inputs.get("due_date"):
            try:
                body["due_date"] = date.fromisoformat(str(inputs["due_date"])).isoformat()
            except ValueError:
                return None, {"error": "due_date must be YYYY-MM-DD; use due_string for words like 'next Monday'.",
                              "kind": "invalid"}
        if inputs.get("priority"):
            body["priority"] = API_PRIORITY[str(inputs["priority"]).upper()]
        if inputs.get("labels") is not None:
            labels, problem = await catalog.label_names(list(inputs["labels"]))
            if problem:
                return None, problem
            body["labels"] = labels
        return body, None

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        inputs = call.inputs
        parent_id = str(inputs.get("parent_id") or "").strip()
        if parent_id and (inputs.get("project") or inputs.get("section")):
            return refused("A sub-task goes where its parent is: give parent_id alone, without project or section.")
        catalog = Catalog(client)
        try:
            body, problem = await self._fields(inputs, catalog)
            if problem:
                return problem, "error"
            if parent_id:
                parent = await client.task(parent_id)
                if parent.get("checked"):
                    return refused(f"'{parent.get('content')}' is completed: reopen it before adding a sub-task.")
                body["parent_id"] = parent_id
            project_id = ""
            if inputs.get("project"):
                project, problem = await catalog.project(inputs["project"])
                if problem:
                    return problem, "error"
                project_id = body["project_id"] = str(project["id"])
            if inputs.get("section"):
                section, problem = await catalog.section(inputs["section"], project_id)
                if problem:
                    return problem, "error"
                body["section_id"] = str(section["id"])
                body["project_id"] = str(section.get("project_id") or project_id)
            await call.progress(f"Adding '{body['content']}' to Todoist")
            created = await client.create_task(body)
        except HttpError as exc:
            return failure(exc)
        if not created.get("id"):
            return {"error": "Todoist accepted the task but returned no id; the outcome is unknown.",
                    "kind": "unknown"}, "error"
        try:
            names = await catalog.project_names()
        except HttpError:
            names = {}   # the task exists; a missing project name is not a failure
        return task_row(created, names), "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        catalog = Catalog(client)
        try:
            body, problem = await self._fields(call.inputs, catalog)
            if problem:
                return problem, "error"
            if not body:
                return refused("Nothing to change: give content, description, due_string, due_date, priority or "
                               "labels.")
            updated = await client.update_task(str(call.inputs["task_id"]), body)
        except HttpError as exc:
            return failure(exc)
        try:
            names = await catalog.project_names()
        except HttpError:
            names = {}
        return task_row(updated, names), "success"

    async def move(self, call):
        """To another project, a section, or under another task: exactly one of the three."""
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        inputs = call.inputs
        given = [k for k in ("project", "section", "parent_id") if str(inputs.get(k) or "").strip()]
        if len(given) != 1:
            return refused("Move a task to exactly one of: project, section, or parent_id (under another task).")
        task_id = str(inputs["task_id"])
        catalog = Catalog(client)
        try:
            task = await client.task(task_id)
            if given == ["project"]:
                project, problem = await catalog.project(inputs["project"])
                if problem:
                    return problem, "error"
                where = {"project_id": str(project["id"])}
            elif given == ["section"]:
                # Named alone, a section is looked for in every project; "Furniture" in two projects is refused
                # with both listed.
                within = str(inputs.get("section_project") or "").strip()
                project_id = ""
                if within:
                    project, problem = await catalog.project(within)
                    if problem:
                        return problem, "error"
                    project_id = str(project["id"])
                section, problem = await catalog.section(inputs["section"], project_id)
                if problem:
                    return problem, "error"
                where = {"section_id": str(section["id"])}
            else:
                parent_id = str(inputs["parent_id"]).strip()
                if parent_id == task_id:
                    return refused("A task cannot be its own parent.")
                await client.task(parent_id)
                where = {"parent_id": parent_id}
            await call.progress(f"Moving '{task.get('content')}'")
            moved = await client.move_task(task_id, where)
            if not moved.get("id"):
                moved = await client.task(task_id)
            names = await catalog.project_names()
        except HttpError as exc:
            return failure(exc)
        return task_row(moved, names), "success"

    async def complete(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        task_id = str(call.inputs["task_id"])
        try:
            task = await client.task(task_id)
            if task.get("checked"):
                return {"task_id": task_id, "content": str(task.get("content") or ""), "completed": True,
                        "already": True, "recurring": False}, "success"
            await client.close_task(task_id)
        except HttpError as exc:
            return failure(exc)
        return {"task_id": task_id, "content": str(task.get("content") or ""), "completed": True, "already": False,
                # Closing a recurring task moves it to its next date rather than finishing it; the assistant says so.
                "recurring": bool((task.get("due") or {}).get("is_recurring"))}, "success"

    async def reopen(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        task_id = str(call.inputs["task_id"])
        try:
            await client.reopen_task(task_id)
        except HttpError as exc:
            return failure(exc)
        return {"task_id": task_id, "reopened": True}, "success"

    async def delete(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        task_id = str(call.inputs["task_id"])
        try:
            # Read first: the answer names what was deleted, and an unknown id is reported as one.
            task = await client.task(task_id)
            await client.delete_task(task_id)
        except HttpError as exc:
            return failure(exc)
        return {"task_id": task_id, "content": str(task.get("content") or ""), "deleted": True}, "success"
