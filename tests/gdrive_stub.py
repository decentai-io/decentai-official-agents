"""A loopback Google: the Drive v3 (and its upload base), Docs v1, Sheets v4, Forms v1 and userinfo subset the Google
Drive agent calls, over real HTTP, on one port, told apart by host as Google's are: www.googleapis.com,
docs.googleapis.com, sheets.googleapis.com, forms.googleapis.com. It holds a small fictional office (Sidra Office
Supplies) and is as strict as Google where the agent depends on it:

- **Shared drives:** a request naming a shared-drive file without ``supportsAllDrives=true`` is 404, as Google answers;
  a listing sees shared-drive files only with ``includeItemsFromAllDrives=true`` and the right ``corpora``
  (``allDrives``, or ``drive`` with its ``driveId``); ``corpora=user`` never sees a shared drive's files.
- **Queries:** ``orderBy`` beside a ``fullText`` term is 400; names are not unique in a folder.
- **Uploads:** multipart is refused over 5 MB (413); a resumable session is opened with a POST that answers a
  ``Location``, then the bytes are PUT there.
- **Rate limits:** ``fail()`` makes the next matching requests answer 429 (with ``Retry-After``) or 403
  ``userRateLimitExceeded``, or anything else.
- **Docs:** without ``includeTabsContent=true`` only the first tab comes back; indexes count UTF-16 units; a
  ``batchUpdate`` whose ``requiredRevisionId`` is not the latest is 400 ``FAILED_PRECONDITION`` and changes nothing; a
  batch is all or nothing; an index at or past a segment's end is refused.
- **Sheets:** a range past the grid is 400; ``valueInputOption`` is required; RAW stores text as text, USER_ENTERED
  turns "=…" into a formula and "12" into a number; values come back trimmed, formatted in the spreadsheet's locale.
- **Forms:** responses come in no particular order; the page token is the service's own and must come back with the
  same filter.
- **Comments** answer only when ``fields`` is given. **Changes** follow a page token from ``startPageToken``.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from tests.conftest import stands_for

FOLDER = "application/vnd.google-apps.folder"
NATIVE = {"document": "application/vnd.google-apps.document",
          "spreadsheet": "application/vnd.google-apps.spreadsheet",
          "presentation": "application/vnd.google-apps.presentation",
          "form": "application/vnd.google-apps.form"}
ROOT = "0AroOtMyDrive"
VALUE = r"'((?:[^'\\]|\\.)*)'"
EXPORT_MOST = 10 * 1024 * 1024
MULTIPART_MOST = 5 * 1024 * 1024


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def at_u16(text: str, units: int) -> int:
    """The Python index of a UTF-16 offset into text."""
    count = 0
    for index, char in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + (ord(letter) - 64)
    return number


class Refused(Exception):
    def __init__(self, status: int, message: str, status_name: str = "", reason: str = ""):
        super().__init__(message)
        self.status, self.message, self.status_name, self.reason = status, message, status_name, reason


class GoogleStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {
            "at-1": {"email": self.ACCOUNT, "hd": "sidra.example"}}
        self.files: Dict[str, Dict[str, Any]] = {
            ROOT: {"id": ROOT, "name": "My Drive", "mimeType": FOLDER, "parents": [], "trashed": False}}
        self.drives: Dict[str, Dict[str, Any]] = {}
        self.perms: Dict[str, List[Dict[str, Any]]] = {}
        self.comments: Dict[str, List[Dict[str, Any]]] = {}
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.sheets: Dict[str, Dict[str, Any]] = {}
        self.forms: Dict[str, Dict[str, Any]] = {}
        self.changes: List[Dict[str, Any]] = []
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.faults: List[Dict[str, Any]] = []
        self.log: List[Dict[str, Any]] = []
        self.uploads: List[str] = []
        self.before_batch = None
        self.clock = 0
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self.lock = threading.RLock()

    # -- time and ids ------------------------------------------------------------------------------------------------
    def tick(self) -> str:
        self.clock += 60
        hours, rest = divmod(self.clock, 3600)
        return f"2026-11-{10 + hours // 24:02d}T{hours % 24:02d}:{rest // 60:02d}:{rest % 60:02d}.000Z"

    def new_id(self, prefix: str = "1dRv") -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}x"

    def add_account(self, email: str, token: str, hd: str = "") -> Dict[str, Any]:
        self.accounts[token] = {"email": email, **({"hd": hd} if hd else {})}
        return {"account": email, "access_token": token, "api_base_url": self.url}

    # -- files -------------------------------------------------------------------------------------------------------
    def stamp(self, name: str, mime: str, parent: Optional[str], by: str = "", **extra) -> Dict[str, Any]:
        item_id = extra.pop("id", None) or self.new_id()
        parent_item = self.files.get(parent or "")
        when = self.tick()
        by = by or self.ACCOUNT
        item = {"id": item_id, "name": name, "mimeType": mime, "parents": [parent] if parent else [],
                "trashed": False, "modifiedTime": when, "createdTime": when,
                "lastModifyingUser": {"emailAddress": by, "displayName": by.split("@")[0].title()},
                "owners": [{"emailAddress": by, "displayName": by.split("@")[0].title()}], **extra}
        if parent_item and parent_item.get("driveId"):
            item["driveId"] = parent_item["driveId"]
            item.pop("owners")
        self.files[item_id] = item
        self.perms[item_id] = [] if item.get("driveId") else [
            {"id": "owner-" + item_id, "type": "user", "role": "owner", "emailAddress": by}]
        self.changed(item_id)
        return item

    def changed(self, item_id: str, removed: bool = False) -> None:
        item = self.files.get(item_id) or {}
        self.changes.append({"fileId": item_id, "time": item.get("modifiedTime") or self.tick(), "removed": removed,
                             "driveId": item.get("driveId")})

    def touch(self, item: Dict[str, Any], by: str) -> None:
        item["modifiedTime"] = self.tick()
        item["lastModifyingUser"] = {"emailAddress": by, "displayName": by.split("@")[0].title()}
        item["_revisions"] = item.get("_revisions", []) + [{"id": str(len(item.get("_revisions", [])) + 1),
                                                            "modifiedTime": item["modifiedTime"],
                                                            "lastModifyingUser": item["lastModifyingUser"],
                                                            **({"size": item["size"]} if "size" in item else {})}]
        self.changed(item["id"])

    def add_drive(self, name: str) -> str:
        drive_id = self.new_id("0AsHaReD")
        self.drives[drive_id] = {"id": drive_id, "name": name}
        self.files[drive_id] = {"id": drive_id, "name": name, "mimeType": FOLDER, "parents": [], "driveId": drive_id,
                                "trashed": False}
        self.perms[drive_id] = []
        return drive_id

    def add_folder(self, path: str, root: str = ROOT) -> str:
        parent = root
        for part in [p for p in path.split("/") if p]:
            found = next((f for f in self.files.values() if parent in f["parents"] and f["name"] == part
                          and f["mimeType"] == FOLDER), None)
            parent = (found or self.stamp(part, FOLDER, parent))["id"]
        return parent

    def add_file(self, folder: str, name: str, content: bytes, mime: str = "application/pdf", root: str = ROOT,
                 by: str = "") -> str:
        item = self.stamp(name, mime, self.add_folder(folder, root), by=by, size=str(len(content)))
        item["_content"] = content
        return item["id"]

    def share_with_me(self, name: str, content: bytes, by: str) -> str:
        item = self.stamp(name, "application/pdf", None, by=by, size=str(len(content)))
        item.update(_content=content, _shared_with_me=True, shared=True,
                    sharingUser={"emailAddress": by, "displayName": by})
        return item["id"]

    def by_path(self, path: str, root: str = ROOT) -> Optional[Dict[str, Any]]:
        item: Optional[Dict[str, Any]] = self.files[root]
        for part in [p for p in path.split("/") if p]:
            item = next((f for f in self.files.values() if item["id"] in f["parents"] and f["name"] == part
                         and not f["trashed"]), None)
            if item is None:
                return None
        return item

    # -- docs --------------------------------------------------------------------------------------------------------
    def add_document(self, folder: str, title: str, tabs: List[Dict[str, Any]], root: str = ROOT) -> str:
        """tabs: [{"title", "body": blocks, "headers": {id: [paragraphs]}, "footers": …, "footnotes": …}]; a block is
        ("STYLE", "text"), ("STYLE", "text", {"image": True} or {"footnote": "fn1"}), or {"table": [[["cell"]]]}."""
        item = self.stamp(title, NATIVE["document"], self.add_folder(folder, root))
        model = []
        for n, tab in enumerate(tabs):
            model.append({"id": tab.get("id") or f"t.{n}", "title": tab.get("title") or f"Tab {n + 1}",
                          "body": [self._block(b) for b in tab.get("body") or [("NORMAL_TEXT", "")]],
                          **{k: {sid: [self._block(b) for b in blocks] for sid, blocks in (tab.get(k) or {}).items()}
                             for k in ("headers", "footers", "footnotes")}})
        self.docs[item["id"]] = {"revision": 1, "tabs": model}
        return item["id"]

    @staticmethod
    def _block(block: Any) -> Dict[str, Any]:
        if isinstance(block, dict) and "table" in block:
            return {"table": [[[{"style": "NORMAL_TEXT", "text": t} for t in cell] for cell in row]
                              for row in block["table"]]}
        style, text = block[0], block[1]
        extra = block[2] if len(block) > 2 else {}
        return {"style": style, "text": text, **extra}

    def doc_text(self, doc_id: str, tab: int = 0) -> List[str]:
        return [p["text"] for p in self._paragraphs(self.docs[doc_id]["tabs"][tab]["body"])]

    def edit_as(self, doc_id: str, tab: int, number: int, text: str) -> None:
        """Someone else changes a body paragraph in the Docs editor."""
        self._paragraphs(self.docs[doc_id]["tabs"][tab]["body"])[number - 1]["text"] = text
        self.docs[doc_id]["revision"] += 1

    @staticmethod
    def _paragraphs(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        found = []
        for block in blocks:
            if "table" in block:
                for row in block["table"]:
                    for cell in row:
                        found += cell
            else:
                found.append(block)
        return found

    @staticmethod
    def _index(element: Dict[str, Any], start: int, end: int) -> Dict[str, Any]:
        # Google leaves out a zero, so a segment's first element has no startIndex.
        return {**({"startIndex": start} if start else {}), "endIndex": end, **element}

    def layout(self, blocks: List[Dict[str, Any]], index: int, body: bool) -> Tuple[List, List]:
        """The structural elements of a segment, and (paragraph, start, end) for each paragraph."""
        spans: List[Tuple[Dict[str, Any], int, int]] = []

        def paragraph(p, at):
            start, elements = at, []
            if p["text"]:
                elements.append(self._index({"textRun": {"content": p["text"]}}, at, at + u16(p["text"])))
                at += u16(p["text"])
            if p.get("image"):
                elements.append(self._index({"inlineObjectElement": {"inlineObjectId": "kix.logo"}}, at, at + 1))
                at += 1
            if p.get("footnote"):
                elements.append(self._index({"footnoteReference": {"footnoteId": p["footnote"],
                                                                   "footnoteNumber": "1"}}, at, at + 1))
                at += 1
            elements.append(self._index({"textRun": {"content": "\n"}}, at, at + 1))
            at += 1
            spans.append((p, start, at))
            return self._index({"paragraph": {"elements": elements,
                                              "paragraphStyle": {"namedStyleType": p["style"]}}}, start, at), at

        content: List[Dict[str, Any]] = [{"endIndex": 1, "sectionBreak": {}}] if body else []
        for block in blocks:
            if "table" in block:
                start = index
                index += 1
                rows = []
                for row in block["table"]:
                    index += 1
                    cells = []
                    for cell in row:
                        index += 1
                        cell_content = []
                        for p in cell:
                            element, index = paragraph(p, index)
                            cell_content.append(element)
                        cells.append({"content": cell_content})
                    rows.append({"tableCells": cells})
                index += 1
                content.append(self._index({"table": {"rows": len(rows), "columns": len(block["table"][0]),
                                                      "tableRows": rows}}, start, index))
            else:
                element, index = paragraph(block, index)
                content.append(element)
        return content, spans

    def document_json(self, doc_id: str, tabs: bool) -> Dict[str, Any]:
        doc = self.docs[doc_id]

        def tab_json(tab):
            out = {"body": {"content": self.layout(tab["body"], 1, True)[0]}}
            for key, id_key in (("headers", "headerId"), ("footers", "footerId"), ("footnotes", "footnoteId")):
                if tab.get(key):
                    out[key] = {sid: {id_key: sid, "content": self.layout(blocks, 0, False)[0]}
                                for sid, blocks in tab[key].items()}
            return out

        head = {"documentId": doc_id, "title": self.files[doc_id]["name"], "revisionId": f"ALm37BV-rev{doc['revision']}"}
        if not tabs:
            return {**head, **tab_json(doc["tabs"][0])}
        return {**head, "tabs": [{"tabProperties": {"tabId": t["id"], "title": t["title"], "index": n},
                                  "documentTab": tab_json(t)} for n, t in enumerate(doc["tabs"])]}

    def _segment(self, doc: Dict[str, Any], where: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
        tab_id = where.get("tabId")
        tab = next((t for t in doc["tabs"] if t["id"] == tab_id), None) if tab_id else doc["tabs"][0]
        if tab is None:
            raise Refused(400, f"Invalid tab id: {tab_id}", "INVALID_ARGUMENT")
        segment_id = where.get("segmentId")
        if not segment_id:
            return tab["body"], True
        for key in ("headers", "footers", "footnotes"):
            if segment_id in (tab.get(key) or {}):
                return tab[key][segment_id], False
        raise Refused(400, f"Invalid segment id: {segment_id}", "INVALID_ARGUMENT")

    def apply(self, doc: Dict[str, Any], requests: List[Dict[str, Any]]) -> None:
        """The batch, in order, all or nothing."""
        working = copy.deepcopy(doc)
        for request in requests:
            if "insertText" in request:
                where = request["insertText"]["location"]
                blocks, body = self._segment(working, where)
                spans = self.layout(blocks, 1 if body else 0, body)[1]
                at, text = int(where.get("index") or 0), request["insertText"]["text"]
                for p, start, _ in spans:
                    if start <= at <= start + u16(p["text"]) and not p.get("image") and not p.get("footnote"):
                        cut = at_u16(p["text"], at - start)
                        whole = p["text"][:cut] + text + p["text"][cut:]
                        pieces = whole.split("\n")
                        p["text"] = pieces[0]
                        extra = [{"style": p["style"], "text": piece} for piece in pieces[1:]]
                        for container in [blocks] + [c for b in blocks if "table" in b for r in b["table"] for c in r]:
                            if any(x is p for x in container):
                                position = next(i for i, x in enumerate(container) if x is p)
                                container[position + 1:position + 1] = extra
                        break
                else:
                    raise Refused(400, f"Invalid requests[0].insertText: Index {at} must be less than the end index "
                                       f"of the referenced segment, and inside a paragraph.", "INVALID_ARGUMENT")
            elif "deleteContentRange" in request:
                span = request["deleteContentRange"]["range"]
                blocks, body = self._segment(working, span)
                a, b = int(span.get("startIndex") or 0), int(span["endIndex"])
                if a >= b:
                    raise Refused(400, "Invalid requests[0].deleteContentRange: The range cannot be empty.",
                                  "INVALID_ARGUMENT")
                for p, start, _ in self.layout(blocks, 1 if body else 0, body)[1]:
                    if start <= a and b <= start + u16(p["text"]):
                        p["text"] = p["text"][:at_u16(p["text"], a - start)] + p["text"][at_u16(p["text"], b - start):]
                        break
                else:
                    raise Refused(400, "Invalid deletion range.", "INVALID_ARGUMENT")
            elif "updateParagraphStyle" in request:
                update = request["updateParagraphStyle"]
                blocks, body = self._segment(working, update["range"])
                a, b = int(update["range"].get("startIndex") or 0), int(update["range"]["endIndex"])
                for p, start, end in self.layout(blocks, 1 if body else 0, body)[1]:
                    if start < b and end > a:
                        p["style"] = update["paragraphStyle"]["namedStyleType"]
            else:
                raise Refused(400, f"Unsupported request {list(request)}", "INVALID_ARGUMENT")
        doc["tabs"] = working["tabs"]

    # -- sheets ------------------------------------------------------------------------------------------------------
    def add_spreadsheet(self, folder: str, title: str, tabs: Dict[str, List[List[Any]]], rows: int = 1000,
                        columns: int = 26, locale: str = "en_GB", root: str = ROOT) -> str:
        item = self.stamp(title, NATIVE["spreadsheet"], self.add_folder(folder, root))
        self.sheets[item["id"]] = {"locale": locale, "tabs": []}
        for name, grid in tabs.items():
            self.add_tab(item["id"], name, grid, rows, columns)
        return item["id"]

    def add_tab(self, sheet_id: str, name: str, grid: List[List[Any]], rows: int = 1000, columns: int = 26) -> Dict:
        book = self.sheets[sheet_id]
        tab = {"sheetId": len(book["tabs"]) * 1000 + 7, "title": name, "rows": rows, "columns": columns, "cells": {},
               "formulas": {}}
        for r, row in enumerate(grid, 1):
            for c, value in enumerate(row, 1):
                if value not in (None, ""):
                    tab["cells"][(r, c)] = value
        book["tabs"].append(tab)
        return tab

    def tab(self, sheet_id: str, name: str) -> Dict[str, Any]:
        return next(t for t in self.sheets[sheet_id]["tabs"] if t["title"] == name)

    def grid(self, sheet_id: str, name: str) -> List[List[Any]]:
        tab = self.tab(sheet_id, name)
        last = max([r for r, _ in tab["cells"]] or [0])
        width = max([c for _, c in tab["cells"]] or [0])
        return [[tab["cells"].get((r, c), "") for c in range(1, width + 1)] for r in range(1, last + 1)]

    def delete_row(self, sheet_id: str, name: str, number: int) -> None:
        tab = self.tab(sheet_id, name)
        cells = {}
        for (r, c), v in tab["cells"].items():
            if r < number:
                cells[(r, c)] = v
            elif r > number:
                cells[(r - 1, c)] = v
        tab["cells"] = cells

    def append_row(self, sheet_id: str, name: str, row: List[Any]) -> None:
        tab = self.tab(sheet_id, name)
        last = max([r for r, _ in tab["cells"]] or [0])
        for c, v in enumerate(row, 1):
            if v not in (None, ""):
                tab["cells"][(last + 1, c)] = v

    def _range(self, book: Dict[str, Any], text: str) -> Tuple[Dict[str, Any], int, int, int, int]:
        match = re.match(r"^(?:'((?:[^']|'')*)'|([^!']+))(?:!([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?)?$", text)
        if not match:
            raise Refused(400, f"Unable to parse range: {text}", "INVALID_ARGUMENT")
        title = (match.group(1) or "").replace("''", "'") if match.group(1) is not None else match.group(2)
        tab = next((t for t in book["tabs"] if t["title"] == title), None)
        if tab is None:
            raise Refused(400, f"Unable to parse range: {text}", "INVALID_ARGUMENT")
        c1 = column_number(match.group(3)) if match.group(3) else 1
        r1 = int(match.group(4)) if match.group(4) else 1
        if match.group(5) is None and match.group(3) is not None:
            c2, r2 = (c1, r1) if match.group(4) else (c1, tab["rows"])
        else:
            c2 = column_number(match.group(5)) if match.group(5) else tab["columns"]
            r2 = int(match.group(6)) if match.group(6) else tab["rows"]
        if r2 > tab["rows"] or c2 > tab["columns"]:
            raise Refused(400, f"Range ({text}) exceeds grid limits. Max rows: {tab['rows']}, max columns: "
                               f"{tab['columns']}", "INVALID_ARGUMENT")
        return tab, r1, c1, r2, c2

    def _shown(self, book, tab, r, c, render):
        value = tab["cells"].get((r, c), "")
        if render == "FORMULA" and (r, c) in tab["formulas"]:
            return tab["formulas"][(r, c)]
        if render in ("UNFORMATTED_VALUE", "FORMULA"):
            return value
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            text = f"{value:.2f}" if isinstance(value, float) else str(value)
            return text.replace(".", ",") if book["locale"].startswith(("de", "fr", "ar")) else text
        return str(value)

    def read_range(self, book, text, render="FORMATTED_VALUE") -> Dict[str, Any]:
        tab, r1, c1, r2, c2 = self._range(book, text)
        rows = []
        for r in range(r1, r2 + 1):
            row = [self._shown(book, tab, r, c, render) for c in range(c1, c2 + 1)]
            while row and row[-1] == "":
                row.pop()
            rows.append(row)
        while rows and not rows[-1]:
            rows.pop()
        return {"range": text, "majorDimension": "ROWS", **({"values": rows} if rows else {})}

    def write_cells(self, tab, r1, c1, values, option):
        for dr, row in enumerate(values):
            for dc, value in enumerate(row):
                key = (r1 + dr, c1 + dc)
                tab["formulas"].pop(key, None)
                if value is None:
                    continue
                if option == "USER_ENTERED" and isinstance(value, str):
                    if value.startswith("="):
                        tab["formulas"][key] = value
                        value = "#FORMULA"
                    elif re.fullmatch(r"-?\d+(\.\d+)?", value):
                        value = float(value) if "." in value else int(value)
                if value == "":
                    tab["cells"].pop(key, None)
                else:
                    tab["cells"][key] = value

    # -- forms -------------------------------------------------------------------------------------------------------
    def add_form(self, title: str, items: List[Dict[str, Any]], responses: List[Dict[str, Any]]) -> str:
        item = self.stamp(title, NATIVE["form"], ROOT)
        self.forms[item["id"]] = {"form": {"formId": item["id"], "info": {"title": title},
                                           "responderUri": f"https://docs.google.com/forms/d/e/{item['id']}/viewform",
                                           "items": items}, "responses": list(responses)}
        return item["id"]

    # -- faults ------------------------------------------------------------------------------------------------------
    def fail(self, method: str, path: str, status: int, times: int = 1, reason: str = "", retry_after: str = "",
             message: str = "", host: str = "") -> None:
        """The next ``times`` requests whose path matches answer with this status instead."""
        self.faults.append({"method": method, "path": re.compile(path), "status": status, "times": times,
                            "reason": reason, "retry_after": retry_after, "message": message, "host": host})

    def requests(self, method: str = "", path: str = "") -> List[Dict[str, Any]]:
        return [r for r in self.log if (not method or r["method"] == method) and re.search(path, r["path"])]

    # -- the server --------------------------------------------------------------------------------------------------
    def start(self) -> "GoogleStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status: int, raw: bytes, kind: str = "application/json",
                      headers: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
                self._send(status, json.dumps(body).encode("utf-8"), headers=headers)

            def _error(self, status: int, message: str, status_name: str = "", reason: str = "",
                       headers: Optional[Dict[str, str]] = None) -> None:
                error: Dict[str, Any] = {"code": status, "message": message}
                if status_name:
                    error["status"] = status_name
                if reason:
                    error["errors"] = [{"reason": reason, "message": message, "domain": "usageLimits"}]
                self._json(status, {"error": error}, headers)

            def _handle(self, method: str) -> None:
                url = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(url.path)
                query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
                q = {k: v[0] for k, v in query.items()}
                host = (self.headers.get("Host") or "").split(":")[0]
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                with stub.lock:
                    stub.log.append({"method": method, "host": host, "path": path, "query": q, "token": token,
                                     "ranges": query.get("ranges", []), "size": len(body),
                                     "content_type": self.headers.get("Content-Type") or ""})
                    for fault in stub.faults:
                        if fault["times"] > 0 and fault["method"] == method and fault["path"].search(path) and (
                                not fault["host"] or fault["host"] == host):
                            fault["times"] -= 1
                            headers = {"Retry-After": fault["retry_after"]} if fault["retry_after"] else None
                            return self._error(fault["status"], fault["message"] or "Injected", reason=fault["reason"],
                                               headers=headers)
                    if token not in stub.accounts:
                        return self._error(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED")
                    me = stub.accounts[token]["email"]
                    try:
                        route = {"www.googleapis.com": stub.www, "docs.googleapis.com": stub.docs_api,
                                 "sheets.googleapis.com": stub.sheets_api,
                                 "forms.googleapis.com": stub.forms_api}.get(host)
                        if route is None:
                            raise Refused(404, f"no host {host}")
                        answer = route(method, path, q, query, body, me, self.headers)
                    except Refused as refused:
                        return self._error(refused.status, refused.message, refused.status_name, refused.reason)
                if isinstance(answer, tuple):
                    status, raw, kind, headers = answer
                    return self._send(status, raw, kind, headers)
                if answer is None:
                    return self._send(204, b"")
                self._json(200, answer)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_PATCH(self):  # noqa: N802
                self._handle("PATCH")

            def do_PUT(self):  # noqa: N802
                self._handle("PUT")

            def do_DELETE(self):  # noqa: N802
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    # -- www.googleapis.com ------------------------------------------------------------------------------------------
    def view(self, item: Dict[str, Any], me: str) -> Dict[str, Any]:
        out = copy.deepcopy({k: v for k, v in item.items() if not k.startswith("_")})
        kind = {FOLDER: "drive/folders", NATIVE["document"]: "document/d",
                NATIVE["spreadsheet"]: "spreadsheets/d"}.get(item["mimeType"], "file/d")
        out["webViewLink"] = f"https://docs.google.com/{kind}/{item['id']}/edit"
        for key in ("lastModifyingUser", "sharingUser"):
            if key in out:
                out[key]["me"] = out[key].get("emailAddress") == me
        if [p for p in self.perms.get(item["id"], []) if p["role"] != "owner"]:
            out["shared"] = True
        return out

    def _file(self, item_id: str, q: Dict[str, str]) -> Dict[str, Any]:
        item = self.files.get(ROOT if item_id == "root" else item_id)
        if item is None or (item.get("driveId") and q.get("supportsAllDrives") != "true"):
            raise Refused(404, f"File not found: {item_id}.", "NOT_FOUND", "notFound")
        return item

    def _matches(self, item: Dict[str, Any], query: str) -> bool:
        if item["id"] == ROOT or item["id"] in self.drives:
            return False
        if "trashed = false" in query and item.get("trashed"):
            return False
        if "sharedWithMe = true" in query and not item.get("_shared_with_me"):
            return False
        parent = re.search(VALUE + r" in parents", query)
        if parent:
            wanted = _unescape(parent.group(1))
            if (ROOT if wanted == "root" else wanted) not in item["parents"]:
                return False
        name = re.search(r"name = " + VALUE, query)
        if name and item["name"] != _unescape(name.group(1)):
            return False
        mime = re.search(r"mimeType = " + VALUE, query)
        if mime and item["mimeType"] != _unescape(mime.group(1)):
            return False
        not_mime = re.search(r"mimeType != " + VALUE, query)
        if not_mime and item["mimeType"] == _unescape(not_mime.group(1)):
            return False
        text = re.search(r"name contains " + VALUE, query)
        if text:
            needle = _unescape(text.group(1)).lower()
            content = item.get("_content", b"").decode("utf-8", "replace").lower()
            if item["id"] in self.docs:
                content += " ".join(" ".join(self.doc_text(item["id"], n)) for n in
                                    range(len(self.docs[item["id"]]["tabs"]))).lower()
            if needle not in item["name"].lower() and not ("fullText" in query and needle in content):
                return False
        return True

    def www(self, method, path, q, query, body, me, headers):
        if path == "/oauth2/v3/userinfo":
            account = next(a for a in self.accounts.values() if a["email"] == me)
            return {"sub": "1" + str(abs(hash(me)) % 10 ** 8), "email": me, "email_verified": True,
                    **({"hd": account["hd"]} if account.get("hd") else {})}
        if path.startswith("/upload/drive/v3/"):
            return self.upload(method, path[len("/upload/drive/v3"):], q, body, me, headers)
        if not path.startswith("/drive/v3/"):
            raise Refused(404, f"no route {path}")
        path = path[len("/drive/v3"):]
        if path == "/about" and method == "GET":
            used = sum(len(f.get("_content", b"")) for f in self.files.values() if not f.get("_shared_with_me"))
            return {"user": {"emailAddress": me, "displayName": me.split("@")[0].title()},
                    "storageQuota": {"limit": str(15 * 1024 ** 3), "usage": str(used)}}
        if path == "/drives" and method == "GET":
            return {"drives": list(self.drives.values())}
        match = re.match(r"^/drives/([^/]+)$", path)
        if match:
            if match.group(1) not in self.drives:
                raise Refused(404, "Shared drive not found.", "NOT_FOUND")
            return self.drives[match.group(1)]
        if path == "/changes/startPageToken":
            return {"startPageToken": str(len(self.changes))}
        if path == "/changes":
            return self.change_page(q, me)
        if path == "/files" and method == "GET":
            return self.listing(q, me)
        if path == "/files" and method == "POST":
            data = json.loads(body or b"{}")
            parent = (data.get("parents") or [ROOT])[0]
            self._file(parent, q)
            item = self.stamp(data["name"], data.get("mimeType") or "application/octet-stream",
                              ROOT if parent == "root" else parent, by=me)
            if item["mimeType"] == NATIVE["document"]:
                self.docs[item["id"]] = {"revision": 1, "tabs": [{"id": "t.0", "title": "Tab 1",
                                                                  "body": [{"style": "NORMAL_TEXT", "text": ""}]}]}
            if item["mimeType"] == NATIVE["spreadsheet"]:
                self.sheets[item["id"]] = {"locale": "en_GB", "tabs": []}
                self.add_tab(item["id"], "Sheet1", [])
            return self.view(item, me)
        match = re.match(r"^/files/([^/]+)(/export|/permissions|/permissions/[^/]+|/comments|/comments/[^/]+|"
                         r"/comments/[^/]+/replies|/revisions|/copy)?$", path)
        if not match:
            raise Refused(404, f"no route {path}")
        tail = match.group(2) or ""
        if tail.startswith("/comments"):
            # The comments API has no supportsAllDrives: a shared-drive file's comments are reached without it.
            item = self.files.get(match.group(1))
            if item is None:
                raise Refused(404, f"File not found: {match.group(1)}.", "NOT_FOUND")
            return self.comment_route(method, item, tail, q, body, me)
        if tail == "/revisions":
            item = self.files.get(match.group(1))
            if item is None:
                raise Refused(404, f"File not found: {match.group(1)}.", "NOT_FOUND")
            return {"revisions": item.get("_revisions") or [{"id": "1", "modifiedTime": item.get("modifiedTime"),
                                                             "lastModifyingUser": item.get("lastModifyingUser")}]}
        item = self._file(match.group(1), q)
        if tail == "/copy":
            data = json.loads(body or b"{}")
            parent = (data.get("parents") or item["parents"] or [ROOT])[0]
            made = self.stamp(data.get("name") or f"Copy of {item['name']}", item["mimeType"], parent, by=me,
                              **({"size": item["size"]} if "size" in item else {}))
            if "_content" in item:
                made["_content"] = item["_content"]
            return self.view(made, me)
        if tail.startswith("/permissions"):
            return self.permission_route(method, item, tail, q, body)
        if tail == "/export":
            native = {v: k for k, v in NATIVE.items()}.get(item["mimeType"])
            allowed = {"document": ["application/pdf", "text/plain", "application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document"],
                       "spreadsheet": ["application/pdf", "text/csv", "application/vnd.openxmlformats-"
                                       "officedocument.spreadsheetml.sheet"],
                       "presentation": ["application/pdf", "text/plain", "application/vnd.openxmlformats-"
                                        "officedocument.presentationml.presentation"]}.get(native or "", [])
            if q.get("mimeType") not in allowed:
                raise Refused(400, "The requested conversion is not supported.", "INVALID_ARGUMENT", "badRequest")
            if item.get("_export_size", 0) > EXPORT_MOST:
                raise Refused(403, "This file is too large to be exported.", "", "exportSizeLimitExceeded")
            head = b"%PDF-1.7\n" if q["mimeType"] == "application/pdf" else b"PK\x03\x04"
            raw = head + b" exported " + item["name"].encode("utf-8") + b"\n" + b"x" * item.get("_export_size", 0)
            return 200, raw, q["mimeType"], {}
        if method == "GET" and q.get("alt") == "media":
            if "_content" not in item:
                raise Refused(403, "Only files with binary content can be downloaded. Use Export with Docs Editors "
                                   "files.", "", "fileNotDownloadable")
            return 200, item["_content"], item["mimeType"], {}
        if method == "GET":
            return self.view(item, me)
        if method == "PATCH":
            data = json.loads(body or b"{}")
            for key in ("name", "trashed"):
                if key in data:
                    item[key] = data[key]
            if q.get("removeParents"):
                item["parents"] = [p for p in item["parents"] if p not in q["removeParents"].split(",")]
            if q.get("addParents"):
                target = self._file(q["addParents"], q)
                item["parents"].append(target["id"])
                if target.get("driveId"):
                    item["driveId"] = target["driveId"]
                else:
                    item.pop("driveId", None)
            self.touch(item, me)
            return self.view(item, me)
        raise Refused(404, f"no route {method} {path}")

    def listing(self, q: Dict[str, str], me: str) -> Dict[str, Any]:
        query = q.get("q", "")
        if "fullText" in query and q.get("orderBy"):
            raise Refused(400, "Sorting is not supported for queries with fullText terms. Results are always in "
                               "descending relevance order.", "INVALID_ARGUMENT", "invalid")
        corpora = q.get("corpora", "user")
        every = q.get("includeItemsFromAllDrives") == "true"
        if corpora in ("allDrives", "drive") and not (every and q.get("supportsAllDrives") == "true"):
            raise Refused(403, "The includeItemsFromAllDrives parameter must be set to true when corpora is set to "
                               "allDrives or drive.", "", "teamDriveInclusionRequired")
        if corpora == "drive" and not q.get("driveId"):
            raise Refused(400, "The driveId parameter must be specified if and only if corpora is set to drive.",
                          "INVALID_ARGUMENT", "invalid")
        rows = []
        for item in self.files.values():
            in_shared = bool(item.get("driveId"))
            if corpora == "user" and in_shared:
                continue
            if corpora == "drive" and item.get("driveId") != q["driveId"]:
                continue
            if self._matches(item, query):
                rows.append(item)
        order = q.get("orderBy", "")
        if order == "folder,name":
            rows.sort(key=lambda f: (f["mimeType"] != FOLDER, f["name"].lower()))
        elif order.startswith("modifiedTime desc") or order.startswith("sharedWithMeTime desc"):
            rows.sort(key=lambda f: f.get("modifiedTime") or "", reverse=True)
        start = int(q.get("pageToken") or 0)
        size = int(q.get("pageSize") or 100)
        answer: Dict[str, Any] = {"files": [self.view(f, me) for f in rows[start:start + size]]}
        if start + size < len(rows):
            answer["nextPageToken"] = str(start + size)
        return answer

    def change_page(self, q: Dict[str, str], me: str) -> Dict[str, Any]:
        token = q.get("pageToken", "")
        if not token.isdigit() or int(token) > len(self.changes):
            raise Refused(400, "Invalid Value", "INVALID_ARGUMENT", "invalid")
        if q.get("driveId") and q["driveId"] not in self.drives:
            raise Refused(404, "Shared drive not found.", "NOT_FOUND", "notFound")
        start, size = int(token), int(q.get("pageSize") or 100)
        every = q.get("includeItemsFromAllDrives") == "true"
        page = self.changes[start:start + size]
        out = []
        for n, change in enumerate(page, start):
            if q.get("driveId") and change["driveId"] != q["driveId"]:
                continue
            if change["driveId"] and not every:
                continue
            item = self.files.get(change["fileId"])
            row = {"changeType": "file", "fileId": change["fileId"], "time": change["time"],
                   "removed": change["removed"] or item is None}
            if item is not None and not row["removed"]:
                row["file"] = self.view(item, me)
            out.append(row)
        answer: Dict[str, Any] = {"changes": out}
        if start + size < len(self.changes):
            answer["nextPageToken"] = str(start + size)
        else:
            answer["newStartPageToken"] = str(len(self.changes))
        return answer

    def permission_route(self, method, item, tail, q, body):
        granted = self.perms.setdefault(item["id"], [])
        if tail == "/permissions" and method == "GET":
            shown = list(granted)
            if item.get("driveId"):
                shown = [{"id": "member-1", "type": "user", "role": "organizer", "emailAddress": self.ACCOUNT,
                          "permissionDetails": [{"inherited": True, "role": "organizer"}]}] + shown
            return {"permissions": shown}
        if tail == "/permissions" and method == "POST":
            data = json.loads(body or b"{}")
            if data.get("type") == "domain" and not data.get("domain"):
                raise Refused(400, "The domain field is required.", "INVALID_ARGUMENT", "required")
            permission = {"id": "perm-" + self.new_id(""), **{k: v for k, v in data.items() if k != "allowFileDiscovery"},
                          "_notified": q.get("sendNotificationEmail")}
            granted.append(permission)
            return {k: v for k, v in permission.items() if not k.startswith("_")}
        if method == "DELETE":
            permission_id = tail.rsplit("/", 1)[1]
            permission = next((p for p in granted if p["id"] == permission_id), None)
            if permission is None:
                raise Refused(404, "Permission not found.", "NOT_FOUND")
            if permission["role"] == "owner":
                raise Refused(403, "The owner of a file cannot be removed.", "", "cannotRemoveOwner")
            granted.remove(permission)
            return None
        raise Refused(404, "no route")

    def comment_route(self, method, item, tail, q, body, me):
        if not q.get("fields"):
            raise Refused(400, "The 'fields' parameter is required for this method.", "INVALID_ARGUMENT", "required")
        comments = self.comments.setdefault(item["id"], [])
        parts = tail.split("/")
        if tail == "/comments" and method == "GET":
            rows = [c for c in comments if q.get("includeDeleted") == "true" or not c["deleted"]]
            if q.get("startModifiedTime"):
                rows = [c for c in rows if c["modifiedTime"] >= q["startModifiedTime"]]
            start, size = int(q.get("pageToken") or 0), min(100, int(q.get("pageSize") or 20))
            answer: Dict[str, Any] = {"comments": copy.deepcopy(rows[start:start + size])}
            for comment in answer["comments"]:
                for x in [comment, *comment["replies"]]:
                    x["author"]["me"] = x["author"].get("emailAddress") == me
            if start + size < len(rows):
                answer["nextPageToken"] = str(start + size)
            return answer
        if tail == "/comments" and method == "POST":
            data = json.loads(body or b"{}")
            return self.comment_as(item["id"], me, data["content"],
                                   quote=(data.get("quotedFileContent") or {}).get("value", ""))
        comment = next((c for c in comments if c["id"] == parts[2]), None)
        if comment is None:
            raise Refused(404, "Comment not found.", "NOT_FOUND")
        if len(parts) == 3 and method == "GET":
            return comment
        if len(parts) == 4 and method == "POST":
            data = json.loads(body or b"{}")
            if not data.get("content") and not data.get("action"):
                raise Refused(400, "A reply needs content or an action.", "INVALID_ARGUMENT")
            return self.reply_as(item["id"], comment["id"], me, data.get("content", ""), data.get("action", ""))
        raise Refused(404, "no route")

    def comment_as(self, file_id: str, who: str, text: str, quote: str = "") -> Dict[str, Any]:
        created = self.tick()
        comment = {"id": self.new_id("AAAAc"), "content": text, "author": {"displayName": who, "emailAddress": who},
                   "createdTime": created, "modifiedTime": created, "resolved": False, "deleted": False,
                   "replies": []}
        if quote:
            comment["quotedFileContent"] = {"mimeType": "text/plain", "value": quote}
        self.comments.setdefault(file_id, []).append(comment)
        return comment

    def reply_as(self, file_id: str, comment_id: str, who: str, text: str, action: str = "") -> Dict[str, Any]:
        comment = next(c for c in self.comments[file_id] if c["id"] == comment_id)
        created = self.tick()
        reply = {"id": self.new_id("AAAAr"), "author": {"displayName": who, "emailAddress": who},
                 "createdTime": created, "modifiedTime": created, "deleted": False,
                 **({"content": text} if text else {}), **({"action": action} if action else {})}
        if action:
            comment["resolved"] = action == "resolve"
        comment["replies"].append(reply)
        comment["modifiedTime"] = created
        return reply

    def upload(self, method, path, q, body, me, headers):
        kind = q.get("uploadType")
        match = re.match(r"^/files(?:/([^/]+))?$", path)
        if not match:
            raise Refused(404, f"no route {path}")
        target = self._file(match.group(1), q) if match.group(1) else None
        if kind == "multipart":
            if len(body) > MULTIPART_MOST + 64 * 1024:
                raise Refused(413, "Request Entity Too Large: a multipart upload is for files of 5 MB or less; use a "
                                   "resumable upload.", "", "uploadTooLarge")
            boundary = headers["Content-Type"].split("boundary=", 1)[1].strip('"')
            parts = body.split(b"--" + boundary.encode())

            def payload(part: bytes) -> bytes:
                _, _, rest = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
                return rest[:-2] if rest.endswith(b"\r\n") else rest

            metadata = json.loads(payload(parts[1]).decode("utf-8"))
            media_head = parts[2].lstrip(b"\r\n").partition(b"\r\n\r\n")[0].decode()
            return self.store(target, metadata, payload(parts[2]), media_head.split(":", 1)[1].strip(), q, me,
                              "multipart")
        if kind == "resumable" and method in ("POST", "PATCH"):
            if not headers.get("X-Upload-Content-Length"):
                raise Refused(400, "X-Upload-Content-Length is required.", "INVALID_ARGUMENT")
            session = self.new_id("up")
            self.sessions[session] = {"target": target, "metadata": json.loads(body or b"{}"), "q": dict(q),
                                      "type": headers.get("X-Upload-Content-Type") or "application/octet-stream",
                                      "length": int(headers["X-Upload-Content-Length"])}
            where = f"http://{headers['Host']}/upload/drive/v3/files?uploadType=resumable&upload_id={session}"
            return 200, b"", "text/plain", {"Location": where}
        if kind == "resumable" and method == "PUT":
            session = self.sessions.pop(q.get("upload_id", ""), None)
            if session is None:
                raise Refused(404, "No such upload session.", "NOT_FOUND")
            if len(body) != session["length"]:
                raise Refused(400, "The bytes sent are not the length announced.", "INVALID_ARGUMENT")
            return self.store(session["target"], session["metadata"], body, session["type"], session["q"], me,
                              "resumable")
        raise Refused(400, f"Unsupported uploadType {kind}", "INVALID_ARGUMENT")

    def store(self, target, metadata, content, mime, q, me, kind):
        self.uploads.append(kind)
        if target is not None:
            target["_content"], target["size"] = content, str(len(content))
            self.touch(target, me)
            return self.view(target, me)
        parent = (metadata.get("parents") or [ROOT])[0]
        self._file(parent, q)
        mime = metadata.get("mimeType") or mime
        item = self.stamp(metadata["name"], mime, parent, by=me, **({} if mime.startswith(
            "application/vnd.google-apps.") else {"size": str(len(content))}))
        if not mime.startswith("application/vnd.google-apps."):
            item["_content"] = content
        return self.view(item, me)

    # -- docs.googleapis.com -----------------------------------------------------------------------------------------
    def docs_api(self, method, path, q, query, body, me, headers):
        match = re.match(r"^/v1/documents/([^/:]+)(:batchUpdate)?$", path)
        if not match or match.group(1) not in self.docs:
            raise Refused(404, f"Requested entity was not found.", "NOT_FOUND")
        doc_id = match.group(1)
        doc = self.docs[doc_id]
        if method == "GET" and not match.group(2):
            return self.document_json(doc_id, q.get("includeTabsContent") == "true")
        if method == "POST" and match.group(2):
            data = json.loads(body or b"{}")
            if self.before_batch is not None:
                self.before_batch(doc_id)
            wanted = (data.get("writeControl") or {}).get("requiredRevisionId")
            if wanted and wanted != f"ALm37BV-rev{doc['revision']}":
                raise Refused(400, f"The required revision ID ({wanted}) does not match the latest revision.",
                              "FAILED_PRECONDITION")
            self.apply(doc, data.get("requests") or [])
            doc["revision"] += 1
            self.touch(self.files[doc_id], me)
            return {"documentId": doc_id, "replies": [{} for _ in data.get("requests") or []],
                    "writeControl": {"requiredRevisionId": f"ALm37BV-rev{doc['revision']}"}}
        raise Refused(404, "no route")

    # -- sheets.googleapis.com ---------------------------------------------------------------------------------------
    def sheets_api(self, method, path, q, query, body, me, headers):
        match = re.match(r"^/v4/spreadsheets/([^/:]+)(?::batchUpdate|/values:batchGet|/values:batchUpdate|"
                         r"/values/(.+?)(:append)?)?$", path)
        if not match or match.group(1) not in self.sheets:
            raise Refused(404, "Requested entity was not found.", "NOT_FOUND")
        sheet_id, book = match.group(1), self.sheets[match.group(1)]
        data = json.loads(body or b"{}") if body else {}
        if path.endswith(":batchUpdate") and "/values" not in path:
            replies = []
            for request in data.get("requests") or []:
                if "addSheet" in request:
                    title = request["addSheet"]["properties"]["title"]
                    if any(t["title"] == title for t in book["tabs"]):
                        raise Refused(400, f"A sheet with the name \"{title}\" already exists.", "INVALID_ARGUMENT")
                    tab = self.add_tab(sheet_id, title, [])
                    replies.append({"addSheet": {"properties": {"sheetId": tab["sheetId"], "title": title,
                                                                "index": len(book["tabs"]) - 1}}})
                elif "updateSheetProperties" in request:
                    props = request["updateSheetProperties"]["properties"]
                    next(t for t in book["tabs"] if t["sheetId"] == props["sheetId"])["title"] = props["title"]
                    replies.append({})
                else:
                    raise Refused(400, f"Unsupported request {list(request)}", "INVALID_ARGUMENT")
            return {"spreadsheetId": sheet_id, "replies": replies}
        if path.endswith("/values:batchGet"):
            ranges = query.get("ranges") or []
            return {"spreadsheetId": sheet_id, "valueRanges": [
                self.read_range(book, r, q.get("valueRenderOption", "FORMATTED_VALUE")) for r in ranges]}
        if path.endswith("/values:batchUpdate"):
            option = data.get("valueInputOption")
            if option not in ("RAW", "USER_ENTERED"):
                raise Refused(400, "'valueInputOption' is required but not specified", "INVALID_ARGUMENT")
            for item in data.get("data") or []:
                tab, r1, c1, _, _ = self._range(book, item["range"])
                self.write_cells(tab, r1, c1, item["values"], option)
            return {"spreadsheetId": sheet_id}
        if match.group(2):
            text = match.group(2)
            if method == "GET":
                return self.read_range(book, text, q.get("valueRenderOption", "FORMATTED_VALUE"))
            option = q.get("valueInputOption")
            if option not in ("RAW", "USER_ENTERED"):
                raise Refused(400, "'valueInputOption' is required but not specified", "INVALID_ARGUMENT")
            values = data.get("values") or []
            if match.group(3):
                tab = self._range(book, text)[0]
                last = max([r for r, _ in tab["cells"]] or [0])
                if q.get("insertDataOption") == "INSERT_ROWS":
                    tab["rows"] = max(tab["rows"], last) + len(values)
                self.write_cells(tab, last + 1, 1, values, option)
                width = max(len(v) for v in values)
                done = f"'{tab['title']}'!A{last + 1}:{chr(64 + width)}{last + len(values)}"
                self.touch(self.files[sheet_id], me)
                return {"spreadsheetId": sheet_id, "updates": {"updatedRange": done, "updatedRows": len(values),
                                                               "updatedCells": sum(len(v) for v in values)}}
            tab, r1, c1, _, _ = self._range(book, text)
            self.write_cells(tab, r1, c1, values, option)
            self.touch(self.files[sheet_id], me)
            return {"spreadsheetId": sheet_id, "updatedRange": text, "updatedCells": sum(len(v) for v in values)}
        if method == "GET":
            return {"spreadsheetId": sheet_id, "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
                    "properties": {"title": self.files[sheet_id]["name"], "locale": book["locale"],
                                   "timeZone": "Asia/Dubai"},
                    "sheets": [{"properties": {"sheetId": t["sheetId"], "title": t["title"], "index": n,
                                               "gridProperties": {"rowCount": t["rows"], "columnCount": t["columns"]}}}
                               for n, t in enumerate(book["tabs"])]}
        raise Refused(404, "no route")

    # -- forms.googleapis.com ----------------------------------------------------------------------------------------
    def forms_api(self, method, path, q, query, body, me, headers):
        match = re.match(r"^/v1/forms/([^/]+)(/responses)?$", path)
        if not match or match.group(1) not in self.forms:
            raise Refused(404, "Requested entity was not found.", "NOT_FOUND")
        form = self.forms[match.group(1)]
        if not match.group(2):
            return form["form"]
        size = int(q.get("pageSize") or 5000)
        if size > 5000:
            raise Refused(400, "page_size must be at most 5000", "INVALID_ARGUMENT")
        given = q.get("filter", "")
        rows = list(form["responses"])
        if given:
            found = re.fullmatch(r"timestamp (>=|>) (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", given)
            if not found:
                raise Refused(400, f"Invalid filter: {given}", "INVALID_ARGUMENT")
            when = found.group(2).replace("Z", "")
            rows = [r for r in rows if (r["lastSubmittedTime"][:19] >= when if found.group(1) == ">=" else
                                        r["lastSubmittedTime"][:19] > when)]
        # No promised order: by a hash of the id, as good as any.
        rows.sort(key=lambda r: hashlib.sha256(r["responseId"].encode()).hexdigest())
        start = 0
        if q.get("pageToken"):
            try:
                held = json.loads(base64.urlsafe_b64decode(q["pageToken"]))
            except ValueError:
                raise Refused(400, "Invalid page token.", "INVALID_ARGUMENT")
            if held["filter"] != given:
                raise Refused(400, "The filter must match the one the page token was given with.",
                              "INVALID_ARGUMENT")
            start = held["offset"]
        answer: Dict[str, Any] = {"responses": rows[start:start + size]} if rows[start:start + size] else {}
        if start + size < len(rows):
            answer["nextPageToken"] = base64.urlsafe_b64encode(json.dumps(
                {"offset": start + size, "filter": given}).encode()).decode()
        return answer

    # -- the stub's address ------------------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("www.googleapis.com", self._server)

    def secret(self, access_token: str = "at-1") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": self.accounts[access_token]["email"], "access_token": access_token,
                "api_base_url": self.url}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
