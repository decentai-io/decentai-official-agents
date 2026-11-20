"""The Notion API, over ``standard.http``.

- **Version 2026-03-11** on every request. Since 2025-09-03 a database is a container of one or more *data
  sources*: the schema and the rows live on a data source (``data_sources/{id}``, ``data_sources/{id}/query``), a
  row's parent is ``{type: data_source_id, ...}``, and search finds ``data_source`` objects. 2026-03-11 also names
  the trash ``in_trash`` (``archived`` is gone) and places appended blocks with ``position``.
- **Busy is waited out.** Notion allows about 3 requests a second per connection and answers more with 429 and
  ``Retry-After`` (or 529 when it is overloaded); ``Http`` waits as told, writes included, since a refused request
  did nothing.
- **Numbers travel exactly.** A body holding a ``Decimal`` is written with the number's own digits, never through a
  float.
- **Refusals carry the way out.** Notion answers alike for "no such page" and "not shared with the integration", so
  a 403 or 404 says how to share the page.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests

from standard.accounts import account
from standard.http import Http, HttpError, segment

API_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
#: Notion's ceiling for page_size on every list endpoint.
PAGE_SIZE_MAX = 100
#: The most users read to put a name to a person.
USERS_MOST = 1000

SHARE_HINT = ("In Notion, open the page (or a page above it), choose the ... menu, then Connections, and add this "
              "integration; or reconnect the workspace and select the page while signing in. The agent sees only "
              "what was shared with it.")

_MARK = "⁣"
_NUMBER = re.compile('"' + _MARK + r"(-?\d+(?:\.\d+)?)" + _MARK + '"')


def encode(body: Any) -> bytes:
    """A request body as UTF-8 JSON, each Decimal written with its own digits."""
    def walk(value: Any) -> Any:
        if isinstance(value, Decimal):
            return f"{_MARK}{format(value, 'f')}{_MARK}"
        if isinstance(value, str):
            return value.replace(_MARK, "")
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value
    return _NUMBER.sub(r"\1", json.dumps(walk(body), ensure_ascii=False)).encode("utf-8")


class Notion(Http):
    SERVICE = "Notion"
    BASE = API_BASE_URL

    def headers(self, method: str, path: str) -> Dict[str, str]:
        return {"Notion-Version": NOTION_VERSION}

    def detail(self, response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:300] or str(response.status_code)
        if isinstance(body, dict) and body.get("object") == "error":
            code, message = str(body.get("code") or ""), str(body.get("message") or "")
            return (f"{message} ({code})" if code and message else message or code)[:400]
        return super().detail(response)

    def busy(self, response: requests.Response) -> bool:
        return response.status_code in (429, 503, 529)

    def scope_refused(self, response: requests.Response, words: str) -> bool:
        # Notion has one grant per workspace: a 403 is a page not shared, never a missing scope.
        return False

    def refusal(self, response: requests.Response, method: str, said: str, words: str) -> HttpError:
        status = response.status_code
        if status == 403 or "restricted_resource" in words:
            return HttpError("denied", f"Notion did not let the integration open this ({words}). {SHARE_HINT}",
                             status)
        if status == 404 or "object_not_found" in words:
            return HttpError("not_found", f"Notion found nothing with that id, or it is not shared with the "
                                          f"integration. {SHARE_HINT}", status)
        if status == 409:
            return HttpError("conflict", "Notion reported a conflicting change to the same data; read it again "
                                         "and retry.", status)
        return super().refusal(response, method, said, words)

    # -- writes, with exact numbers -----------------------------------------------------------------------------
    async def send(self, method: str, path: str, body: Any, write: bool = True) -> Dict[str, Any]:
        """A JSON body sent; ``write=False`` for a POST that only reads (search, query), tried again like a read."""
        return await self.request(method, path, data=encode(body), write=write,
                                  headers={"Content-Type": "application/json; charset=utf-8"})

    # -- reads ----------------------------------------------------------------------------------------------------
    @staticmethod
    def paging(page_size: int, start_cursor: str = "") -> Dict[str, Any]:
        paging: Dict[str, Any] = {"page_size": max(1, min(int(page_size), PAGE_SIZE_MAX))}
        if start_cursor:
            paging["start_cursor"] = start_cursor
        return paging

    async def me(self) -> Dict[str, Any]:
        return await self.get("users/me")

    async def search(self, query: str, kind: str, page_size: int, start_cursor: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"sort": {"direction": "descending", "timestamp": "last_edited_time"},
                                **self.paging(page_size, start_cursor)}
        if query:
            body["query"] = query
        if kind == "page":
            body["filter"] = {"property": "object", "value": "page"}
        elif kind == "database":
            body["filter"] = {"property": "object", "value": "data_source"}
        return await self.send("POST", "search", body, write=False)

    async def page(self, page_id: str) -> Dict[str, Any]:
        return await self.get(f"pages/{segment(page_id.strip())}")

    async def children(self, block_id: str, page_size: int = PAGE_SIZE_MAX,
                       start_cursor: str = "") -> Dict[str, Any]:
        return await self.get(f"blocks/{segment(block_id.strip())}/children",
                              params=self.paging(page_size, start_cursor))

    async def blocks_until(self, block_id: str, wanted: int, cap: int) -> Tuple[List[Dict[str, Any]], bool]:
        """Top-level blocks, read until more than ``wanted`` are held (or ``cap``), and whether more follow."""
        blocks: List[Dict[str, Any]] = []
        cursor = ""
        while True:
            answer = await self.children(block_id, PAGE_SIZE_MAX, cursor)
            blocks.extend(answer.get("results") or [])
            if not answer.get("has_more"):
                return blocks[:cap], len(blocks) > cap
            if len(blocks) > wanted or len(blocks) >= cap:
                return blocks[:cap], True
            cursor = str(answer.get("next_cursor") or "")

    async def database(self, database_id: str) -> Dict[str, Any]:
        return await self.get(f"databases/{segment(database_id.strip())}")

    async def data_source(self, source_id: str) -> Dict[str, Any]:
        return await self.get(f"data_sources/{segment(source_id.strip())}")

    async def query(self, source_id: str, page_size: int, start_cursor: str = "",
                    filter_: Optional[Dict[str, Any]] = None,
                    sorts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = self.paging(page_size, start_cursor)
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        return await self.send("POST", f"data_sources/{segment(source_id.strip())}/query", body, write=False)

    async def comments(self, block_id: str, page_size: int, start_cursor: str = "") -> Dict[str, Any]:
        return await self.get("comments", params={"block_id": str(block_id).strip(),
                                                  **self.paging(page_size, start_cursor)})

    async def users(self) -> List[Dict[str, Any]]:
        """The workspace's people (not bots), at most USERS_MOST."""
        found: List[Dict[str, Any]] = []
        cursor = ""
        while len(found) < USERS_MOST:
            answer = await self.get("users", params=self.paging(PAGE_SIZE_MAX, cursor))
            found.extend(u for u in answer.get("results") or [] if u.get("type") == "person")
            cursor = str(answer.get("next_cursor") or "")
            if not (answer.get("has_more") and cursor):
                break
        return found[:USERS_MOST]

    # -- writes ---------------------------------------------------------------------------------------------------
    async def create_page(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self.send("POST", "pages", body)

    async def update_page(self, page_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self.send("PATCH", f"pages/{segment(page_id.strip())}", body)

    async def append(self, block_id: str, children: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self.send("PATCH", f"blocks/{segment(block_id.strip())}/children", {"children": children})

    async def add_comment(self, page_id: str, rich_text: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self.send("POST", "comments", {"parent": {"page_id": str(page_id).strip()},
                                                    "rich_text": rich_text})


async def client_for(call: Any, wanted: Any = None, ask: bool = True) -> Tuple[Optional[Notion], str]:
    """A client for the workspace named (or the only one); with none kept, the person is asked to connect one."""
    secret, why = await account(call, "notion", wanted, ask=ask, service="Notion")
    if secret is None:
        return None, why
    client = Notion(token=secret.get("access_token") or "", api_base_url=secret.get("api_base_url"))
    client.account = str(secret.get("account") or "")
    return client, ""


def not_connected(why: str):
    return {"error": why, "kind": "auth"}, "error"


def invalid(message: str):
    return {"error": message, "kind": "invalid"}, "error"
