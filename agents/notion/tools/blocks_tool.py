"""Adding to the end of a page: paragraphs, to-dos and bullets.

Only plain kinds are offered. A heading or a table written by the agent into a page others keep would be a layout
decision, which is the person's to make in Notion. Notion takes at most 100 blocks in one request; the manifest
holds a call to 50. With no ``position`` Notion appends at the end.
"""

from standard import ToolBase
from standard.http import HttpError

from .notion_api import client_for, invalid, not_connected
from .pages_tool import trashed
from .schema import PageRow
from .text import Text

KINDS = {"paragraph": "paragraph", "to_do": "to_do", "bullet": "bulleted_list_item"}


class BlocksTool(ToolBase):
    id = "blocks"

    @staticmethod
    def _block(item):
        kind = KINDS[str(item.get("type") or "paragraph")]
        body = {"rich_text": Text.write(str(item.get("text") or ""))}
        if kind == "to_do":
            body["checked"] = bool(item.get("checked"))
        return {"object": "block", "type": kind, kind: body}

    async def append(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        page_id = str(call.inputs["page_id"]).strip()
        items = [i for i in call.inputs["items"] if str(i.get("text") or "").strip()]
        if not items:
            return invalid("Every item was empty; nothing to add.")
        try:
            page = await client.page(page_id)
            if trashed(page):
                return invalid("That page is in the Notion trash; restore it first (pages_archive with restore).")
            answer = await client.append(page_id, [self._block(i) for i in items])
        except HttpError as failed:
            return failed.answer()
        row = PageRow.row(page, with_properties=False)
        added = answer.get("results") or []
        write_ref = await call.records.create("write", {
            "action": "blocks_appended", "page_id": page_id, "title": row["title"], "account": client.account,
            "parent_id": PageRow.parent_id(page), "link": row["url"],
            "changed": f"{len(items)} blocks",
            "detail": {"items": [{"type": str(i.get("type") or "paragraph"),
                                  "text": Text.clip(str(i.get("text") or ""), 500)}
                                 for i in items]}})
        return {"page_id": page_id, "title": row["title"], "url": row["url"],
                "appended": len(items),
                "block_ids": [str(b.get("id") or "") for b in added][-len(items):],
                "write_ref": write_ref}, "success"
