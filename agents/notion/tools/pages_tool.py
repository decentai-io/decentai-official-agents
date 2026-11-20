"""Reading a page, and creating, changing or trashing one.

A page is read as its properties and its body, one line per block, a window at a time: ``from`` and ``max_blocks``
choose the window. Blocks are fetched a hundred at a time only as far as the window needs (``total`` is a floor
when ``total_capped`` says more follow). Children of a toggle or a list item are fetched one level down, because
that is where a checklist or a folded section keeps its content; anything deeper is named, not walked.

Every write goes through the schema first (schema.py) and leaves a ``write`` record: what was sent, and for an
update what was there before, so a person can see and undo what the agent changed. Trashing a page is Notion's own
trash (``in_trash``), from which it can be restored.
"""

from standard import ToolBase
from standard.http import HttpError

from .notion_api import client_for, invalid, not_connected
from .schema import PageRow, SchemaProblem
from .sources import Several, people_for, resolve
from .text import BlockText, PropertyText, Text

#: How many top-level blocks are read at most to count a page.
BLOCK_CAP = 1000
#: How many children of one block are fetched.
CHILDREN_CAP = 100


def paragraph(text: str):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": Text.write(text)}}


def trashed(page) -> bool:
    return bool(page.get("in_trash") or page.get("archived"))


