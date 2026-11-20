"""Word documents: read by paragraph with their comments, and one paragraph's text changed in two steps.

Graph has no API for what is inside a Word document, so the file itself is read (docx_file.py), in a thread off the
agent's loop. A document is fetched again only when its cTag moved: the cTag changes when the content is saved, and
not on a rename, which moves only the eTag.

An edit is proposed and recorded (its text kept encrypted, in values), then applied by a separate call: the file is
fetched fresh, the paragraph checked to read as it did, its words replaced, and the whole file sent back with
``If-Match`` on the recorded cTag, so a save by anyone else in between is refused by Microsoft (412) rather than
overwritten. A document open for editing elsewhere is locked (423): that is said, and the edit stays proposed.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from standard import ResourceDenied, ToolBase
from standard.http import HttpError, human_size

from .common import client, failed, ids, refused
from .docx_file import NotEditable, WordPackage
from .drive import Drive, drive_for, item_row, person

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
#: The largest Word file read here. Its bytes stay inside the program (they are never a chat file), so this only
#: keeps one call's memory and time in bounds.
WORD_MOST = 20 * 1024 * 1024
#: The longest paragraph text an edit records.
TEXT_MOST = 20000
#: Documents kept by (account, drive, item, cTag), so reading one again costs no download while it is unchanged.
CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
CACHE_MOST = 6


def is_docx(item) -> bool:
    return "file" in item and str(item.get("name") or "").lower().endswith(".docx")


def cut(text, most=2000) -> str:
    text = str(text or "")
    return text if len(text) <= most else text[:most] + "…"


async def open_document(drive: Drive, item_id: str, drive_id: str = "", item=None):
    """(item, package) for a Word file, or HttpError saying why not."""
    item = item or await drive.item(item_id, drive_id)
    name = str(item.get("name") or "")
    if not is_docx(item):
        raise HttpError("invalid", f"'{name}' is not a Word (.docx) document.")
    size = int(item.get("size") or 0)
    if size > WORD_MOST:
        raise HttpError("too_large", f"'{name}' is {human_size(size)}, more than the {human_size(WORD_MOST)} this "
                                     f"agent reads.")
    key = (drive.account, drive_id, item_id, str(item.get("cTag") or item.get("eTag") or ""))
    raw = CACHE.get(key)
    if raw is None:
        raw = await drive.fetch(item, drive_id, most=WORD_MOST, what=f"'{name}'")
        CACHE[key] = raw
        while len(CACHE) > CACHE_MOST:
            CACHE.popitem(last=False)
    CACHE.move_to_end(key)
    try:
        package = await asyncio.to_thread(WordPackage, raw)
    except Exception as broken:
        raise HttpError("invalid", f"'{name}' could not be read as a Word document: {broken}") from None
    return item, package


class WordTool(ToolBase):
    id = "word"

    async def find(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        most = int(call.inputs.get("max_results") or 10)
        query = str(call.inputs.get("query") or "").strip() or "docx"
        try:
            answer = await drive.search(query, 50)
        except HttpError as error:
            return failed(error)
        rows = [item_row(i) for i in answer.get("value") or []]
        rows = sorted((r for r in rows if r["kind"] == "file" and r["name"].lower().endswith(".docx")),
                      key=lambda r: r["modified"], reverse=True)
        return {"documents": rows[:most], "more": len(rows) > most}, "success"

    async def read(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        start = int(call.inputs.get("from") or 1)
        most = int(call.inputs.get("max_paragraphs") or 50)
        budget = int(call.inputs.get("max_chars") or 20000)
        try:
            item, package = await open_document(drive, item_id, drive_id)
        except HttpError as error:
            return failed(error)
        total = len(package.paragraphs)
        rows, used = [], 0
        for number in range(start, total + 1):
            row = package.row(number)
            if len(rows) >= most or (rows and used + len(row["text"]) > budget):
                break
            if len(row["text"]) > budget:
                # One paragraph longer than the whole budget is still shown, cut, and says so.
                row["text"], row["truncated"] = row["text"][:budget], True
            rows.append(row)
            used += len(row["text"])
        result = {"item_id": item_id, "name": str(item.get("name") or ""), "version": str(item.get("cTag") or ""),
                  "modified": str(item.get("lastModifiedDateTime") or ""),
                  "modified_by": person(item.get("lastModifiedBy")), "total_paragraphs": total, "from": start,
                  "paragraphs": rows, "comment_count": len(package.comments()), "link": str(item.get("webUrl") or "")}
        if drive_id:
            result["drive_id"] = drive_id
        if start + len(rows) <= total:
            result["next_from"] = start + len(rows)
        return result, "success"

    async def comments(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        start = int(call.inputs.get("from") or 1)
        most = int(call.inputs.get("max_results") or 25)
        try:
            item, package = await open_document(drive, item_id, drive_id)
        except HttpError as error:
            return failed(error)
        found = package.comments()
        rows = [{"comment_id": c["comment_id"], "author": c["author"], "date": c["date"], "text": cut(c["text"]),
                 "paragraph": c["paragraph"], "paragraph_text": cut(c["paragraph_text"], 300)}
                for c in found[start - 1: start - 1 + most]]
        result = {"item_id": item_id, "name": str(item.get("name") or ""), "total": len(found), "from": start,
                  "comments": rows}
        if start - 1 + len(rows) < len(found):
            result["next_from"] = start + len(rows)
        return result, "success"

    async def propose(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        number = int(call.inputs["paragraph"])
        new_text = str(call.inputs["new_text"])
        if "\n" in new_text or "\r" in new_text:
            return refused("new_text is one paragraph and cannot hold a line break; propose each paragraph on its "
                           "own.")
        try:
            item, package = await open_document(drive, item_id, drive_id)
        except HttpError as error:
            return failed(error)
        total = len(package.paragraphs)
        if not 1 <= number <= total:
            return refused(f"The document has {total} paragraphs; there is no paragraph {number}.")
        old_text = package.paragraphs[number - 1].text
        if not package.plain(number):
            return refused(f"Paragraph {number} holds tracked changes, a picture, an object or an equation; it is not "
                           f"rewritten here. Edit it in Word.")
        if len(old_text) > TEXT_MOST:
            return refused(f"Paragraph {number} is {len(old_text):,} characters, more than an edit records "
                           f"({TEXT_MOST:,}).")
        if old_text == new_text:
            return refused(f"Paragraph {number} already reads exactly that.")
        name = str(item.get("name") or "")
        ref = await call.records.create("edit", {
            "item_id": item_id, **({"drive_id": drive_id} if drive_id else {}), "name": name, "paragraph": number,
            "version": str(item.get("cTag") or ""), "status": "proposed",
            **({"account": drive.account} if drive.account else {}),
            "old_text": old_text, "new_text": new_text,
            **({"note": str(call.inputs["note"])} if call.inputs.get("note") else {})})
        return {"edit_ref": ref, "name": name, "paragraph": number, "old_text": old_text, "new_text": new_text,
                "link": str(item.get("webUrl") or "")}, "success"

    async def apply(self, call):
        edit_ref = str(call.inputs["edit_ref"])
        try:
            record = await call.records.read("edit", edit_ref)
        except ResourceDenied:
            return refused("There is no such proposed edit; propose it first.", "not_found")
        fields = record["fields"]
        status = str(fields.get("status") or "")
        if status == "unknown":
            return refused("This edit's last save had no answer from Microsoft. Open the document to see whether it "
                           "landed; it is not sent again.", "unknown")
        if status != "proposed":
            return refused(f"This edit is {status}; only a proposed edit can be applied. Read the document and propose "
                           f"afresh.")
        drive, why = await drive_for(call, fields.get("account") or call.inputs.get("account"))
        if drive is None:
            return refused(why, "auth")
        item_id, drive_id = str(fields.get("item_id") or ""), str(fields.get("drive_id") or "")
        number = int(fields.get("paragraph") or 0)
        old_text, new_text = str(fields.get("old_text") or ""), str(fields.get("new_text") or "")
        version = str(fields.get("version") or "")
        try:
            item = await drive.item(item_id, drive_id)
        except HttpError as error:
            if error.kind == "not_found":
                return await self.refuse(call, edit_ref, "The document is gone (deleted or unshared).", "not_found")
            return failed(error)
        if str(item.get("cTag") or "") != version:
            who = person(item.get("lastModifiedBy")) or "someone"
            return await self.refuse(call, edit_ref, f"The document's content changed after this edit was proposed "
                                                     f"(saved by {who} at {item.get('lastModifiedDateTime')}). Read it "
                                                     f"again and propose afresh.")
        try:
            _, package = await open_document(drive, item_id, drive_id, item)
        except HttpError as error:
            return failed(error)
        paragraph = package.paragraphs[number - 1] if 1 <= number <= len(package.paragraphs) else None
        if paragraph is None or paragraph.text != old_text:
            return await self.refuse(call, edit_ref, f"Paragraph {number} no longer reads as it did when proposed.")
        try:
            raw = await asyncio.to_thread(package.replace_text, number, new_text)
            # The file must still open before it replaces the person's copy.
            await asyncio.to_thread(WordPackage, raw)
        except NotEditable as wrong:
            return await self.refuse(call, edit_ref, str(wrong), "invalid")
        if len(raw) > WORD_MOST:
            return await self.refuse(call, edit_ref, f"The edited document would be {human_size(len(raw))}, over the "
                                                     f"{human_size(WORD_MOST)} this agent edits.", "too_large")
        await call.progress(f"Saving the edit to paragraph {number} of {fields.get('name')}")
        try:
            saved = await drive.replace(item_id, drive_id, raw, version, DOCX)
        except HttpError as error:
            if error.kind == "unknown":
                await call.records.update("edit", edit_ref, {"status": "unknown", "reason": error.message})
                return failed(error)
            if error.kind in ("auth", "scope", "rate", "locked"):
                # Nothing was saved, and the edit may be applied once the document is free again.
                return failed(error, f"'{fields.get('name')}'")
            return await self.refuse(call, edit_ref, error.message, error.kind)
        await call.records.update("edit", edit_ref, {"status": "applied", "version": str(saved.get("cTag") or "")})
        return {"applied": True, "status": "applied", "paragraph": number,
                "link": str(saved.get("webUrl") or item.get("webUrl") or "")}, "success"

    @staticmethod
    async def refuse(call, edit_ref, reason, kind="conflict"):
        await call.records.update("edit", edit_ref, {"status": "refused", "reason": reason})
        return refused(reason + " Nothing in the document was changed.", kind, status="refused")

    async def discard(self, call):
        edit_ref = str(call.inputs["edit_ref"])
        try:
            record = await call.records.read("edit", edit_ref)
        except ResourceDenied:
            return refused("There is no such proposed edit.", "not_found")
        if str(record["fields"].get("status") or "") == "applied":
            return refused("This edit is already in the document; discarding the record would not undo it.")
        await call.records.update("edit", edit_ref, {"status": "discarded"})
        return {"discarded": True}, "success"
