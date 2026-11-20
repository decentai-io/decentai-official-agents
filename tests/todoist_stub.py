"""A loopback Todoist: the subset of the unified API v1 the Todoist agent calls, over real HTTP, holding a
fictional task list for Sidra Office Supplies. As strict as Todoist's OpenAPI description where the agent depends
on it:

- List endpoints answer ``results`` and ``next_cursor`` (null at the end); the completed endpoint answers ``items``
  and leaves ``next_cursor`` out at the end. A cursor is opaque ("<a>.<b>"), good only with the same parameters,
  and ``limit`` is 1..200; anything else is 400 INVALID_ARGUMENT_VALUE.
- ``tasks/completed/by_completion_date``: since inclusive, until exclusive, at most three months; a recurring
  task's completed occurrences are not there (it stays open at its next date).
- ``activities``: ``object_event_types`` is a JSON list; date_from inclusive, date_to exclusive; newest first; one
  event per completion, recurring ones included; 403 when the plan has no activity log.
- ``tasks/{id}/move`` takes exactly one of project_id, section_id, parent_id; the sub-tasks move with it. A task's
  update never moves it.
- A write's ``X-Request-Id`` seen before is answered as it was, and not done again.
- ``rate_limit`` answers that many requests with 429, ``Retry-After`` and ``error_extra.retry_after``.
- Errors are ``{error, error_tag, error_code, http_code, error_extra}``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from tests.conftest import stands_for

TOKEN = "tk-1"


def _moment(text: str) -> datetime:
    text = str(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    """Todoist's own spelling of a moment."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TodoistStub:
    ACCOUNT = "sam@sidra.example"
    TIMEZONE = "Asia/Dubai"

    def __init__(self, token: str = TOKEN, account: str = ACCOUNT):
        self.token, self.account = token, account
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.sections: Dict[str, Dict[str, Any]] = {}
        self.labels: Dict[str, Dict[str, Any]] = {}
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.comments: Dict[str, Dict[str, Any]] = {}
        self.writes: List[tuple] = []            # (method, path, body) of every write done
        self.completed_queries: List[Dict[str, str]] = []
        self.activity_queries: List[Dict[str, str]] = []
        self.events: List[Dict[str, Any]] = []
        self.request_ids: List[str] = []         # of every write asked, done or answered again
        self.answered: Dict[str, tuple] = {}
        self.activity_log = True                 # the plan has an activity log
        self.rate_limit = 0                      # answer this many requests with 429
        self.busy_after_write = False            # do the next write, then answer 503 as if it had not been
        self.requests = 0
        self._n = 1000
        self._event = 9000
        self._server: Optional[ThreadingHTTPServer] = None

    # -- seeding -----------------------------------------------------------------------------------------------
    def _id(self) -> str:
        self._n += 1
        return f"6X{self._n}"

    def add_project(self, name: str, shared: bool = False, inbox: bool = False,
                    parent_id: Optional[str] = None) -> str:
        project_id = self._id()
        self.projects[project_id] = {
            "id": project_id, "name": name, "parent_id": parent_id,
            "is_shared": shared, "inbox_project": inbox, "is_archived": False,
            "is_deleted": False, "color": "charcoal", "child_order": len(self.projects),
        }
        return project_id

    def delete_project(self, project_id: str) -> None:
        """A project deleted in the app, with its tasks."""
        del self.projects[project_id]
        for task in self.tasks.values():
            if task["project_id"] == project_id:
                task["is_deleted"] = True

    def add_section(self, project_id: str, name: str) -> str:
        section_id = self._id()
        self.sections[section_id] = {"id": section_id, "project_id": project_id,
                                     "name": name, "is_archived": False}
        return section_id

    def add_label(self, name: str) -> str:
        label_id = self._id()
        self.labels[label_id] = {"id": label_id, "name": name, "color": "blue"}
        return label_id

    def add_task(self, content: str, project_id: str, *, section_id: Optional[str] = None,
                 labels=None, priority: int = 1, due_date: Optional[str] = None,
                 due_string: Optional[str] = None, recurring: bool = False,
                 description: str = "", parent_id: Optional[str] = None) -> str:
        task_id = self._id()
        due = None
        if due_date:
            due = {"date": due_date, "string": due_string or due_date,
                   "is_recurring": recurring, "lang": "en", "timezone": None}
        self.tasks[task_id] = {
            "id": task_id, "user_id": "u1", "project_id": project_id,
            "section_id": section_id, "parent_id": parent_id, "labels": list(labels or []),
            "priority": priority, "due": due, "content": content,
            "description": description, "checked": False, "is_deleted": False,
            "added_at": "2026-09-01T08:00:00.000000Z", "completed_at": None,
            "child_order": len(self.tasks), "completed_count": 0, "note_count": 0,
        }
        return task_id

    def complete_at(self, task_id: str, moment: datetime) -> None:
        """A task ticked off in the Todoist app at that moment. A recurring one moves to its next date and stays
        open, as Todoist does: only the activity log keeps that completion."""
        task = self.tasks[task_id]
        self._event += 1
        self.events.append({"id": self._event, "object_type": "item", "object_id": task_id,
                            "event_type": "completed", "event_date": stamp(moment),
                            "parent_project_id": task["project_id"], "parent_item_id": None,
                            "initiator_id": None, "extra_data_id": self._event,
                            "extra_data": {"content": task["content"], "client": "Todoist for Web"}})
        task["completed_count"] = int(task.get("completed_count") or 0) + 1
        due = task.get("due") or {}
        if due.get("is_recurring"):
            due["date"] = (date.fromisoformat(due["date"][:10]) + timedelta(days=7)).isoformat()
            return
        task["checked"] = True
        task["completed_at"] = stamp(moment)

    # -- server ------------------------------------------------------------------------------------------------
    def start(self) -> "TodoistStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            # -- answers -----------------------------------------------------------------------------------
            def _send(self, status: int, body: Any, headers=None) -> None:
                raw = b"" if body is None else json.dumps(body).encode("utf-8")
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _done(self, status: int, body: Any) -> None:
                """A success. A write's is kept under its request id, so the same request is answered again,
                not done again."""
                request_id = self.headers.get("X-Request-Id") or ""
                if self.command in ("POST", "DELETE") and request_id:
                    stub.answered.setdefault(request_id, (status, body))
                    if stub.busy_after_write:
                        stub.busy_after_write = False
                        return self._error(503, "Service unavailable")
                self._send(status, body)

            def _json(self, status: int, body: Any) -> None:
                self._done(status, body)

            def _empty(self) -> None:
                self._done(204, None)

            def _error(self, status: int, message: str, extra=None, headers=None) -> None:
                tags = {400: "INVALID_ARGUMENT_VALUE", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                        429: "TOO_MANY_REQUESTS", 503: "SERVICE_UNAVAILABLE"}
                self._send(status, {"error": message, "error_tag": tags.get(status, "ERROR"),
                                    "error_code": {401: 477, 400: 20}.get(status, status), "http_code": status,
                                    "error_extra": dict(extra or {})}, headers)

            def _refused(self, argument: str, expected: str = "") -> None:
                self._error(400, "Invalid argument value",
                            {"argument": argument, **({"expected": expected} if expected else {})})

            # -- every request -----------------------------------------------------------------------------
            def _gate(self) -> bool:
                stub.requests += 1
                if stub.rate_limit > 0:
                    stub.rate_limit -= 1
                    self._error(429, "Too many requests", {"retry_after": 1}, {"Retry-After": "1"})
                    return False
                if self.headers.get("Authorization") != f"Bearer {stub.token}":
                    self._error(401, "Unauthorized", {"retry_after": 3})
                    return False
                return True

            def _replayed(self) -> bool:
                """A write whose request id was answered before: answered as it was, not done again."""
                request_id = self.headers.get("X-Request-Id") or ""
                stub.request_ids.append(request_id)
                if request_id and request_id in stub.answered:
                    self._send(*stub.answered[request_id])
                    return True
                return False

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                return json.loads(raw or b"{}")

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                path = url.path
                if not path.startswith("/api/v1/"):
                    # Only the unified API v1: the retired REST v2 and Sync v9 paths answer 410 Gone.
                    self.multi = {}
                    return None, {}
                self.multi = urllib.parse.parse_qs(url.query)
                return path[len("/api/v1/"):], {k: v[0] for k, v in self.multi.items()}

            def _page(self, rows, query, key="results"):
                try:
                    limit = int(query.get("limit") or 50)
                except ValueError:
                    return self._refused("limit")
                if not 1 <= limit <= 200:
                    return self._refused("limit", "Input should be less than or equal to 200")
                # A cursor is opaque, and good only with the parameters it was made for.
                same = {k: v for k, v in query.items() if k not in ("cursor", "limit")}
                signature = hashlib.sha256(json.dumps(same, sort_keys=True).encode()).hexdigest()[:12]
                offset = 0
                if "cursor" in query:
                    head, _, tail = str(query["cursor"]).partition(".")
                    if not head.startswith("c") or not head[1:].isdigit() or tail != signature:
                        return self._refused("cursor")
                    offset = int(head[1:])
                page = rows[offset: offset + limit]
                cursor = f"c{offset + limit}.{signature}" if offset + limit < len(rows) else None
                if key == "items":
                    # The completed endpoint leaves next_cursor out on its last page.
                    return self._send(200, {key: page, **({"next_cursor": cursor} if cursor else {})})
                self._send(200, {key: page, "next_cursor": cursor})

            def _open_task(self, task_id):
                task = stub.tasks.get(task_id)
                if task is None or task["is_deleted"]:
                    self._error(404, "Task not found")
                    return None
                return task

            # -- reads ---------------------------------------------------------------------------------------
            def do_GET(self):  # noqa: N802
                if not self._gate():
                    return
                path, query = self._route()
                if path is None:
                    return self._error(410, "This API version is gone: use /api/v1")
                if path == "user":
                    return self._send(200, {"id": "u1", "email": stub.account, "full_name": "Sam Haddad",
                                            "tz_info": {"timezone": stub.TIMEZONE, "gmt_string": "+04:00",
                                                        "hours": 4, "minutes": 0, "is_dst": 0}})
                if path == "projects":
                    return self._page(list(stub.projects.values()), query)
                if path == "sections":
                    rows = [s for s in stub.sections.values()
                            if not query.get("project_id") or s["project_id"] == query["project_id"]]
                    return self._page(rows, query)
                if path == "labels":
                    return self._page(list(stub.labels.values()), query)
                if path == "tasks":
                    ids = set(query["ids"].split(",")) if query.get("ids") else None
                    rows = [t for t in stub.tasks.values()
                            if not t["checked"] and not t["is_deleted"]
                            and (ids is None or t["id"] in ids)
                            and (not query.get("project_id") or t["project_id"] == query["project_id"])
                            and (not query.get("section_id") or t["section_id"] == query["section_id"])
                            and (not query.get("parent_id") or t["parent_id"] == query["parent_id"])
                            and (not query.get("label") or query["label"] in t["labels"])]
                    return self._page(rows, query)
                if path == "tasks/filter":
                    matcher = stub._filter(query.get("query") or "")
                    if matcher is None:
                        return self._refused("query")
                    rows = [t for t in stub.tasks.values()
                            if not t["checked"] and not t["is_deleted"] and matcher(t)]
                    return self._page(rows, query)
                if path == "tasks/completed/by_completion_date":
                    stub.completed_queries.append(dict(query))
                    try:
                        since, until = _moment(query["since"]), _moment(query["until"])
                    except (KeyError, ValueError):
                        return self._refused("since", "since and until are required date-times")
                    if until <= since or until - since > timedelta(days=92):
                        return self._refused("until", "The range must be at most 3 months")
                    if query.get("project_id") and query["project_id"] not in stub.projects:
                        return self._error(404, "Project not found")
                    rows = [t for t in stub.tasks.values()
                            if t["checked"] and not t["is_deleted"]
                            and since <= _moment(t["completed_at"]) < until
                            and (not query.get("project_id") or t["project_id"] == query["project_id"])]
                    rows.sort(key=lambda t: t["completed_at"], reverse=True)
                    return self._page(rows, query, key="items")
                if path == "activities":
                    return self._activities(query)
                if path.startswith("tasks/"):
                    task = self._open_task(path.split("/")[1])
                    if task is not None:
                        self._send(200, task)
                    return
                if path == "comments":
                    if bool(query.get("task_id")) == bool(query.get("project_id")):
                        return self._refused("task_id", "Exactly one of task_id or project_id")
                    rows = [c for c in stub.comments.values() if c["task_id"] == query.get("task_id")]
                    return self._page(rows, query)
                self._error(404, f"no route {path}")

            def _activities(self, query):
                stub.activity_queries.append(dict(query))
                if not stub.activity_log:
                    return self._error(403, "Activity log is not available on the current plan")
                # object_event_types is an array of "object:event" strings, one query value each; a JSON text in
                # one value is not a type it knows. event_type is one plain event type (or "!type").
                wanted = list(self.multi.get("object_event_types") or [])
                if any(":" not in w or w.startswith("[") for w in wanted):
                    return self._refused("object_event_types")
                event_type = str(query.get("event_type") or "")
                valid = {"added", "deleted", "updated", "archived", "unarchived", "completed", "uncompleted",
                         "shared", "left", "moved"}
                if event_type and event_type.lstrip("!") not in valid:
                    return self._refused("event_type")
                if event_type:
                    wanted.append(f":{event_type}") if not event_type.startswith("!") else None
                if set(query) - {"object_type", "object_id", "parent_project_id", "parent_item_id", "event_type",
                                 "object_event_types", "annotate_notes", "annotate_parents", "cursor", "limit",
                                 "date_from", "date_to", "initiator_id", "workspace_id", "include_parent_object",
                                 "include_child_objects", "initiator_id_null"}:
                    return self._refused(sorted(set(query))[0])
                try:
                    start = _moment(query["date_from"]) if query.get("date_from") else None
                    end = _moment(query["date_to"]) if query.get("date_to") else None
                except ValueError:
                    return self._refused("date_from")
                project = query.get("parent_project_id")
                if project and project not in stub.projects:
                    return self._error(404, "Project not found")

                def fits(event):
                    kinds = {f"{event['object_type']}:{event['event_type']}", f"{event['object_type']}:",
                             f":{event['event_type']}"}
                    moment = _moment(event["event_date"])
                    return ((not wanted or kinds & set(wanted)) and (start is None or moment >= start)
                            and (end is None or moment < end)
                            and (not project or event["parent_project_id"] == project))
                rows = sorted((e for e in stub.events if fits(e)), key=lambda e: (e["event_date"], e["id"]),
                              reverse=True)
                return self._page(rows, query)

            # -- writes --------------------------------------------------------------------------------------
            def do_POST(self):  # noqa: N802
                if not self._gate():
                    return
                path, _ = self._route()
                if path is None:
                    return self._error(410, "This API version is gone: use /api/v1")
                body = self._payload()
                if self._replayed():
                    return
                stub.writes.append(("POST", path, body))
                if path == "tasks":
                    return stub._create_task(self, body)
                if path == "projects":
                    project_id = stub.add_project(body["name"], parent_id=body.get("parent_id"))
                    return self._json(200, stub.projects[project_id])
                if path == "comments":
                    if self._open_task(body.get("task_id")) is None:
                        return
                    comment_id = stub._id()
                    stub.comments[comment_id] = {
                        "id": comment_id, "task_id": body["task_id"], "content": body["content"],
                        "posted_at": "2026-09-13T08:00:00.000000Z"}
                    return self._json(200, stub.comments[comment_id])
                parts = path.split("/")
                if len(parts) >= 2 and parts[0] == "tasks":
                    task = self._open_task(parts[1])
                    if task is None:
                        return
                    if len(parts) == 3 and parts[2] == "close":
                        stub.complete_at(task["id"], datetime.now(timezone.utc))
                        return self._empty()
                    if len(parts) == 3 and parts[2] == "reopen":
                        task["checked"], task["completed_at"] = False, None
                        return self._empty()
                    if len(parts) == 3 and parts[2] == "move":
                        return stub._move_task(self, task, body)
                    if len(parts) == 2:
                        return stub._update_task(self, task, body)
                self._error(404, f"no route {path}")

            def do_DELETE(self):  # noqa: N802
                if not self._gate():
                    return
                path, _ = self._route()
                if path is None:
                    return self._error(410, "This API version is gone: use /api/v1")
                if self._replayed():
                    return
                stub.writes.append(("DELETE", path, {}))
                task = self._open_task(path.split("/")[-1])
                if task is None:
                    return
                task["is_deleted"] = True
                self._empty()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    # -- behaviour ---------------------------------------------------------------------------------------------
    def _due(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if body.get("due_date"):
            return {"date": body["due_date"], "string": body["due_date"],
                    "is_recurring": False, "lang": "en", "timezone": None}
        words = str(body.get("due_string") or "")
        if words == "no date":
            return None
        today = date.today()
        when = {"today": today, "tomorrow": today + timedelta(days=1)}.get(words, today)
        return {"date": when.isoformat(), "string": words,
                "is_recurring": words.startswith("every"), "lang": "en", "timezone": None}

    def _check_labels(self, handler, labels) -> bool:
        # Real Todoist would create an unknown personal label silently; the stub refuses so a test notices the
        # agent let one through.
        known = {l["name"] for l in self.labels.values()}
        if any(name not in known for name in labels or []):
            handler._refused("labels", "stub: unknown label sent")
            return False
        return True

    def _create_task(self, handler, body):
        parent = None
        if body.get("parent_id"):
            parent = self.tasks.get(body["parent_id"])
            if parent is None or parent["is_deleted"]:
                return handler._refused("parent_id")
        project_id = body.get("project_id") or (parent or {}).get("project_id") or next(
            p["id"] for p in self.projects.values() if p["inbox_project"])
        if project_id not in self.projects:
            return handler._refused("project_id")
        if parent is not None and parent["project_id"] != project_id:
            return handler._refused("parent_id", "The parent is in another project")
        if not self._check_labels(handler, body.get("labels")):
            return
        task_id = self.add_task(body["content"], project_id,
                                section_id=body.get("section_id") or (parent or {}).get("section_id"),
                                labels=body.get("labels"), priority=body.get("priority", 1),
                                description=body.get("description", ""), parent_id=body.get("parent_id"))
        if body.get("due_string") or body.get("due_date"):
            self.tasks[task_id]["due"] = self._due(body)
        handler._json(200, self.tasks[task_id])

    def _move_task(self, handler, task, body):
        given = [k for k in ("project_id", "section_id", "parent_id") if body.get(k) is not None]
        if len(given) != 1 or set(body) - {"project_id", "section_id", "parent_id"}:
            return handler._refused("project_id", "Exactly one of project_id, section_id or parent_id")
        key, value = given[0], body[given[0]]
        if key == "project_id":
            if value not in self.projects:
                return handler._error(404, "Project not found")
            where = {"project_id": value, "section_id": None, "parent_id": None}
        elif key == "section_id":
            if value not in self.sections:
                return handler._error(404, "Section not found")
            where = {"project_id": self.sections[value]["project_id"], "section_id": value, "parent_id": None}
        else:
            parent = self.tasks.get(value)
            if parent is None or parent["is_deleted"] or value == task["id"]:
                return handler._refused("parent_id")
            where = {"project_id": parent["project_id"], "section_id": parent["section_id"], "parent_id": value}
        task.update(where)
        moving = [task["id"]]
        while moving:   # the sub-tasks go with it
            current = moving.pop()
            for child in self.tasks.values():
                if child["parent_id"] == current:
                    child["project_id"], child["section_id"] = task["project_id"], task["section_id"]
                    moving.append(child["id"])
        handler._json(200, task)

    def _update_task(self, handler, task, body):
        if set(body) & {"project_id", "section_id", "parent_id"}:
            # Todoist moves a task only through tasks/{id}/move.
            return handler._refused("project_id", "Use the move endpoint")
        if not self._check_labels(handler, body.get("labels")):
            return
        for field in ("content", "description", "priority", "labels"):
            if field in body:
                task[field] = body[field]
        if "due_string" in body or "due_date" in body:
            task["due"] = self._due(body)
        handler._json(200, task)

    def _filter(self, query: str):
        """A sliver of Todoist's filter language: today, overdue, #Project, @label and p1–p4, joined by '|' (or)
        and '&' (and)."""
        today = date.today().isoformat()

        def term(word):
            word = word.strip()
            if word == "today":
                return lambda t: bool(t["due"]) and t["due"]["date"][:10] == today
            if word == "overdue":
                return lambda t: bool(t["due"]) and t["due"]["date"][:10] < today
            if word.startswith("#"):
                ids = {p["id"] for p in self.projects.values() if p["name"].lower() == word[1:].lower()}
                return lambda t: t["project_id"] in ids
            if word.startswith("@"):
                return lambda t: word[1:] in t["labels"]
            if word in ("p1", "p2", "p3", "p4"):
                return lambda t: t["priority"] == 5 - int(word[1])
            return None

        alternatives = []
        for alternative in query.split("|"):
            terms = [term(w) for w in alternative.split("&")]
            if not terms or any(t is None for t in terms):
                return None
            alternatives.append(terms)
        return lambda task: any(all(t(task) for t in terms) for terms in alternatives)

    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("api.todoist.com", self._server)

    def secret(self, access_token: Optional[str] = None) -> Dict[str, Any]:
        """The credential as the platform hands it to the agent: the account, the token, and the loopback base
        URL."""
        return {"account": self.account, "access_token": self.token if access_token is None else access_token,
                "api_base_url": self.url + "/api/v1"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
