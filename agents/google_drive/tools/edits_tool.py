"""Edits to a Google Doc: proposed as a record, applied to the document only by a separate call.

A proposal keeps one paragraph's text as it was read, the text to put in its place (both in the record's encrypted
values), and the document's revision then. Nothing in the document changes. Applying reads the document again and
sends the change with ``writeControl.requiredRevisionId`` set to the recorded revision, so a document someone changed
in the meantime is refused by Google itself rather than overwritten. The range comes from that fresh read, never
from the proposal; a paragraph in a header, footer, footnote or another tab is addressed by its tab and segment.
"""

from __future__ import annotations

from standard import ToolBase

from .common import HttpError, connect, failed, no_account, refused, segment
from .docs_model import doc_link, paragraphs, stale
from .docs_tool import document

TEXT_MOST = 8000


class EditsTool(ToolBase):
    id = "edits"

    async def propose(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        document_id = str(call.inputs["document_id"])
        number = int(call.inputs["paragraph"])
        new_text = str(call.inputs["new_text"])
        if "\n" in new_text or "\r" in new_text:
            return refused("new_text is one paragraph and cannot hold a line break; propose each paragraph on its "
                           "own.")
        try:
            doc = await document(clients.docs, document_id)
        except HttpError as error:
            return failed(error)
        found = paragraphs(doc)
        if not 1 <= number <= len(found):
            return refused(f"The document has {len(found)} paragraphs; there is no paragraph {number}.")
        paragraph = found[number - 1]
        if not paragraph.plain:
            return refused(f"Paragraph {number} holds an image, a chip, a footnote mark or another object besides "
                           f"text; it is not rewritten here. Edit it in Google Docs: {doc_link(document_id)}")
        if len(paragraph.text) > TEXT_MOST or len(new_text) > TEXT_MOST:
            return refused(f"Paragraph {number} is {len(paragraph.text):,} characters and the new text "
                           f"{len(new_text):,}; an edit holds at most {TEXT_MOST:,} each.")
        if paragraph.text == new_text:
            return refused(f"Paragraph {number} already reads exactly that.")
        title, revision = str(doc.get("title") or ""), str(doc.get("revisionId") or "")
        edit_ref = await call.records.create("edit", {
            "document_id": document_id, "paragraph": number, "tab_id": paragraph.tab_id,
            "segment_id": paragraph.segment_id, "revision_id": revision, "status": "proposed",
            "account": clients.address, "title": title, "old_text": paragraph.text, "new_text": new_text,
            "note": str(call.inputs.get("note") or "")})
        return {"edit_ref": edit_ref, "title": title, "paragraph": number, "tab": paragraph.tab,
                "part": paragraph.part, "old_text": paragraph.text, "new_text": new_text, "revision_id": revision,
                "link": doc_link(document_id)}, "success"

    async def apply(self, call):
        edit_ref = str(call.inputs["edit_ref"])
        fields = (await call.records.read("edit", edit_ref))["fields"]
        clients, why = await connect(call, wanted=call.inputs.get("account") or fields.get("account"))
        if clients is None:
            return no_account(why)
        status = str(fields.get("status") or "")
        if status == "unknown":
            return refused("This edit's last apply got no answer from Google. Open the document to see whether it "
                           "landed; it is not sent again.", "unknown")
        if status != "proposed":
            return refused(f"This edit is {status}; only a proposed edit can be applied. Read the document and "
                           f"propose afresh.")
        document_id = str(fields.get("document_id") or "")
        number = int(float(fields.get("paragraph") or 0))
        old_text, new_text = str(fields.get("old_text") or ""), str(fields.get("new_text") or "")
        revision = str(fields.get("revision_id") or "")
        try:
            doc = await document(clients.docs, document_id)
        except HttpError as error:
            if error.kind == "not_found":
                return await self._refuse(call, edit_ref, "The document is gone, or this account can no longer "
                                                          "open it.", "not_found")
            return failed(error)
        if str(doc.get("revisionId") or "") != revision:
            return await self._refuse(call, edit_ref, "The document changed after this edit was proposed (or the "
                                                      "proposal is over a day old, past which Google may not honour "
                                                      "its revision). Read it again and propose afresh.")
        found = paragraphs(doc)
        paragraph = found[number - 1] if 1 <= number <= len(found) else None
        if (paragraph is None or paragraph.text != old_text or not paragraph.plain
                or paragraph.tab_id != str(fields.get("tab_id") or "")
                or paragraph.segment_id != str(fields.get("segment_id") or "")):
            return await self._refuse(call, edit_ref, f"Paragraph {number} no longer reads as it did when proposed.")
        where = {**({"tabId": paragraph.tab_id} if paragraph.tab_id else {}),
                 **({"segmentId": paragraph.segment_id} if paragraph.segment_id else {})}
        requests = []
        if paragraph.text:
            # Up to, not including, the paragraph's own newline: the paragraph and its style stay.
            requests.append({"deleteContentRange": {"range": {"startIndex": paragraph.start,
                                                              "endIndex": paragraph.end - 1, **where}}})
        if new_text:
            requests.append({"insertText": {"location": {"index": paragraph.start, **where}, "text": new_text}})
        await call.progress(f"Applying the edit to paragraph {number} of {fields.get('title')}")
        try:
            answer = await clients.docs.post(f"documents/{segment(document_id)}:batchUpdate", json={
                "requests": requests, "writeControl": {"requiredRevisionId": revision}})
        except HttpError as error:
            if error.kind == "unknown":
                await call.records.update("edit", edit_ref, {"status": "unknown", "reason": error.message})
                return failed(error)
            if stale(error):
                return await self._refuse(call, edit_ref, "Someone changed the document while the edit was being "
                                                          "sent, so Google refused it.")
            if error.kind in ("auth", "scope", "rate"):
                return failed(error)
            return await self._refuse(call, edit_ref, error.message, error.kind)
        applied = str((answer.get("writeControl") or {}).get("requiredRevisionId") or "")
        await call.records.update("edit", edit_ref, {"status": "applied", "applied_revision_id": applied})
        return {"applied": True, "status": "applied", "paragraph": number, "revision_id": applied,
                "link": doc_link(document_id)}, "success"

    @staticmethod
    async def _refuse(call, edit_ref, reason, kind="conflict"):
        await call.records.update("edit", edit_ref, {"status": "refused", "reason": reason})
        return {"error": reason + " Nothing in the document was changed.", "kind": kind, "status": "refused"}, "error"

    async def discard(self, call):
        edit_ref = str(call.inputs["edit_ref"])
        status = str((await call.records.read("edit", edit_ref))["fields"].get("status") or "")
        if status == "applied":
            return refused("This edit is already in the document; discarding the record would not undo it.")
        await call.records.update("edit", edit_ref, {"status": "discarded"})
        return {"discarded": True}, "success"
