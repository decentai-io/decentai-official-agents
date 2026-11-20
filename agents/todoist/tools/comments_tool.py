"""A comment on a task: visible to everyone the project is shared with, which is why adding one is level 3."""

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, failure, not_connected
from .todoist_api import task_link


class CommentsTool(ToolBase):
    id = "comments"

    async def add(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        task_id = str(call.inputs["task_id"])
        try:
            task = await client.task(task_id)
            comment = await client.add_comment(task_id, str(call.inputs["content"]))
        except HttpError as exc:
            return failure(exc)
        if not comment.get("id"):
            return {"error": "Todoist accepted the comment but returned no id; the outcome is unknown.",
                    "kind": "unknown"}, "error"
        return {"comment_id": str(comment["id"]), "task_id": task_id, "task": str(task.get("content") or ""),
                "posted_at": str(comment.get("posted_at") or ""), "link": task_link(task_id)}, "success"
