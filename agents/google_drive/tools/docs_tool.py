"""Google Docs read as numbered paragraphs across every tab, made new, and added to on the person's go-ahead.

An insert is sent with ``writeControl.requiredRevisionId`` set to the revision it was planned on, so a document someone
changed in between is refused by Google itself rather than written into at the wrong place.
"""

from __future__ import annotations

from standard import ToolBase

from .common import DOCUMENT, ONE, HttpError, connect, failed, find_folder, no_account, refused, segment, u16
from .docs_model import doc_link, paragraphs, stale, tabs

STYLES = ["NORMAL_TEXT", "TITLE", "SUBTITLE", "HEADING_1", "HEADING_2", "HEADING_3", "HEADING_4"]


async def document(docs, document_id: str) -> dict:
    return await docs.get(f"documents/{segment(document_id)}", params={"includeTabsContent": "true"})


class DocsTool(ToolBase):
    id = "docs"

    async def read(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        document_id = str(inputs["document_id"])
        start = int(inputs.get("from") or 1)
        most = int(inputs.get("max_paragraphs") or 60)
        budget = int(inputs.get("max_chars") or 20000)
        wanted_tab = str(inputs.get("tab") or "").strip().lower()
        try:
            doc = await document(clients.docs, document_id)
        except HttpError as error:
            return failed(error)
        found = paragraphs(doc)
        listed = [{"tab_id": tab_id, "title": title,
                   "paragraphs": sum(1 for p in found if p.tab_id == tab_id)} for tab_id, title, _ in tabs(doc)]
        if wanted_tab and not any(wanted_tab in (t["tab_id"].lower(), t["title"].lower()) for t in listed):
            return refused(f"The document has no tab {inputs['tab']!r}; its tabs are "
                           f"{', '.join(repr(t['title']) for t in listed)}.", "not_found")
        rows, used, last, blank = [], 0, start - 1, 0
        for paragraph in found[start - 1:]:
            if wanted_tab and wanted_tab not in (paragraph.tab_id.lower(), paragraph.tab.lower()):
                last = paragraph.number
                continue
            if not paragraph.text.strip():
                # An empty line is not a paragraph a person would count (seen live: "the first five paragraphs"
                # came back as a title and three blanks); its number stays, so edits still name the right one.
                if not rows or len(rows) < most:
                    blank += 1
                    last = paragraph.number
                continue
            if len(rows) >= most or (rows and used + len(paragraph.text) > budget):
                break
            row = paragraph.row()
            if len(row["text"]) > budget:
                # A paragraph longer than the whole budget is still shown, cut, and says so.
                row["text"], row["truncated"] = row["text"][:budget], True
            rows.append(row)
            used += len(row["text"])
            last = paragraph.number
        result = {"document_id": str(doc.get("documentId") or document_id), "title": str(doc.get("title") or ""),
                  "revision_id": str(doc.get("revisionId") or ""), "link": doc_link(document_id),
                  "tabs": listed, "total_paragraphs": len(found), "from": start, "paragraphs": rows,
                  **({"blank_skipped": blank} if blank else {})}
        if last < len(found) and any(not wanted_tab or wanted_tab in (p.tab_id.lower(), p.tab.lower())
                                     for p in found[last:]):
            result["next_from"] = last + 1
        return result, "success"

    async def create(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        title = str(inputs["title"]).strip()
        lines = [str(p) for p in inputs.get("paragraphs") or []]
        if any("\n" in p or "\r" in p for p in lines):
            return refused("Each paragraph is one line; give several paragraphs instead of line breaks.")
        try:
            body = {"name": title, "mimeType": DOCUMENT}
            folder = None
            if inputs.get("folder_id") or inputs.get("folder_path"):
                folder = await find_folder(clients.drive, inputs.get("folder_id"), inputs.get("folder_path"))
                body["parents"] = [folder["id"]]
            made = await clients.drive.post("files", params={**ONE, "fields": "id,name,webViewLink"}, json=body)
            document_id = str(made.get("id") or "")
            if lines:
                await clients.docs.post(f"documents/{segment(document_id)}:batchUpdate", json={"requests": [
                    {"insertText": {"location": {"index": 1}, "text": "\n".join(lines)}}]})
        except HttpError as error:
            return failed(error)
        return {"document_id": document_id, "title": title, "link": doc_link(document_id),
                "paragraphs": len(lines), **({"folder": str(folder.get("name") or "")} if folder else {})}, "success"

    async def insert(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        document_id = str(inputs["document_id"])
        lines = [str(p) for p in inputs["paragraphs"]]
        if any("\n" in p or "\r" in p for p in lines):
            return refused("Each paragraph is one line; give several paragraphs instead of line breaks.")
        style = str(inputs.get("style") or "NORMAL_TEXT")
        after = inputs.get("after_paragraph")
        try:
            doc = await document(clients.docs, document_id)
        except HttpError as error:
            return failed(error)
        found = paragraphs(doc)
        revision = str(doc.get("revisionId") or "")
        wanted_tab = str(inputs.get("tab") or "").strip().lower()
        if after:
            if not 1 <= int(after) <= len(found):
                return refused(f"The document has {len(found)} paragraphs; there is no paragraph {after}.")
            anchor = found[int(after) - 1]
            if anchor.part != "body" or anchor.in_table:
                return refused(f"Paragraph {after} is in a {'table' if anchor.in_table else anchor.part}; new "
                               f"paragraphs go after a paragraph of the body.")
            tab_id = anchor.tab_id
        else:
            listed = tabs(doc)
            chosen = next((t for t in listed if wanted_tab in (t[0].lower(), t[1].lower())), None) if wanted_tab \
                else listed[0]
            if chosen is None:
                return refused(f"The document has no tab {inputs['tab']!r}.", "not_found")
            tab_id = chosen[0]
        body = [p for p in found if p.tab_id == tab_id and p.part == "body" and not p.in_table]
        if not body:
            return refused("That tab has no body to add to.")
        joined = "\n".join(lines)
        if after == 0:
            index, text, first = body[0].start, joined + "\n", body[0].start
        elif after and found[int(after) - 1] is not body[-1]:
            anchor = found[int(after) - 1]
            index, text, first = anchor.end, joined + "\n", anchor.end
        else:
            # After the last paragraph: before its own newline, which the segment must keep at its end.
            index, text, first = body[-1].end - 1, "\n" + joined, body[-1].end
        where = {"tabId": tab_id} if tab_id else {}
        requests = [{"insertText": {"location": {"index": index, **where}, "text": text}},
                    {"updateParagraphStyle": {"range": {"startIndex": first, "endIndex": first + u16(joined) + 1,
                                                        **where},
                                              "paragraphStyle": {"namedStyleType": style},
                                              "fields": "namedStyleType"}}]
        await call.progress(f"Adding {len(lines)} paragraph(s) to {doc.get('title')}")
        try:
            answer = await clients.docs.post(f"documents/{segment(document_id)}:batchUpdate", json={
                "requests": requests, "writeControl": {"requiredRevisionId": revision}})
        except HttpError as error:
            if stale(error):
                return refused("The document changed while this was being added, so Google refused it and nothing "
                               "was written. Read it again and add afresh.", "conflict")
            return failed(error)
        return {"inserted": len(lines), "document_id": document_id,
                "revision_id": str((answer.get("writeControl") or {}).get("requiredRevisionId") or ""),
                "link": doc_link(document_id)}, "success"
