"""Watches: a Word document's saves and comments, an Excel table's new rows, a form's new responses.

A watch is a record of the person's (``status`` active or closed) that remembers where it got to: a document's cTag
(which moves when its content is saved, not when it is renamed) and the comments it had seen; a table's row count;
a form's highest response id. ``watches_check`` reads every active watch through ``standard.watch``: each on its
own, a failing one listed in ``problems`` with its place kept, a gone one closed, and every place written only once
all were read. It is schedulable: level 1, no model, held to a time budget well inside its timeout.
"""

from __future__ import annotations

from standard import ResourceDenied, ToolBase
from standard.http import HttpError
from standard.watch import Gone, Seen, active, check, close, listing

from .common import client, failed, ids, refused, work_only
from .drive import drive_for, person
from .excel_tool import WHAT as EXCEL
from .forms_tool import WHAT as FORMS
from .responses import response_table, responses
from .word_tool import cut, open_document
from .workbook import Workbook, blank, keyed

BUDGET = 80.0
#: Rows (or comments, or responses) one watch hands on per check; the rest wait for the next.
PER_WATCH = 20
#: Rows read before the remembered count when looking for new responses, for rows deleted since.
LOOKBACK = 25
KEYS_MOST = 19000


def comment_keys(package) -> str:
    # 17 characters a comment: past some 1,100 comments the oldest fall out and could be reported once more.
    return ",".join(c["key"] for c in package.comments())[-KEYS_MOST:].lstrip(",")


def label(watch) -> str:
    fields = watch.get("fields") or {}
    where = fields.get("table") or fields.get("worksheet") or ""
    return f"{fields.get('kind', 'watch')}: {fields.get('name', '')}" + (f" ({where})" if where else "")


