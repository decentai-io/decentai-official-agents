"""Finding pages and databases by title.

Notion's search matches titles only, and only across what was shared with the integration, which is exactly what
the agent can open: a page missing here is a page to share, not a page that does not exist. Databases come back as
their data sources (Notion's 2025-09-03 model): the id given is the data source's, which every database function
takes, and ``database_id`` names the database it belongs to.
"""

from standard import ToolBase
from standard.http import HttpError

from .notion_api import client_for, not_connected
from .schema import PageRow
from .text import PropertyText, Text


class SearchTool(ToolBase):
    id = "search"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        most = int(call.inputs.get("max_results") or 20)
        try:
            answer = await client.search(str(call.inputs.get("query") or "").strip(),
                                         str(call.inputs.get("kind") or "any"), most,
                                         str(call.inputs.get("start_cursor") or ""))
        except HttpError as failed:
            return failed.answer()
        rows = []
        for obj in (answer.get("results") or [])[:most]:
            kind = str(obj.get("object") or "")
            row = {"id": str(obj.get("id") or ""), "kind": "database" if kind == "data_source" else kind,
                   "title": Text.clip(PropertyText.title(obj), 300), "url": str(obj.get("url") or ""),
                   "last_edited": str(obj.get("last_edited_time") or ""), "parent": PageRow.parent(obj)}
            if kind == "data_source":
                parent = obj.get("parent") or {}
                row["database_id"] = str(parent.get("database_id") or "")
                row["parent"] = PageRow.parent({"parent": obj.get("database_parent") or {}}) or row["parent"]
            rows.append(row)
        result = {"results": rows}
        if answer.get("has_more") and answer.get("next_cursor"):
            result["next_cursor"] = str(answer["next_cursor"])
        return result, "success"