class PagesTool(ToolBase):
    id = "pages"

    # -- reading ---------------------------------------------------------
    async def read(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        page_id = str(call.inputs["page_id"]).strip()
        start = int(call.inputs.get("from") or 0)
        most = int(call.inputs.get("max_blocks") or 30)
        try:
            page = await client.page(page_id)
            blocks, capped = await client.blocks_until(page_id, start + most, BLOCK_CAP)
            lines = BlockText.lines(blocks)
            rows = []
            for index in range(start, min(start + most, len(blocks))):
                block = blocks[index]
                rows.append({"index": index, "depth": 0, "block_id": str(block.get("id") or ""),
                             "type": str(block.get("type") or ""), "text": lines[index]})
                if block.get("has_children") and block.get("type") not in BlockText.OWN_PAGES:
                    rows.extend(await self._nested(client, block, index))
        except HttpError as failed:
            return failed.answer()
        result = {**PageRow.row(page), "parent": PageRow.parent(page), "in_trash": trashed(page),
                  "blocks": rows, "from": start, "total": len(blocks),
                  "total_capped": capped, "more": start + most < len(blocks) or capped}
        if result["more"]:
            result["next_from"] = start + most
        return result, "success"

    @staticmethod
    async def _nested(client, block, index):
        """One level of a block's children; anything deeper is named."""
        answer = await client.children(str(block.get("id") or ""), CHILDREN_CAP)
        children = answer.get("results") or []
        rows = []
        for child, text in zip(children, BlockText.lines(children)):
            if child.get("has_children") and child.get("type") not in BlockText.OWN_PAGES:
                text += " (has nested content, not shown)"
            rows.append({"index": index, "depth": 1, "block_id": str(child.get("id") or ""),
                         "type": str(child.get("type") or ""), "text": text})
        if answer.get("has_more"):
            rows.append({"index": index, "depth": 1, "block_id": "", "type": "more",
                         "text": f"(more than {CHILDREN_CAP} nested blocks; the rest not shown)"})
        return rows

    # -- writing ---------------------------------------------------------
    @staticmethod
    def _with_title(schema, values, title):
        """The title input folded into the property values, once."""
        values = dict(values or {})
        if not title:
            return values
        name = schema.title_property()
        if any(str(k).lower() == name.lower() for k in values):
            raise SchemaProblem(f"The title was given twice: as title and as the "
                                f"'{name}' property. Give it once.")
        values[name] = title
        return values

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        inputs = call.inputs
        database_id = str(inputs.get("database_id") or "").strip()
        parent_page_id = str(inputs.get("parent_page_id") or "").strip()
        title = str(inputs.get("title") or "").strip()
        if bool(database_id) == bool(parent_page_id):
            return invalid("Give exactly one of database_id (a new row) or "
                           "parent_page_id (a new page under that page).")
        try:
            if database_id:
                schema = await resolve(client, database_id)
                if schema.in_trash:
                    return invalid(f"The database '{schema.title}' is in the Notion trash; restore it first.")
                try:
                    values = self._with_title(schema, inputs.get("properties"), title)
                    payload, shown = schema.build(values, await people_for(client, schema, values))
                except SchemaProblem as problem:
                    return invalid(str(problem))
                parent = {"type": "data_source_id", "data_source_id": schema.source_id}
                title = title or next((shown[n] for n in shown if n == schema.title_property()), "")
            else:
                if inputs.get("properties"):
                    return invalid("A page under a page has only a title; properties "
                                   "belong to rows of a database.")
                if not title:
                    return invalid("A new page needs a title.")
                payload = {"title": {"title": Text.write(title)}}
                shown = {"title": title}
                parent = {"type": "page_id", "page_id": parent_page_id}
            body = {"parent": parent, "properties": payload}
            paragraphs = [str(p) for p in inputs.get("paragraphs") or []]
            if paragraphs:
                body["children"] = [paragraph(p) for p in paragraphs]
            created = await client.create_page(body)
        except Several as several:
            return invalid(str(several))
        except HttpError as failed:
            return failed.answer()
        row = PageRow.row(created, with_properties=False)
        write_ref = await call.records.create("write", {
            "action": "page_created", "page_id": row["page_id"], "account": client.account,
            "title": row["title"] or title, "parent_id": database_id or parent_page_id,
            "link": row["url"], "changed": ", ".join(shown),
            "detail": {"sent": shown, "paragraphs": len(paragraphs)}})
        return {"page_id": row["page_id"], "url": row["url"], "title": row["title"] or title,
                "parent": PageRow.parent(created) or ("database " + database_id if database_id
                                                      else "page " + parent_page_id),
                "write_ref": write_ref}, "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        page_id = str(call.inputs["page_id"]).strip()
        title = str(call.inputs.get("title") or "").strip()
        values = dict(call.inputs.get("properties") or {})
        if not values and not title:
            return invalid("Nothing to change: give properties or a title.")
        try:
            page = await client.page(page_id)
            if trashed(page):
                return invalid("That page is in the Notion trash; restore it first (pages_archive with restore).")
            source_id, database_id = PageRow.source_of(page)
            if source_id or database_id:
                schema = await resolve(client, source_id or database_id)
                try:
                    values = self._with_title(schema, values, title)
                    payload, shown = schema.build(values, await people_for(client, schema, values))
                except SchemaProblem as problem:
                    return invalid(str(problem))
            else:
                if values:
                    return invalid("This page is not a database row; only its title "
                                   "can be changed.")
                payload, shown = {"title": {"title": Text.write(title)}}, {"title": title}
            before = page.get("properties") or {}
            previous = {name: PropertyText.render(before[name]) if name in before else ""
                        for name in payload}
            updated = await client.update_page(page_id, {"properties": payload})
        except Several as several:
            return invalid(str(several))
        except HttpError as failed:
            return failed.answer()
        after = updated.get("properties") or {}
        now = {name: PropertyText.render(after[name]) if name in after else shown.get(name, "")
               for name in payload}
        row = PageRow.row(updated, with_properties=False)
        write_ref = await call.records.create("write", {
            "action": "page_updated", "page_id": page_id, "title": row["title"], "account": client.account,
            "parent_id": database_id or PageRow.parent_id(page), "link": row["url"],
            "changed": ", ".join(payload),
            "detail": {"previous": previous, "now": now}})
        return {"page_id": page_id, "url": row["url"], "title": row["title"],
                "changed": list(payload), "previous": previous, "now": now,
                "write_ref": write_ref}, "success"

    async def archive(self, call):
        """A page or row moved to Notion's trash, or back out of it with restore."""
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        page_id = str(call.inputs["page_id"]).strip()
        restore = bool(call.inputs.get("restore"))
        try:
            page = await client.page(page_id)
            if trashed(page) != restore:
                row = PageRow.row(page, with_properties=False)
                return {"page_id": page_id, "title": row["title"], "url": row["url"], "in_trash": trashed(page),
                        "already": True}, "success"
            changed = await client.update_page(page_id, {"in_trash": not restore})
        except HttpError as failed:
            return failed.answer()
        row = PageRow.row(changed or page, with_properties=False)
        write_ref = await call.records.create("write", {
            "action": "page_restored" if restore else "page_archived", "page_id": page_id,
            "title": row["title"], "account": client.account, "parent_id": PageRow.parent_id(page),
            "link": row["url"], "changed": "in_trash", "detail": {"in_trash": not restore}})
        return {"page_id": page_id, "title": row["title"], "url": row["url"], "in_trash": not restore,
                "already": False, "write_ref": write_ref}, "success"
