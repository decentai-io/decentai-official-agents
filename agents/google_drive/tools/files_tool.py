"""Finding, fetching and filing in Google Drive: My Drive, shared drives, and what was shared with the account.

- **Shared drives everywhere.** A search reads every drive (``corpora=allDrives``); a folder's listing reads the
  drive it is in; every request naming a file says ``supportsAllDrives``.
- **A download is held to what a chat file can be** (8 MiB less 64 KiB): a file Drive says is bigger is refused before
  a byte is read, with its link. A Google file is exported to the format asked (PDF, Word, Excel, PowerPoint, text);
  a spreadsheet tab as CSV is written here, UTF-8 with a BOM so Excel reads Arabic.
- **An upload** of up to 5 MB is one multipart request; a bigger one is a resumable session, as Google asks.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
import os
import uuid

from standard import ResourceDenied, ToolBase

from .common import (ALL_DRIVES, DOCUMENT, DRAWING, FIELDS, FILE_MOST, FOLDER, ONE, PRESENTATION, SPREADSHEET,
                     WANTED, HttpError, connect, failed, find_folder, folder_names, get_item, human_size, item_row,
                     listing_params, literal, no_account, refused, segment)
from .sheets_model import a1, column_letter, find_tab, no_tab, tab_row

MULTIPART_MOST = 5 * 1024 * 1024
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
FORMATS = {"pdf": (PDF, ".pdf"), "docx": (DOCX, ".docx"), "xlsx": (XLSX, ".xlsx"), "pptx": (PPTX, ".pptx"),
           "txt": ("text/plain", ".txt"), "csv": ("text/csv", ".csv")}
#: What each Google file may become, its usual format first.
EXPORTS = {DOCUMENT: ["docx", "pdf", "txt"], SPREADSHEET: ["xlsx", "pdf", "csv"],
           PRESENTATION: ["pptx", "pdf", "txt"], DRAWING: ["pdf"]}
#: What an uploaded office file becomes when the person asks for it as a Google file.
CONVERTS = {DOCX: DOCUMENT, "application/msword": DOCUMENT, "text/plain": DOCUMENT, "text/html": DOCUMENT,
            XLSX: SPREADSHEET, "application/vnd.ms-excel": SPREADSHEET, "text/csv": SPREADSHEET,
            PPTX: PRESENTATION, "application/vnd.ms-powerpoint": PRESENTATION}
KNOWN = {".docx": DOCX, ".xlsx": XLSX, ".pptx": PPTX, ".csv": "text/csv", ".md": "text/markdown",
         ".json": "application/json"}


def file_name(name: str) -> str:
    """A Drive name as a file name: a slash would cut it."""
    return (name.replace("/", "-").replace("\\", "-").strip() or "file")


def with_extension(name: str, extension: str) -> str:
    return name if name.lower().endswith(extension) else name + extension


def mime_of(name: str) -> str:
    return KNOWN.get(os.path.splitext(name.lower())[1]) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def page(answer: dict, rows: list) -> dict:
    result = {"items": rows}
    if answer.get("nextPageToken"):
        result["next_page_token"] = str(answer["nextPageToken"])
    if answer.get("incompleteSearch"):
        result["incomplete"] = True
    return result


class FilesTool(ToolBase):
    id = "files"

    # -- reads -----------------------------------------------------------------------------------------------------
    async def search(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        clauses = ["trashed = false"]
        text = str(inputs.get("query") or "").strip()
        if text:
            clauses.append(f"(name contains {literal(text)} or fullText contains {literal(text)})")
        if inputs.get("kind"):
            clauses.append(f"mimeType = {literal(WANTED[inputs['kind']])}")
        if inputs.get("modified_after"):
            clauses.append(f"modifiedTime > {literal(inputs['modified_after'])}")
        params = {**ALL_DRIVES, "corpora": "allDrives", "q": " and ".join(clauses),
                  "pageSize": int(inputs.get("max_results") or 20), "fields": f"nextPageToken,incompleteSearch,"
                                                                            f"files({FIELDS})"}
        # Drive refuses orderBy beside a fullText term; without one, newest first.
        if not text:
            params["orderBy"] = "modifiedTime desc"
        if inputs.get("page_token"):
            params["pageToken"] = str(inputs["page_token"])
        try:
            answer = await clients.drive.get("files", params=params)
            items = answer.get("files") or []
            names = await folder_names(clients.drive, items)
        except HttpError as error:
            return failed(error)
        rows = [item_row(i, names.get(str((i.get("parents") or [""])[0]))) for i in items]
        return page(answer, rows), "success"

    async def list(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        try:
            folder = await find_folder(clients.drive, inputs.get("folder_id"), inputs.get("folder_path"))
            params = {**listing_params(folder), "q": f"{literal(folder['id'])} in parents and trashed = false",
                      "orderBy": "folder,name", "pageSize": int(inputs.get("max_results") or 50),
                      "fields": f"nextPageToken,files({FIELDS})"}
            if inputs.get("page_token"):
                params["pageToken"] = str(inputs["page_token"])
            answer = await clients.drive.get("files", params=params)
        except HttpError as error:
            return failed(error)
        name = str(folder.get("name") or "")
        result = page(answer, [item_row(i, name) for i in answer.get("files") or []])
        result["folder"] = {"item_id": str(folder.get("id") or ""), "name": name,
                            **({"drive_id": str(folder["driveId"])} if folder.get("driveId") else {})}
        return result, "success"

    async def shared_with_me(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        params = {**ALL_DRIVES, "corpora": "user", "q": "sharedWithMe = true and trashed = false",
                  "orderBy": "sharedWithMeTime desc", "pageSize": int(call.inputs.get("max_results") or 25),
                  "fields": f"nextPageToken,files({FIELDS})"}
        if call.inputs.get("page_token"):
            params["pageToken"] = str(call.inputs["page_token"])
        try:
            answer = await clients.drive.get("files", params=params)
        except HttpError as error:
            return failed(error)
        return page(answer, [item_row(i) for i in answer.get("files") or []]), "success"

    async def drives(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        params = {"pageSize": int(call.inputs.get("max_results") or 50), "fields": "nextPageToken,drives(id,name)"}
        if call.inputs.get("page_token"):
            params["pageToken"] = str(call.inputs["page_token"])
        try:
            answer = await clients.drive.get("drives", params=params)
        except HttpError as error:
            return failed(error)
        result = {"drives": [{"drive_id": str(d.get("id") or ""), "name": str(d.get("name") or "")}
                             for d in answer.get("drives") or []]}
        if answer.get("nextPageToken"):
            result["next_page_token"] = str(answer["nextPageToken"])
        return result, "success"

    async def get(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        try:
            item = await get_item(clients.drive, str(call.inputs["item_id"]))
            names = await folder_names(clients.drive, [item])
        except HttpError as error:
            return failed(error)
        return {"item": item_row(item, names.get(str((item.get("parents") or [""])[0])))}, "success"

    async def versions(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        item_id = str(call.inputs["item_id"])
        try:
            item = await get_item(clients.drive, item_id, "id,name,mimeType")
            found = await clients.drive.token_pages(
                f"files/{segment(item_id)}/revisions", items="revisions", most=1000, params={
                    "pageSize": 200, "fields": "nextPageToken,revisions(id,modifiedTime,size,keepForever,"
                                               "originalFilename,lastModifyingUser(emailAddress,displayName))"})
        except HttpError as error:
            return failed(error)
        rows = []
        for revision in sorted(found, key=lambda r: str(r.get("modifiedTime") or ""), reverse=True):
            row = {"version_id": str(revision.get("id") or ""), "modified": str(revision.get("modifiedTime") or "")}
            if revision.get("lastModifyingUser"):
                row["modified_by"] = str(revision["lastModifyingUser"].get("emailAddress")
                                         or revision["lastModifyingUser"].get("displayName") or "")
            if revision.get("size") not in (None, ""):
                row["size"] = int(revision["size"])
            if revision.get("keepForever"):
                row["kept_forever"] = True
            rows.append(row)
        most = int(call.inputs.get("max_results") or 20)
        return {"name": str(item.get("name") or ""), "total": len(rows), "versions": rows[:most]}, "success"

    async def download(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        wanted = str(call.inputs.get("format") or "")
        try:
            item = await get_item(clients.drive, str(call.inputs["item_id"]))
        except HttpError as error:
            return failed(error)
        name, mime = str(item.get("name") or ""), str(item.get("mimeType") or "")
        link = str(item.get("webViewLink") or "")
        if mime == FOLDER:
            return refused(f"'{name}' is a folder; download the files in it one by one.")
        native = mime.startswith("application/vnd.google-apps.")
        if native:
            allowed = EXPORTS.get(mime)
            if not allowed:
                return refused(f"'{name}' is a Google {mime.rsplit('.', 1)[-1]}, which Drive cannot export. Open it: "
                               f"{link}", "invalid")
            wanted = wanted or allowed[0]
            if wanted not in allowed:
                return refused(f"'{name}' can be brought in as {', '.join(allowed)}, not {wanted}.")
            if wanted == "csv":
                return await self._csv(call, clients, item)
            export_type, extension = FORMATS[wanted]
            filename = with_extension(file_name(name), extension)
            await call.progress(f"Exporting {filename}")
            try:
                raw = await clients.drive.download(f"files/{segment(item['id'])}/export",
                                                   params={"mimeType": export_type}, what=f"'{filename}'")
            except HttpError as error:
                if "exportsizelimitexceeded" in error.message.lower().replace(" ", "") or (
                        error.status == 403 and "too large" in error.message.lower()):
                    return refused(f"Google will not export '{name}': an export may be at most 10 MB. Open it in "
                                   f"Google instead: {link}", "too_large", link=link)
                return self._too_large(error, link) or failed(error)
        else:
            if wanted and FORMATS[wanted][0] != mime:
                return refused(f"Drive converts only Google Docs, Sheets and Slides; '{name}' is {mime} and comes "
                               f"in as it is (leave format out).")
            size = int(item.get("size") or 0)
            if size > FILE_MOST:
                return refused(f"'{name}' is {human_size(size)}; at most {human_size(FILE_MOST)} can be brought into "
                               f"the chat. It was not downloaded. Open it in Drive instead: {link}", "too_large",
                               size=size, limit=FILE_MOST, link=link)
            filename = file_name(name)
            await call.progress(f"Downloading {filename}")
            try:
                raw = await clients.drive.download(f"files/{segment(item['id'])}",
                                                   params={**ONE, "alt": "media"}, what=f"'{filename}'")
            except HttpError as error:
                return self._too_large(error, link) or failed(error)
        return {"file_ref": call.give(filename, raw), "filename": filename, "size": len(raw)}, "success"

    @staticmethod
    def _too_large(error: HttpError, link: str):
        if error.kind == "too_large":
            return refused(f"{error.message} It was not saved. Open it in Google instead: {link}", "too_large",
                           limit=FILE_MOST, link=link)
        return None

    async def _csv(self, call, clients, item):
        """One tab as CSV: Drive's own export gives the first tab only, without a BOM."""
        try:
            meta = await clients.sheets.get(f"spreadsheets/{segment(item['id'])}", params={
                "fields": "sheets.properties(sheetId,title,index,gridProperties(rowCount,columnCount))"})
            wanted = str(call.inputs.get("tab") or "")
            sheets = meta.get("sheets") or []
            tab = find_tab(meta, wanted) if wanted else (tab_row(sheets[0]) if sheets else None)
            if tab is None:
                return no_tab(meta, wanted)
            rows, start, last = [], 1, column_letter(tab["grid_columns"])
            while start <= tab["grid_rows"]:
                end = min(tab["grid_rows"], start + 4999)
                cells = a1(tab["tab"], f"A{start}:{last}{end}")
                answer = await clients.sheets.get(
                    f"spreadsheets/{segment(item['id'])}/values/{segment(cells)}",
                    params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"})
                got = answer.get("values") or []
                rows += got
                if len(got) < end - start + 1:
                    break
                start = end + 1
        except HttpError as error:
            return failed(error)

        def written() -> bytes:
            out = io.StringIO()
            csv.writer(out, lineterminator="\r\n").writerows(rows)
            return out.getvalue().encode("utf-8-sig")

        raw = await asyncio.to_thread(written)
        filename = with_extension(file_name(f"{item.get('name')} - {tab['tab']}"), ".csv")
        if len(raw) > FILE_MOST:
            return refused(f"'{filename}' would be {human_size(len(raw))}; at most {human_size(FILE_MOST)} can be "
                           f"brought into the chat. Read it with sheets_read instead.", "too_large",
                           size=len(raw), limit=FILE_MOST)
        return {"file_ref": call.give(filename, raw), "filename": filename, "size": len(raw),
                "tab": tab["tab"], "rows": len(rows)}, "success"

    # -- writes ----------------------------------------------------------------------------------------------------
    async def upload(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        try:
            path = await call.file(str(inputs["file_ref"]))
        except ResourceDenied:
            return refused(f"{inputs['file_ref']} is no file of this chat's.", "not_found")
        name = str(inputs.get("name") or path.name).strip()
        size = path.stat().st_size
        if size > FILE_MOST:
            return refused(f"The file is {human_size(size)}, more than the {human_size(FILE_MOST)} a chat file may "
                           f"be.", "too_large", size=size, limit=FILE_MOST)
        raw = await asyncio.to_thread(path.read_bytes)
        mime = mime_of(name)
        metadata = {"name": name}
        if inputs.get("convert"):
            target = CONVERTS.get(mime)
            if not target:
                return refused(f"Drive converts Word, Excel, PowerPoint, CSV and text files to Google files; "
                               f"{name} ({mime}) is uploaded as it is (leave convert out).")
            metadata["mimeType"] = target
            metadata["name"] = os.path.splitext(name)[0] or name
        on_conflict = str(inputs.get("on_conflict") or "rename")
        try:
            folder = await find_folder(clients.drive, inputs.get("folder_id"), inputs.get("folder_path"))
            same = await self._named(clients.drive, folder, metadata["name"])
            if same and on_conflict == "fail":
                return refused(f"'{folder.get('name')}' already has a file named '{metadata['name']}'.", "conflict")
            await call.progress(f"Uploading {name} ({human_size(len(raw))})")
            if same and on_conflict == "replace":
                item = await self._send(clients.upload, f"files/{segment(same[0]['id'])}", "PATCH", {}, raw, mime)
                replaced = True
            else:
                if same:
                    metadata["name"] = await self._free(clients.drive, folder, metadata["name"])
                metadata["parents"] = [folder["id"]]
                item = await self._send(clients.upload, "files", "POST", metadata, raw, mime)
                replaced = False
        except HttpError as error:
            return failed(error)
        return {"item": item_row(item, str(folder.get("name") or "")), "replaced": replaced}, "success"

    @staticmethod
    async def _named(drive, folder, name):
        answer = await drive.get("files", params={
            **listing_params(folder), "pageSize": 10, "fields": "files(id,name,mimeType)",
            "q": f"{literal(folder['id'])} in parents and name = {literal(name)} and trashed = false "
                 f"and mimeType != '{FOLDER}'"})
        return answer.get("files") or []

    async def _free(self, drive, folder, name):
        """ "notice.pdf" as "notice 1.pdf", "notice 2.pdf", … — the first the folder hasn't got."""
        stem, extension = os.path.splitext(name)
        for number in range(1, 100):
            candidate = f"{stem} {number}{extension}"
            if not await self._named(drive, folder, candidate):
                return candidate
        return f"{stem} {uuid.uuid4().hex[:6]}{extension}"

    @staticmethod
    async def _send(upload, path, method, metadata, raw, mime):
        """Up to 5 MB in one multipart request; more in a resumable session: its address asked first, then the
        bytes sent there in one PUT."""
        params = {**ONE, "fields": FIELDS}
        if len(raw) <= MULTIPART_MOST:
            boundary = "decentai-" + uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                    + json.dumps(metadata, ensure_ascii=False).encode("utf-8")
                    + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode() + raw
                    + f"\r\n--{boundary}--\r\n".encode())
            return await upload.request(method, path, params={**params, "uploadType": "multipart"}, data=body,
                                        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                                        timeout=180)
        started = await upload.request(method, path, params={**params, "uploadType": "resumable"},
                                       data=json.dumps(metadata, ensure_ascii=False).encode("utf-8"), raw=True,
                                       headers={"Content-Type": "application/json; charset=UTF-8",
                                                "X-Upload-Content-Type": mime,
                                                "X-Upload-Content-Length": str(len(raw))})
        session = str(started.headers.get("Location") or "")
        if not session:
            raise HttpError("http", "Google Drive opened no upload session (no Location was given).")
        item = await upload.request("PUT", "", absolute=session, data=raw, headers={"Content-Type": mime},
                                    timeout=300)
        if not item.get("id"):
            raise HttpError("unknown", "Google Drive took the bytes but did not say the upload finished; look in the "
                                       "folder before uploading again.")
        return item

    async def create_folder(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        name = str(call.inputs["name"]).strip()
        try:
            parent = await find_folder(clients.drive, call.inputs.get("folder_id"), call.inputs.get("folder_path"))
            answer = await clients.drive.get("files", params={
                **listing_params(parent), "pageSize": 1, "fields": "files(id)",
                "q": f"{literal(parent['id'])} in parents and name = {literal(name)} and mimeType = '{FOLDER}' "
                     f"and trashed = false"})
            if answer.get("files"):
                return refused(f"'{parent.get('name')}' already has a folder named '{name}'.", "conflict",
                               item_id=str(answer["files"][0]["id"]))
            item = await clients.drive.post("files", params={**ONE, "fields": FIELDS},
                                            json={"name": name, "mimeType": FOLDER, "parents": [parent["id"]]})
        except HttpError as error:
            return failed(error)
        return {"item": item_row(item, str(parent.get("name") or ""))}, "success"

    async def move(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        item_id = str(inputs["item_id"])
        body, params = {}, {**ONE, "fields": FIELDS}
        if str(inputs.get("new_name") or "").strip():
            body["name"] = str(inputs["new_name"]).strip()
        try:
            target = None
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                target = await find_folder(clients.drive, inputs.get("to_folder_id"), inputs.get("to_folder_path"))
                current = await get_item(clients.drive, item_id, "id,parents")
                params["addParents"] = target["id"]
                if current.get("parents"):
                    params["removeParents"] = ",".join(current["parents"])
            if not body and target is None:
                return refused("Nothing to change: give new_name, or a folder to move to.")
            item = await clients.drive.patch(f"files/{segment(item_id)}", params=params, json=body)
        except HttpError as error:
            return failed(error)
        return {"item": item_row(item, str((target or {}).get("name") or "") or None)}, "success"

    async def copy(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        body = {}
        try:
            source = await get_item(clients.drive, str(inputs["item_id"]), "id,name,mimeType,parents")
            if source.get("mimeType") == FOLDER:
                return refused("Drive copies files, not folders.")
            folder = None
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                folder = await find_folder(clients.drive, inputs.get("to_folder_id"), inputs.get("to_folder_path"))
                body["parents"] = [folder["id"]]
            body["name"] = str(inputs.get("new_name") or "").strip() or f"Copy of {source.get('name')}"
            item = await clients.drive.post(f"files/{segment(source['id'])}/copy",
                                            params={**ONE, "fields": FIELDS}, json=body)
        except HttpError as error:
            return failed(error)
        return {"item": item_row(item, str((folder or {}).get("name") or "") or None)}, "success"

    async def trash(self, call):
        return await self._trashed(call, True)

    async def restore(self, call):
        return await self._trashed(call, False)

    async def _trashed(self, call, trashed: bool):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        try:
            item = await clients.drive.patch(f"files/{segment(str(call.inputs['item_id']))}",
                                             params={**ONE, "fields": FIELDS}, json={"trashed": trashed})
        except HttpError as error:
            return failed(error)
        result = {"item": item_row(item), "trashed": bool(item.get("trashed"))}
        if trashed:
            result["note"] = ("In the Drive trash; files_restore brings it back. Drive empties the trash after 30 "
                              "days.")
        return result, "success"
