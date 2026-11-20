"""Todoist's unified API v1 over the connected account, on the shared ``standard.http`` client.

Every path the agent calls is in this one class. ``Http`` carries the rest: each request off the agent's loop, reads
tried again on a dropped connection, a timeout, 429 and 5xx after the wait Todoist names, writes tried again only
on 429/503 (Todoist did nothing) and otherwise ``unknown``, and one set of failure kinds.

Todoist specifics:

- List endpoints answer ``{"results": [...], "next_cursor": ...}``; the completed-task endpoints say ``items`` and
  leave ``next_cursor`` out on the last page. A cursor is opaque and only good with the same parameters.
- ``limit`` is at most 200.
- An error body is ``{"error", "error_tag", "error_code", "http_code", "error_extra"}``; ``error_extra`` may say
  which argument was refused and how long to wait (``retry_after``).
- A write carries ``X-Request-Id``, the same on every attempt, so a write Todoist is asked twice is done once.
- No object carries a link: the web app's links are built here from the id.
"""

from __future__ import annotations

import uuid
from typing import Any

import requests

from standard.http import Http, segment

WEB_URL = "https://app.todoist.com/app"
#: Todoist's own ceiling per page.
PAGE_LIMIT = 200
#: Pages read before a "whole list" read stops. Two thousand projects, labels or completions in one answer is
#: past anything a person asks.
MAX_PAGES = 10


def task_link(task_id: str) -> str:
    return f"{WEB_URL}/task/{task_id}" if task_id else ""


def project_link(project_id: str) -> str:
    return f"{WEB_URL}/project/{project_id}" if project_id else ""


class Todoist(Http):
    SERVICE = "Todoist"
    BASE = "https://api.todoist.com/api/v1"
    # Todoist's own processing timeout is 15 seconds a request.
    READ_TIMEOUT = 25
    WRITE_TIMEOUT = 30

    def __init__(self, secret: dict[str, Any]):
        super().__init__(token=secret.get("access_token") or "", api_base_url=secret.get("api_base_url"))
        self.account = str(secret.get("account") or "")

    # -- Todoist's own words -------------------------------------------------------------------------------------
    def detail(self, response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return super().detail(response)
        if not isinstance(body, dict):
            return str(body)[:400]
        words = str(body.get("error") or body.get("error_tag") or body)[:300]
        extra = body.get("error_extra") if isinstance(body.get("error_extra"), dict) else {}
        said = [f"{key}: {extra[key]}" for key in ("argument", "expected") if extra.get(key)]
        return f"{words} ({'; '.join(said)})" if said else words

    @classmethod
    def retry_after(cls, response: requests.Response | None) -> float:
        """The header, or Todoist's ``error_extra.retry_after`` when only the body says it."""
        named = super().retry_after(response)
        if named or response is None:
            return named
        try:
            extra = (response.json() or {}).get("error_extra") or {}
            return max(0.0, float(extra.get("retry_after") or 0))
        except (ValueError, AttributeError, TypeError):
            return 0.0

    # -- transport -------------------------------------------------------------------------------------------------
    async def write(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """One write, with an X-Request-Id of its own that every attempt of it repeats."""
        return await self.request(method, path, json=body, headers={"X-Request-Id": str(uuid.uuid4())})

    async def every(self, path: str, params: dict[str, Any], key: str = "results",
                    pages: int = MAX_PAGES) -> tuple[list[dict[str, Any]], bool]:
        """Every page of a list endpoint, up to ``pages``: (rows, whether more were left unread)."""
        rows: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(pages):
            page = await self.get(path, params={**params, "limit": PAGE_LIMIT, **({"cursor": cursor} if cursor else {})})
            rows.extend(page.get(key) or [])
            cursor = str(page.get("next_cursor") or "")
            if not cursor:
                return rows, False
        return rows, True

    async def all(self, path: str, params: dict[str, Any], key: str = "results") -> list[dict[str, Any]]:
        return (await self.every(path, params, key))[0]

    # -- reads -----------------------------------------------------------------------------------------------------
    async def user(self) -> dict[str, Any]:
        return await self.get("user")

    async def projects(self) -> list[dict[str, Any]]:
        return await self.all("projects", {})

    async def sections(self, project_id: str = "") -> list[dict[str, Any]]:
        return await self.all("sections", {"project_id": project_id} if project_id else {})

    async def labels(self) -> list[dict[str, Any]]:
        return await self.all("labels", {})

    async def tasks_page(self, *, project_id: str = "", section_id: str = "", parent_id: str = "", label: str = "",
                         limit: int = 20, cursor: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        for name, value in (("project_id", project_id), ("section_id", section_id), ("parent_id", parent_id),
                            ("label", label), ("cursor", cursor)):
            if value:
                params[name] = value
        return await self.get("tasks", params=params)

    async def tasks_by_id(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """The open tasks among these ids, by id: a closed or deleted one is not among them."""
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), 100):
            rows = await self.all("tasks", {"ids": ",".join(ids[start:start + 100])})
            found.update({str(t.get("id")): t for t in rows})
        return found

    async def filter_page(self, query: str, limit: int = 20, cursor: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self.get("tasks/filter", params=params)

    async def task(self, task_id: str) -> dict[str, Any]:
        return await self.get(f"tasks/{segment(task_id)}")

    async def comment_count(self, task_id: str) -> int:
        # A v1 task's note_count is deprecated (always 0): the comments are counted.
        return len(await self.all("comments", {"task_id": task_id}))

    async def completed(self, since: str, until: str, project_id: str = "") -> tuple[list[dict[str, Any]], bool]:
        """Tasks completed from since (inclusive) to until (exclusive), at most three months apart. A recurring
        task's completed occurrences are not among them: it stays open, at its next date."""
        params: dict[str, Any] = {"since": since, "until": until}
        if project_id:
            params["project_id"] = project_id
        return await self.every("tasks/completed/by_completion_date", params, key="items")

    async def completions(self, date_from: str, date_to: str, project_id: str = "") -> tuple[list[dict[str, Any]], bool]:
        """Completion events of the activity log, from date_from (inclusive) to date_to (exclusive): one per
        completion, a recurring task's occurrences included. How far back depends on the person's plan.

        ``event_type=completed`` is a plain string; only tasks are completed, so it is every task completion.
        (``object_event_types`` is an array in the query, whose spelling there is not documented.)"""
        params: dict[str, Any] = {"event_type": "completed", "date_from": date_from, "date_to": date_to,
                                  "annotate_parents": "true"}
        if project_id:
            params["parent_project_id"] = project_id
        return await self.every("activities", params)

    # -- writes ----------------------------------------------------------------------------------------------------
    async def create_task(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.write("POST", "tasks", body)

    async def update_task(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.write("POST", f"tasks/{segment(task_id)}", body)

    async def move_task(self, task_id: str, where: dict[str, str]) -> dict[str, Any]:
        """Exactly one of project_id, section_id or parent_id."""
        return await self.write("POST", f"tasks/{segment(task_id)}/move", where)

    async def close_task(self, task_id: str) -> None:
        await self.write("POST", f"tasks/{segment(task_id)}/close")

    async def reopen_task(self, task_id: str) -> None:
        await self.write("POST", f"tasks/{segment(task_id)}/reopen")

    async def delete_task(self, task_id: str) -> None:
        await self.write("DELETE", f"tasks/{segment(task_id)}")

    async def create_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.write("POST", "projects", body)

    async def add_comment(self, task_id: str, content: str) -> dict[str, Any]:
        return await self.write("POST", "comments", {"task_id": task_id, "content": content})
