"""A loopback Notion API (version 2026-03-11): the subset the Notion agent calls, over real HTTP, holding a small
fictional workspace for Sidra Office Supplies.

It is as strict as Notion where the agent depends on it:

- every request carries ``Notion-Version: 2026-03-11`` and the bearer token;
- a database is a container of data sources (2025-09-03): ``GET databases/{id}`` lists its ``data_sources`` and no
  schema; the schema and the rows are a data source's (``GET data_sources/{id}``, ``POST data_sources/{id}/query``);
  the old ``POST databases/{id}/query`` is refused; a row's parent is ``{type: data_source_id, data_source_id,
  database_id}``; search's object filter takes ``page`` or ``data_source``, never ``database``;
- the trash is ``in_trash`` (2026-03-11): ``archived`` in a request is refused, and a page in the trash cannot be
  edited;
- ``page_size`` is 1 to 100; a rich-text object holds at most 2,000 characters (UTF-16, as Notion counts) and any
  array at most 100 items; one append at most 100 blocks; ``after`` is gone (``position`` replaces it);
- a filter names a property of the schema with that property's own type, and nests at most two groups deep;
- a number is a JSON number; people are user ids the workspace knows;
- last_edited_time is kept to the minute (the stub's ``clock``, which a test moves);
- a page the integration was not given answers 403 restricted_resource, an unknown id 404 object_not_found;
- busy: ``limit_next`` answers that many requests 429 with ``Retry-After: retry_after``; ``per_second`` answers 429
  (Retry-After 1) to more than that many requests in one second, as Notion's ~3 a second.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from tests.conftest import stands_for

VERSION = "2026-03-11"
TOKEN = "at-1"
USERS = [
    {"object": "user", "id": "user-dana", "type": "person", "name": "Dana Harbour",
     "person": {"email": "dana@sidra.example"}},
    {"object": "user", "id": "user-omar", "type": "person", "name": "Omar Nasser",
     "person": {"email": "omar@sidra.example"}},
    {"object": "user", "id": "user-layla", "type": "person", "name": "ليلى حداد",
     "person": {"email": "layla@sidra.example"}},
    {"object": "user", "id": "bot-sidra", "type": "bot", "name": "DecentAI", "bot": {}},
]


class Invalid(Exception):
    """A request Notion refuses as validation_error."""


def rich(text: str) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": {"content": text, "link": None},
             "annotations": {"bold": False}, "plain_text": text, "href": None}]


def plain(parts: Any) -> str:
    return "".join(str(p.get("plain_text") or (p.get("text") or {}).get("content") or "")
                   for p in parts or [])


def units(text: str) -> int:
    """A text's length as Notion counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def checked_rich(parts: Any, where: str) -> None:
    if not isinstance(parts, list):
        raise Invalid(f"{where} should be an array.")
    if len(parts) > 100:
        raise Invalid(f"{where}.length should be ≤ 100, instead was {len(parts)}.")
    for part in parts:
        content = str((part.get("text") or {}).get("content") or "")
        if units(content) > 2000:
            raise Invalid(f"{where}[0].text.content.length should be ≤ 2000, instead was {units(content)}.")


def encoded(body: Any) -> bytes:
    def default(value):
        if isinstance(value, Decimal):
            return float(value)
        raise TypeError(value)
    return json.dumps(body, default=default).encode("utf-8")