class WatchesTool(ToolBase):
    id = "watches"

    async def same(self, call, fields):
        """An active watch already made for the same thing: never a second one."""
        for record in active(await call.records.list("watch", {"kind": fields["kind"],
                                                               "item_id": fields["item_id"]})):
            kept = record["fields"]
            if all(str(kept.get(k) or "") == str(fields.get(k) or "") for k in ("drive_id", "table", "worksheet")):
                return record
        return None

    async def made(self, call, fields, shown):
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        found = await self.same(call, fields)
        if found is not None:
            return {"watch_ref": found["ref"], "already": True, **shown}, "success"
        if call.inputs.get("note"):
            fields["note"] = str(call.inputs["note"])
        ref = await call.records.create("watch", {**fields, "status": "active"})
        return {"watch_ref": ref, "already": False, **shown}, "success"

    # -- making watches ----------------------------------------------------------------------------------------
    async def document(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        try:
            item, package = await open_document(drive, item_id, drive_id)
        except HttpError as error:
            return failed(error)
        name = str(item.get("name") or "")
        return await self.made(call, {
            "kind": "document", "item_id": item_id, "drive_id": drive_id, "name": name, "account": drive.account,
            "version": str(item.get("cTag") or ""), "modified": str(item.get("lastModifiedDateTime") or ""),
            "comment_keys": comment_keys(package)}, {"name": name, "comments": len(package.comments())})

    async def rows(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call, "workbook_id")
        table = str(call.inputs.get("table") or "").strip()
        worksheet = str(call.inputs.get("worksheet") or "").strip()
        if not table and not worksheet:
            return refused("Name the table to watch (preferred) or the worksheet.")
        try:
            refusal = await work_only(drive, EXCEL)
            if refusal:
                return refusal
            item = await drive.item(item_id, drive_id)
            book = Workbook(drive, item_id, drive_id)
            region = await (book.table_region(table) if table else book.sheet_region(worksheet))
        except HttpError as error:
            return failed(error)
        name = str(item.get("name") or "")
        return await self.made(call, {
            "kind": "rows", "item_id": item_id, "drive_id": drive_id, "name": name, "account": drive.account,
            "table": region.name if table else "", "worksheet": region.worksheet, "cursor": region.rows},
            {"name": name, "table": region.name if table else "", "worksheet": region.worksheet, "rows": region.rows,
             "headers": region.headers})

    async def responses(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call, "workbook_id")
        try:
            refusal = await work_only(drive, FORMS)
            if refusal:
                return refusal
            item = await drive.item(item_id, drive_id)
            book = Workbook(drive, item_id, drive_id)
            hit = await response_table(book, str(call.inputs.get("table") or ""))
            if hit is None:
                return refused("This workbook has no Forms response table to watch.", "not_found")
            region, layout = hit
            seen = await responses(book, region, layout, max(0, region.rows - LOOKBACK), region.rows)
        except HttpError as error:
            return failed(error)
        name = str(item.get("name") or "")
        last_id = max([r["response_id"] for r in seen] or [0])
        return await self.made(call, {
            "kind": "responses", "item_id": item_id, "drive_id": drive_id, "name": name, "account": drive.account,
            "table": region.name, "last_id": last_id, "cursor": region.rows},
            {"name": name, "table": region.name, "last_id": last_id, "responses": region.rows})

    # -- checking ----------------------------------------------------------------------------------------------
    async def check(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        try:
            if wanted:
                watches = active([await call.records.read("watch", wanted)])
            else:
                watches = active(await call.records.list("watch"))
        except ResourceDenied as refusal:
            return refused(f"That watch could not be read: {refusal}", "not_found")
        account = str(call.inputs.get("account") or "").strip().lower()
        if account:
            watches = [w for w in watches if account in str(w["fields"].get("account") or "").lower()]
        most = int(call.inputs.get("max_results") or PER_WATCH)
        drives: dict[str, object] = {}

        async def read(watch):
            fields = watch["fields"]
            named = str(fields.get("account") or "")
            drive = drives.get(named)
            if drive is None:
                drive, why = await drive_for(call, named or None, ask=False)
                if drive is None:
                    raise HttpError("auth", why)
                drives[named] = drive
            kind = fields.get("kind")
            if kind == "document":
                # A listing carries keys only: the comments seen are kept encrypted, so the watch is read whole.
                if "comment_keys" not in fields:
                    watch = await call.records.read("watch", watch["ref"])
                return await self.document_seen(drive, watch, most)
            if kind == "rows":
                return await self.rows_seen(drive, watch, most)
            return await self.responses_seen(drive, watch, most)

        found = await check(call, "watch", watches, read, BUDGET, label)
        split: dict[str, list] = {"document": [], "comment": [], "row": [], "reset": [], "response": []}
        for row in found.pop("rows"):
            split[row.pop("kind")].append(row)
        return {**found, "documents": split["document"], "comments": split["comment"], "rows": split["row"],
                "reset": split["reset"], "responses": split["response"]}, "success"

    @staticmethod
    async def document_seen(drive, watch, most):
        fields = watch["fields"]
        item_id, drive_id = str(fields.get("item_id") or ""), str(fields.get("drive_id") or "")
        item = await drive.item(item_id, drive_id)
        if "deleted" in item:
            raise Gone("The document was deleted.")
        version = str(item.get("cTag") or "")
        if version == str(fields.get("version") or ""):
            # Unchanged content: a rename, a move or a share moves only the eTag, and is not news.
            return Seen()
        item, package = await open_document(drive, item_id, drive_id, item)
        me = await drive.me()
        seen = set(str(fields.get("comment_keys") or "").split(","))
        own = (me.name or "").strip().lower()
        fresh = [c for c in package.comments() if c["key"] not in seen and (not own or c["author"].strip().lower()
                                                                             != own)]
        modifier = str((((item.get("lastModifiedBy") or {}).get("user") or {}).get("email")) or "").lower()
        by_me = bool(modifier) and modifier in me.mine
        rows = []
        if fresh or not by_me:
            rows.append({"kind": "document", "watch_ref": watch["ref"], "item_id": item_id, "name": str(item.get("name")
                                                                                                    or ""),
                         "modified": str(item.get("lastModifiedDateTime") or ""),
                         "modified_by": person(item.get("lastModifiedBy")), "new_comments": len(fresh),
                         "link": str(item.get("webUrl") or "")})
            rows += [{"kind": "comment", "watch_ref": watch["ref"], "item_id": item_id, "author": c["author"],
                      "date": c["date"], "text": cut(c["text"]), "paragraph": c["paragraph"],
                      "paragraph_text": cut(c["paragraph_text"], 300)} for c in fresh[:most]]
        return Seen(rows, {"version": version, "modified": str(item.get("lastModifiedDateTime") or ""),
                           "comment_keys": comment_keys(package)})

    @staticmethod
    async def rows_seen(drive, watch, most):
        fields = watch["fields"]
        book = Workbook(drive, str(fields.get("item_id") or ""), str(fields.get("drive_id") or ""))
        table, worksheet = str(fields.get("table") or ""), str(fields.get("worksheet") or "")
        region = await (book.table_region(table) if table else book.sheet_region(worksheet))
        cursor = int(fields.get("cursor") or 0)
        common = {"watch_ref": watch["ref"], "workbook": str(fields.get("name") or ""), "table": table,
                  "worksheet": region.worksheet}
        if region.rows < cursor:
            # Rows were deleted or moved: what lies past the old count is not new, and what is new cannot be told
            # apart. Nothing is reported for this watch this time; it counts again from here.
            what = f"table {region.name}" if table else f"worksheet {region.worksheet}"
            return Seen([{"kind": "reset", **common, "rows": region.rows,
                          "note": f"The {what} in {common['workbook']} now has {region.rows} rows, fewer than the "
                                  f"{cursor} it had at the last check: nothing is reported for it this time, and the "
                                  f"watch counts again from here."}], {"cursor": region.rows})
        stop = min(region.rows, cursor + most)
        lines = await book.read_rows(region, cursor, stop)
        rows = [{"kind": "row", **common, "row_number": region.row_number(cursor + i),
                 "values": keyed(region.headers, line)} for i, line in enumerate(lines) if not blank(line)]
        return Seen(rows, {"cursor": stop} if stop > cursor else None, more=region.rows > stop)

    @staticmethod
    async def responses_seen(drive, watch, most):
        fields = watch["fields"]
        book = Workbook(drive, str(fields.get("item_id") or ""), str(fields.get("drive_id") or ""))
        hit = await response_table(book, str(fields.get("table") or ""))
        if hit is None:
            raise Gone("The workbook no longer has its Forms response table.")
        region, layout = hit
        last_id, seen_rows = int(fields.get("last_id") or 0), int(fields.get("cursor") or 0)
        base = min(seen_rows, region.rows)
        # The id is the cursor, not the row count: a person tidying the workbook deletes rows, so the read starts a
        # little before the remembered count and the id decides what is new.
        start, stop = max(0, base - LOOKBACK), min(region.rows, base + most + 1)
        fresh = sorted((r for r in await responses(book, region, layout, start, stop) if r["response_id"] > last_id),
                       key=lambda r: r["response_id"])
        more = len(fresh) > most or stop < region.rows
        fresh = fresh[:most]
        row_count = (max(r["row_number"] for r in fresh) - region.header_row) if len(fresh) == most and more \
            else stop
        rows = [{"kind": "response", "watch_ref": watch["ref"], "workbook": str(fields.get("name") or ""),
                 "table": region.name, **r} for r in fresh]
        cursor = {"last_id": fresh[-1]["response_id"] if fresh else last_id, "cursor": row_count}
        changed = (cursor["last_id"], cursor["cursor"]) != (last_id, seen_rows)
        return Seen(rows, cursor if changed else None, more=more)

    # -- the person's watches ----------------------------------------------------------------------------------
    async def list(self, call):
        def describe(record):
            fields = record["fields"]
            return {"kind": str(fields.get("kind") or ""), "name": str(fields.get("name") or ""),
                    **({"table": str(fields["table"])} if fields.get("table") else {}),
                    **({"worksheet": str(fields["worksheet"])} if fields.get("worksheet") else {}),
                    **({"account": str(fields["account"])} if fields.get("account") else {}),
                    "item_id": str(fields.get("item_id") or "")}
        found = await listing(call, "watch", describe, bool(call.inputs.get("include_closed")))
        return {"watches": found}, "success"

    async def close(self, call):
        try:
            return await close(call, "watch", str(call.inputs["watch_ref"])), "success"
        except ResourceDenied:
            return refused("There is no such watch; watches_list shows them.", "not_found")