class NotionStub:
    ACCOUNT = "demo@sidra.example"
    WORKSPACE = "Sidra Office Supplies"

    def __init__(self):
        self.clock = "2026-09-10T09:00:00.000Z"
        self.databases: Dict[str, Dict[str, Any]] = {}
        self.sources: Dict[str, Dict[str, Any]] = {}
        self.pages: Dict[str, Dict[str, Any]] = {}
        self.children: Dict[str, List[Dict[str, Any]]] = {}
        self.comments: List[Dict[str, Any]] = []
        self.restricted: set = set()
        self.created: List[Dict[str, Any]] = []
        self.updated: List[Dict[str, Any]] = []
        self.appended: List[Dict[str, Any]] = []
        self.commented: List[Dict[str, Any]] = []
        self.queries: List[Dict[str, Any]] = []
        self.raw: List[bytes] = []
        self.refused: List[str] = []
        self.requests = 0
        self.unversioned = 0
        self.limit_next = 0
        self.retry_after = "7"
        self.per_second = 0
        self.limited = 0
        self._recent: List[float] = []
        self._n = 0
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def _id(self) -> str:
        self._n += 1
        return f"{self._n:08x}-5eed-4a11-8b0c-{self._n:012x}"

    @staticmethod
    def _url(title: str, page_id: str) -> str:
        slug = re.sub(r"[^\w]+", "-", title).strip("-")
        return f"https://www.notion.so/{slug}-{page_id.replace('-', '')}"

    def _schema(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        properties = {}
        for name, spec in schema.items():
            kind, options = (spec, []) if isinstance(spec, str) else spec
            prop: Dict[str, Any] = {"id": name[:4].lower(), "name": name, "type": kind, kind: {}}
            if options:
                prop[kind] = {"options": [{"id": f"o{i}", "name": o, "color": "default"}
                                          for i, o in enumerate(options)]}
            properties[name] = prop
        return properties

    def add_database(self, title: str, schema: Dict[str, Any], parent_page: str = "") -> str:
        """A database with one data source of this schema: name -> type, or name -> (type, [options])."""
        database_id = self._id()
        self.databases[database_id] = {
            "object": "database", "id": database_id, "title": rich(title),
            "url": self._url(title, database_id), "created_time": self.clock,
            "last_edited_time": self.clock, "data_sources": [],
            "parent": ({"type": "page_id", "page_id": parent_page} if parent_page
                       else {"type": "workspace", "workspace": True}),
            "in_trash": False, "is_inline": False}
        self.add_source(database_id, title, schema)
        return database_id

    def add_source(self, database_id: str, name: str, schema: Dict[str, Any]) -> str:
        source_id = self._id()
        database = self.databases[database_id]
        database["data_sources"].append({"id": source_id, "name": name})
        self.sources[source_id] = {
            "object": "data_source", "id": source_id, "title": rich(name),
            "properties": self._schema(schema), "created_time": self.clock, "last_edited_time": self.clock,
            "parent": {"type": "database_id", "database_id": database_id},
            "database_parent": database["parent"], "in_trash": False}
        return source_id

    def source_of(self, database_id: str) -> str:
        return self.databases[database_id]["data_sources"][0]["id"]

    def _value(self, source_id: str, name: str, value: Any) -> Dict[str, Any]:
        """A Python value as the property object Notion returns."""
        prop = self.sources[source_id]["properties"][name]
        kind = prop["type"]
        out: Dict[str, Any] = {"id": prop["id"], "type": kind}
        if kind in ("title", "rich_text"):
            out[kind] = rich(value) if value else []
        elif kind in ("select", "status"):
            out[kind] = {"name": value, "color": "default"} if value else None
        elif kind == "multi_select":
            out[kind] = [{"name": v, "color": "default"} for v in value or []]
        elif kind == "date":
            out[kind] = {"start": value, "end": None} if value else None
        elif kind == "people":
            out[kind] = [self.user(u) for u in value or []]
        else:
            out[kind] = value
        return out

    @staticmethod
    def user(user_id: str) -> Dict[str, Any]:
        return next(dict(u) for u in USERS if u["id"] == user_id)

    def _output(self, source_id: str, name: str, given: Dict[str, Any]) -> Dict[str, Any]:
        """A property as the agent sent it, checked as Notion checks it, as Notion stores and returns it."""
        prop = self.sources[source_id]["properties"][name]
        kind = prop["type"]
        if kind not in given:
            raise Invalid(f"{name} is expected to be {kind}.")
        value = given.get(kind)
        if kind in ("title", "rich_text"):
            checked_rich(value, f"body.properties.{name}.{kind}")
            value = rich(plain(value)) if plain(value) else []
        elif kind in ("select", "status") and value:
            value = {"name": value["name"], "color": "default"}
        elif kind == "multi_select":
            if len(value or []) > 100:
                raise Invalid(f"body.properties.{name}.multi_select.length should be ≤ 100.")
            value = [{"name": v["name"], "color": "default"} for v in value or []]
        elif kind == "number":
            if value is not None and (isinstance(value, (bool, str)) or not isinstance(value, (int, Decimal))):
                raise Invalid(f"body.properties.{name}.number should be a number or `null`.")
        elif kind == "people":
            known = {u["id"] for u in USERS}
            for person in value or []:
                if person.get("id") not in known:
                    raise Invalid(f"Could not find user with ID: {person.get('id')}.")
            value = [self.user(p["id"]) for p in value or []]
        elif kind in ("created_time", "last_edited_time", "formula", "rollup", "unique_id"):
            raise Invalid(f"{name} is a {kind} property and cannot be written.")
        return {"id": prop["id"], "type": kind, kind: value}

    def add_row(self, database_id: str, values: Dict[str, Any], created: str = "", edited: str = "",
                source_id: str = "") -> str:
        page_id = self._id()
        source_id = source_id or self.source_of(database_id)
        source = self.sources[source_id]
        title_name = next(n for n, p in source["properties"].items() if p["type"] == "title")
        properties = {name: self._value(source_id, name, values.get(name)) for name in source["properties"]}
        self.pages[page_id] = {
            "object": "page", "id": page_id, "created_time": created or self.clock,
            "last_edited_time": edited or created or self.clock,
            "created_by": {"object": "user", "id": "user-dana"},
            "parent": {"type": "data_source_id", "data_source_id": source_id, "database_id": database_id},
            "in_trash": False, "is_locked": False, "properties": properties,
            "url": self._url(str(values.get(title_name) or ""), page_id)}
        return page_id

    def edit_row(self, page_id: str, values: Dict[str, Any]) -> None:
        """A person editing a row in Notion, at the stub's clock."""
        page = self.pages[page_id]
        source_id = page["parent"]["data_source_id"]
        for name, value in values.items():
            page["properties"][name] = self._value(source_id, name, value)
        page["last_edited_time"] = self.clock

    def add_page(self, title: str, parent_page: str = "", edited: str = "") -> str:
        page_id = self._id()
        self.pages[page_id] = {
            "object": "page", "id": page_id, "created_time": edited or self.clock,
            "last_edited_time": edited or self.clock,
            "parent": ({"type": "page_id", "page_id": parent_page} if parent_page
                       else {"type": "workspace", "workspace": True}),
            "in_trash": False, "is_locked": False,
            "properties": {"title": {"id": "title", "type": "title", "title": rich(title)}},
            "url": self._url(title, page_id)}
        if parent_page:
            self.add_block(parent_page, "child_page", title=title, block_id=page_id)
        return page_id

    def add_block(self, parent: str, kind: str, text: str = "", block_id: str = "",
                  **extra) -> str:
        block_id = block_id or self._id()
        if kind in ("child_page", "child_database"):
            body: Dict[str, Any] = {"title": extra.pop("title", text)}
        elif kind == "divider":
            body = {}
        else:
            body = {"rich_text": rich(text), "color": "default"}
        body.update(extra)
        self.children.setdefault(parent, []).append({
            "object": "block", "id": block_id, "type": kind, kind: body,
            "has_children": False, "in_trash": False,
            "created_time": self.clock, "last_edited_time": self.clock})
        # A block that gains a child says so, as Notion's has_children does.
        for siblings in self.children.values():
            for block in siblings:
                if block["id"] == parent:
                    block["has_children"] = True
        return block_id

    def add_comment(self, page_id: str, text: str, author: str = "user-dana") -> str:
        comment_id = self._id()
        self.comments.append({"object": "comment", "id": comment_id,
                              "parent": {"type": "page_id", "page_id": page_id},
                              "discussion_id": "disc-" + comment_id[:8],
                              "created_time": self.clock, "last_edited_time": self.clock,
                              "created_by": {"object": "user", "id": author},
                              "rich_text": rich(text)})
        return comment_id

    # -- queries -------------------------------------------------------
    @staticmethod
    def _text(prop: Dict[str, Any]) -> str:
        kind = prop["type"]
        value = prop.get(kind)
        if kind in ("title", "rich_text"):
            return plain(value)
        if kind in ("select", "status"):
            return (value or {}).get("name") or ""
        if kind == "date":
            return (value or {}).get("start") or ""
        return "" if value is None else str(value)

    @staticmethod
    def depth(filter_: Dict[str, Any]) -> int:
        groups = filter_.get("and") or filter_.get("or")
        if groups is None:
            return 0
        return 1 + max((NotionStub.depth(f) for f in groups), default=0)

    def _check_filter(self, source: Dict[str, Any], filter_: Dict[str, Any]) -> None:
        if self.depth(filter_) > 2:
            raise Invalid("body.filter: compound filters may be nested at most two levels deep.")
        stack = [filter_]
        while stack:
            item = stack.pop()
            group = item.get("and") if "and" in item else item.get("or")
            if group is not None:
                if len(group) > 100:
                    raise Invalid("body.filter: a compound filter holds at most 100 filters.")
                stack.extend(group)
                continue
            if item.get("timestamp"):
                continue
            prop = source["properties"].get(item.get("property"))
            if prop is None:
                raise Invalid(f"Could not find property with name or id: {item.get('property')}")
            if prop["type"] not in item:
                raise Invalid(f"body.filter.{prop['type']} should be defined for property {item['property']}.")

    def _matches(self, page: Dict[str, Any], filter_: Optional[Dict[str, Any]]) -> bool:
        if not filter_:
            return True
        if "and" in filter_:
            return all(self._matches(page, f) for f in filter_["and"])
        if "or" in filter_:
            return any(self._matches(page, f) for f in filter_["or"])
        if filter_.get("timestamp"):
            kind = filter_["timestamp"]
            (operator, wanted), = filter_[kind].items()
            return self._compare(page[kind], operator, wanted)
        prop = page["properties"][filter_["property"]]
        kind = prop["type"]
        (operator, wanted), = filter_[kind].items()
        value = prop.get(kind)
        if operator in ("is_empty", "is_not_empty"):
            empty = value in (None, [], "") or (kind in ("title", "rich_text") and not plain(value))
            return empty == (operator == "is_empty")
        if kind == "multi_select":
            has = any(o["name"] == wanted for o in value or [])
            return has == (operator == "contains")
        if kind in ("people", "created_by", "last_edited_by"):
            ids = [u["id"] for u in (value if isinstance(value, list) else [value or {}])]
            return (wanted in ids) == (operator == "contains")
        if kind == "checkbox":
            return (bool(value) == wanted) == (operator == "equals")
        if kind == "number":
            if value is None:
                return False
            return self._compare(Decimal(str(value)), operator, Decimal(str(wanted)))
        text = self._text(prop)
        if kind == "date":
            return bool(text) and self._compare(text[:10], operator, str(wanted)[:10])
        if operator == "equals":
            return text == wanted
        if operator == "does_not_equal":
            return text != wanted
        if operator == "contains":
            return wanted.lower() in text.lower()
        if operator == "does_not_contain":
            return wanted.lower() not in text.lower()
        if operator == "starts_with":
            return text.lower().startswith(wanted.lower())
        if operator == "ends_with":
            return text.lower().endswith(wanted.lower())
        raise Invalid(f"stub cannot filter {filter_}")

    @staticmethod
    def _compare(have: Any, operator: str, wanted: Any) -> bool:
        return {"equals": have == wanted, "does_not_equal": have != wanted,
                "greater_than": have > wanted, "less_than": have < wanted,
                "greater_than_or_equal_to": have >= wanted, "less_than_or_equal_to": have <= wanted,
                "on_or_after": have >= wanted, "on_or_before": have <= wanted,
                "after": have > wanted, "before": have < wanted}[operator]

    def _sorted(self, pages: List[Dict[str, Any]], sorts: List[Dict[str, Any]]):
        for sort in reversed(sorts or []):
            if sort.get("timestamp"):
                key = (lambda p, k=sort["timestamp"]: p[k])
            else:
                key = (lambda p, n=sort["property"]: self._text(p["properties"][n]))
            pages = sorted(pages, key=key, reverse=sort["direction"] == "descending")
        return pages

    @staticmethod
    def _page_of(items: List[Dict[str, Any]], body: Dict[str, Any]) -> Dict[str, Any]:
        start = int(body.get("start_cursor") or 0)
        size = int(body.get("page_size") or 100)
        if not 1 <= size <= 100:
            raise Invalid(f"body.page_size should be ≤ 100, instead was {size}.")
        chunk = items[start:start + size]
        more = start + size < len(items)
        return {"object": "list", "results": chunk, "has_more": more,
                "next_cursor": str(start + size) if more else None}

    # -- the server ----------------------------------------------------
    def start(self) -> "NotionStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None):
                raw = encoded(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, status: int, code: str, message: str):
                stub.refused.append(f"{code}: {message}")
                self._json(status, {"object": "error", "status": status,
                                    "code": code, "message": message})

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                stub.raw.append(raw)
                return json.loads(raw.decode("utf-8"), parse_float=Decimal) if raw else {}

            def _busy(self) -> bool:
                with stub._lock:
                    now = time.monotonic()
                    stub._recent = [t for t in stub._recent if now - t < 1.0] + [now]
                    if stub.limit_next:
                        stub.limit_next -= 1
                        after = stub.retry_after
                    elif stub.per_second and len(stub._recent) > stub.per_second:
                        after = "1"
                    else:
                        return False
                    stub.limited += 1
                self._json(429, {"object": "error", "status": 429, "code": "rate_limited",
                                 "message": "You have been rate limited. Please try again in a few minutes.",
                                 "additional_data": {"retry_after": after}},
                           {"Retry-After": after})
                return True

            def _gate(self) -> Optional[tuple]:
                """Version, token, rate limit; then the route."""
                stub.requests += 1
                if self.headers.get("Notion-Version") != VERSION:
                    stub.unversioned += 1
                    self._error(400, "missing_version", "Notion-Version header failed validation.")
                    return None
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._error(401, "unauthorized", "API token is invalid.")
                    return None
                if self._busy():
                    return None
                url = urllib.parse.urlparse(self.path)
                path = url.path[len("/v1"):] if url.path.startswith("/v1") else url.path
                return path.strip("/").split("/"), urllib.parse.parse_qs(url.query)

            def _object(self, kind: str, object_id: str) -> Optional[Dict[str, Any]]:
                if object_id in stub.restricted:
                    self._error(403, "restricted_resource", "Insufficient permissions for this endpoint.")
                    return None
                found = {"page": stub.pages, "database": stub.databases,
                         "data_source": stub.sources}[kind].get(object_id)
                if found is None:
                    self._error(404, "object_not_found",
                                f"Could not find {kind} with ID: {object_id}. Make sure the "
                                f"relevant pages and databases are shared with your integration.")
                return found

            def _serve(self, method: str) -> None:
                routed = self._gate()
                if routed is None:
                    return
                parts, query = routed
                try:
                    body = self._payload() if method in ("POST", "PATCH") else {}
                    getattr(self, f"_{method.lower()}")(parts, query, body)
                except Invalid as wrong:
                    self._error(400, "validation_error", str(wrong))

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

            def do_PATCH(self):  # noqa: N802
                self._serve("PATCH")

            def _get(self, parts, query, body):
                args = {k: v[0] for k, v in query.items()}
                if parts == ["users", "me"]:
                    return self._json(200, {
                        "object": "user", "id": "bot-sidra", "type": "bot",
                        "name": "DecentAI", "bot": {"owner": {"type": "workspace", "workspace": True},
                                                    "workspace_name": stub.WORKSPACE}})
                if parts == ["users"]:
                    return self._json(200, stub._page_of(USERS, args))
                if len(parts) == 2 and parts[0] in ("pages", "databases", "data_sources"):
                    found = self._object(parts[0][:-1], parts[1])
                    return found is not None and self._json(200, found)
                if len(parts) == 3 and parts[0] == "blocks" and parts[2] == "children":
                    parent = parts[1]
                    if parent in stub.restricted:
                        return self._error(403, "restricted_resource", "Insufficient permissions.")
                    known = parent in stub.pages or any(
                        b["id"] == parent for bs in stub.children.values() for b in bs)
                    if not known:
                        return self._error(404, "object_not_found", f"Could not find block {parent}.")
                    return self._json(200, stub._page_of(stub.children.get(parent, []), args))
                if parts == ["comments"]:
                    block_id = args.get("block_id", "")
                    if self._object("page", block_id) is None:
                        return
                    rows = [c for c in stub.comments if c["parent"]["page_id"] == block_id]
                    return self._json(200, stub._page_of(rows, args))
                self._error(400, "invalid_request_url", f"Invalid request URL: GET {'/'.join(parts)}")

            def _post(self, parts, query, body):
                if parts == ["search"]:
                    wanted = str(body.get("query") or "").lower()
                    kind = (body.get("filter") or {}).get("value")
                    if kind not in (None, "page", "data_source"):
                        raise Invalid(f'body.filter.value should be "page" or "data_source", instead was '
                                      f'"{kind}".')
                    items = [o for o in list(stub.pages.values()) + list(stub.sources.values())
                             if o["id"] not in stub.restricted and not o.get("in_trash")
                             and (not kind or o["object"] == kind)
                             and wanted in self._title(o).lower()]
                    items.sort(key=lambda o: o["last_edited_time"], reverse=True)
                    return self._json(200, stub._page_of(items, body))
                if len(parts) == 3 and parts[0] == "databases" and parts[2] == "query":
                    return self._error(400, "invalid_request_url",
                                       "Invalid request URL: since 2025-09-03 rows are queried at "
                                       "data_sources/{data_source_id}/query.")
                if len(parts) == 3 and parts[0] == "data_sources" and parts[2] == "query":
                    source = self._object("data_source", parts[1])
                    if source is None:
                        return
                    if body.get("filter"):
                        stub._check_filter(source, body["filter"])
                    stub.queries.append(body)
                    rows = [p for p in stub.pages.values()
                            if p["parent"].get("data_source_id") == parts[1] and not p.get("in_trash")
                            and stub._matches(p, body.get("filter"))]
                    rows = stub._sorted(rows, body.get("sorts"))
                    return self._json(200, {**stub._page_of(rows, body), "type": "page_or_data_source"})
                if parts == ["pages"]:
                    return self._create(body)
                if parts == ["comments"]:
                    page_id = body["parent"]["page_id"]
                    checked_rich(body.get("rich_text"), "body.rich_text")
                    if self._object("page", page_id) is None:
                        return
                    stub.commented.append(body)
                    comment_id = stub.add_comment(page_id, plain(body["rich_text"]), "bot-sidra")
                    return self._json(200, next(c for c in stub.comments if c["id"] == comment_id))
                self._error(400, "invalid_request_url", f"Invalid request URL: POST {'/'.join(parts)}")

            @staticmethod
            def _title(obj: Dict[str, Any]) -> str:
                if obj["object"] in ("database", "data_source"):
                    return plain(obj["title"])
                return next((plain(p["title"]) for p in obj["properties"].values()
                             if p["type"] == "title"), "")

            def _children(self, children: List[Dict[str, Any]]) -> None:
                if len(children) > 100:
                    raise Invalid(f"body.children.length should be ≤ 100, instead was {len(children)}.")
                for child in children:
                    checked_rich(child[child["type"]].get("rich_text") or [], f"body.children.{child['type']}")

            def _create(self, body: Dict[str, Any]):
                parent = body.get("parent") or {}
                properties = body.get("properties") or {}
                self._children(body.get("children") or [])
                if parent.get("database_id") and not parent.get("data_source_id"):
                    database = self._object("database", parent["database_id"])
                    if database is None:
                        return
                    if len(database["data_sources"]) != 1:
                        raise Invalid("This database has several data sources: give parent.data_source_id.")
                    parent = {"data_source_id": database["data_sources"][0]["id"]}
                if parent.get("data_source_id"):
                    source = self._object("data_source", parent["data_source_id"])
                    if source is None:
                        return
                    unknown = [n for n in properties if n not in source["properties"]]
                    if unknown:
                        raise Invalid(f"{unknown[0]} is not a property that exists.")
                    outputs = {n: stub._output(source["id"], n, g) for n, g in properties.items()}
                    page_id = stub.add_row(source["parent"]["database_id"], {}, source_id=source["id"])
                    page = stub.pages[page_id]
                    page["properties"].update(outputs)
                else:
                    if self._object("page", parent.get("page_id", "")) is None:
                        return
                    checked_rich(properties["title"]["title"], "body.properties.title.title")
                    page_id = stub.add_page(plain(properties["title"]["title"]), parent["page_id"])
                    page = stub.pages[page_id]
                page["url"] = stub._url(self._title(page), page_id)
                for child in body.get("children") or []:
                    stub.add_block(page_id, child["type"], plain(child[child["type"]]["rich_text"]))
                stub.created.append(body)
                self._json(200, page)

            def _patch(self, parts, query, body):
                if len(parts) == 2 and parts[0] == "pages":
                    if "archived" in body:
                        raise Invalid("body.archived is not a valid parameter since 2026-03-11: use in_trash.")
                    page = self._object("page", parts[1])
                    if page is None:
                        return
                    if page.get("in_trash") and body.get("properties"):
                        raise Invalid("Can't edit block that is archived. You must unarchive the block before "
                                      "editing.")
                    source_id = page["parent"].get("data_source_id")
                    outputs = {}
                    for name, given in (body.get("properties") or {}).items():
                        if source_id:
                            if name not in stub.sources[source_id]["properties"]:
                                raise Invalid(f"{name} is not a property that exists.")
                            outputs[name] = stub._output(source_id, name, given)
                        else:
                            checked_rich(given["title"], "body.properties.title.title")
                            outputs[name] = {"id": "title", "type": "title", "title": rich(plain(given["title"]))}
                    page["properties"].update(outputs)
                    if "in_trash" in body:
                        page["in_trash"] = bool(body["in_trash"])
                    page["last_edited_time"] = stub.clock
                    stub.updated.append({"page_id": parts[1], **body})
                    return self._json(200, page)
                if len(parts) == 3 and parts[0] == "blocks" and parts[2] == "children":
                    if "after" in body:
                        raise Invalid("body.after is not a valid parameter since 2026-03-11: use position.")
                    page = self._object("page", parts[1])
                    if page is None:
                        return
                    if page.get("in_trash"):
                        raise Invalid("Can't edit block that is archived.")
                    self._children(body.get("children") or [])
                    made = []
                    for child in body.get("children") or []:
                        kind = child["type"]
                        extra = {"checked": child[kind]["checked"]} if kind == "to_do" else {}
                        made.append(stub.add_block(parts[1], kind,
                                                   plain(child[kind]["rich_text"]), **extra))
                    page["last_edited_time"] = stub.clock
                    stub.appended.append({"page_id": parts[1], **body})
                    return self._json(200, {"object": "list", "has_more": False, "next_cursor": None,
                                            "results": [b for b in stub.children[parts[1]]
                                                        if b["id"] in made]})
                self._error(400, "invalid_request_url", f"Invalid request URL: PATCH {'/'.join(parts)}")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("api.notion.com", self._server)

    def secret(self, access_token: str = TOKEN, account: str = "") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": account or self.ACCOUNT, "access_token": access_token,
                "api_base_url": self.url + "/v1"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
