"""Finding, fetching and filing in OneDrive.

Reads go straight to Graph. A download becomes a file of the chat (never larger than a chat file may be; a bigger
one is refused before a byte is read, with its link). An upload takes a file handed to the call. Every change is a
change the person can undo in their own drive (level 2): a delete goes to the recycle bin.
"""

from __future__ import annotations

import asyncio
import os
import time

from standard import ResourceDenied, ToolBase
from standard.http import FILE_MOST, HttpError, human_size

from .common import client, failed, ids, refused
from .drive import clean_path, drive_for, file_host, item_row, person, too_large

CONVERTIBLE = (".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp", ".csv", ".txt")
#: How long a copy is waited for before its progress is handed back to be asked again.
COPY_WAIT = 15.0


def page(answer, rows):
    result = {"items": rows}
    if answer.get("@odata.nextLink"):
        result["next_page_token"] = str(answer["@odata.nextLink"])
    return result


async def folder_of(drive, folder_id, folder_path, drive_id=""):
    """The folder the person named, as ((id, drive_id), None), or (None, the refusal)."""
    if folder_id:
        return (str(folder_id), str(drive_id or "")), None
    path = clean_path(folder_path or "/")
    try:
        folder = await drive.by_path(path)
    except HttpError as error:
        if error.kind == "not_found":
            return None, refused(f"OneDrive has no folder at '{path}'.", "not_found")
        raise
    if "folder" not in folder:
        return None, refused(f"'{path}' is a file, not a folder.")
    return (str(folder.get("id") or ""), ""), None


class FilesTool(ToolBase):
    id = "files"

    # -- reads -------------------------------------------------------------------------------------------------
    async def search(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        try:
            answer = await drive.search(str(call.inputs["query"]), int(call.inputs.get("max_results") or 20),
                                        str(call.inputs.get("page_token") or ""))
        except HttpError as error:
            return failed(error)
        return page(answer, [item_row(i) for i in answer.get("value") or []]), "success"

    async def list(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        token = str(call.inputs.get("page_token") or "")
        given_drive = str(call.inputs.get("drive_id") or "")
        try:
            if token:
                answer = await drive.children("", "", 0, token)
            else:
                found, problem = await folder_of(drive, call.inputs.get("folder_id"), call.inputs.get("folder_path"),
                                                 given_drive)
                if problem:
                    return problem
                answer = await drive.children(found[0], found[1], int(call.inputs.get("max_results") or 50))
        except HttpError as error:
            return failed(error)
        rows = [item_row(i) for i in answer.get("value") or []]
        for row in rows:
            if given_drive:
                row.setdefault("drive_id", given_drive)
        return page(answer, rows), "success"

    async def shared_with_me(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        try:
            entries, how = await drive.shared_with_me(int(call.inputs.get("max_results") or 25),
                                                      str(call.inputs.get("query") or ""))
        except HttpError as error:
            return failed(error)
        return {"items": [item_row(e) for e in entries], "found_by": how}, "success"

    async def get(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        try:
            item = await drive.item(item_id, drive_id)
        except HttpError as error:
            return failed(error)
        row = item_row(item)
        if drive_id:
            row["drive_id"] = drive_id
        if "@microsoft.graph.downloadUrl" in item:
            row["downloadable"] = True
        return {"item": row}, "success"

    async def versions(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        try:
            found = await drive.versions(item_id, drive_id)
        except HttpError as error:
            return failed(error)
        most = int(call.inputs.get("max_results") or 20)
        return {"versions": [{"version_id": str(v.get("id") or ""), "modified": str(v.get("lastModifiedDateTime") or ""),
                              "modified_by": person(v.get("lastModifiedBy")), "size": int(v.get("size") or 0)}
                             for v in found[:most]], "total": len(found)}, "success"

    async def download(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        try:
            item = await drive.item(item_id, drive_id)
        except HttpError as error:
            return failed(error)
        row = item_row(item)
        if "folder" in item or "file" not in item:
            return refused(f"'{row['name']}' is a folder; download the files in it one by one.")
        stem, ext = os.path.splitext(row["name"])
        convert = bool(call.inputs.get("as_pdf")) and ext.lower() != ".pdf"
        if convert and ext.lower() not in CONVERTIBLE:
            return refused(f"Only Office and text files convert to PDF; download '{row['name']}' as it is.")
        if row["size"] > FILE_MOST and not convert:
            return too_large(row["name"], row["size"], row["link"])
        filename = stem + ".pdf" if convert else row["name"]
        await call.progress(f"Downloading {row['name']}" + (" as PDF" if convert else ""))
        try:
            data = await drive.fetch(item, drive_id, as_pdf=convert, what=f"'{filename}'")
        except HttpError as error:
            if error.kind == "too_large":
                return {**error.answer()[0], "link": row["link"]}, "error"
            return failed(error)
        if not data and row["size"] and not convert:
            return refused("OneDrive returned no content for that file.", "http")
        return {"file_ref": call.give(filename, data), "filename": filename, "size": len(data)}, "success"

    # -- writes ------------------------------------------------------------------------------------------------
    async def upload(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        file_ref, text = str(call.inputs.get("file_ref") or ""), call.inputs.get("text")
        if bool(file_ref) == (text is not None):
            return refused("Give either file_ref (a file handed to this call) or text (words to save as a new file), "
                           "not both and not neither.")
        if text is not None:
            # Words the person gave, saved as a file of their own: UTF-8, with the name they chose.
            name = str(call.inputs.get("name") or "").strip()
            if not name:
                return refused("Name the new file, with its extension (notes.txt, summary.md).")
            data = str(text).encode("utf-8")
        else:
            try:
                path = await call.file(file_ref)
            except ResourceDenied:
                return refused(f"No file '{file_ref}' was handed to this call; attach it to the chat first.",
                               "not_found")
            name = str(call.inputs.get("name") or path.name).strip()
            size = path.stat().st_size
            if size > FILE_MOST:
                return refused(f"The file is {human_size(size)}, more than the {human_size(FILE_MOST)} a chat file "
                               f"may be.", "too_large")
            data = await asyncio.to_thread(path.read_bytes)
        if not name or "/" in name or "\\" in name:
            return refused("Give the file a name without slashes.")
        try:
            found, problem = await folder_of(drive, call.inputs.get("folder_id"), call.inputs.get("folder_path"),
                                             call.inputs.get("drive_id"))
            if problem:
                return problem
            await call.progress(f"Uploading {name} ({human_size(len(data))})")
            item = await drive.upload(found[0], found[1], name, data, str(call.inputs.get("on_conflict") or "rename"))
        except HttpError as error:
            if error.status == 409:
                return refused(f"The folder already has a file named '{name}'; nothing was uploaded. Upload it with "
                               f"on_conflict rename or replace.", "conflict")
            return failed(error)
        return {"item": item_row(item), "how": "one request" if len(data) < 4 * 1024 * 1024 else "upload session"}, \
            "success"

    async def create_folder(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        name = str(call.inputs["name"]).strip()
        try:
            found, problem = await folder_of(drive, call.inputs.get("folder_id"), call.inputs.get("folder_path"),
                                             call.inputs.get("drive_id"))
            if problem:
                return problem
            item = await drive.create_folder(found[0], found[1], name)
        except HttpError as error:
            if error.status == 409:
                return refused(f"There is already something named '{name}' there.", "conflict")
            return failed(error)
        return {"item": item_row(item)}, "success"

    async def move(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        inputs = call.inputs
        item_id, drive_id = ids(call)
        patch = {}
        if str(inputs.get("new_name") or "").strip():
            patch["name"] = str(inputs["new_name"]).strip()
        try:
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                found, problem = await folder_of(drive, inputs.get("to_folder_id"), inputs.get("to_folder_path"),
                                                 inputs.get("to_drive_id") or drive_id)
                if problem:
                    return problem
                if found[1] != drive_id:
                    return refused("OneDrive moves an item only within its own drive; copy it (files_copy) to put it "
                                   "in another drive.")
                patch["parentReference"] = {"id": found[0]}
            if not patch:
                return refused("Nothing to change: give new_name, or a folder to move to.")
            item = await drive.update(item_id, drive_id, patch)
        except HttpError as error:
            if error.status == 409:
                return refused("Something with that name is already there; nothing was moved.", "conflict")
            return failed(error)
        return {"item": item_row(item)}, "success"

    async def move_many(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        inputs = call.inputs
        drive_id = str(inputs.get("drive_id") or "")
        if not (inputs.get("to_folder_id") or inputs.get("to_folder_path")):
            return refused("Give the folder to move to.")
        try:
            found, problem = await folder_of(drive, inputs.get("to_folder_id"), inputs.get("to_folder_path"),
                                             inputs.get("to_drive_id") or drive_id)
        except HttpError as error:
            return failed(error)
        if problem:
            return problem
        moved, missed, renewed = [], [], False
        for item_id in dict.fromkeys(str(i) for i in inputs["item_ids"]):
            for attempt in (1, 2):
                try:
                    moved.append(item_row(await drive.update(item_id, drive_id, {"parentReference": {"id": found[0]}})))
                    break
                except HttpError as error:
                    if error.kind == "auth" and attempt == 1 and not renewed:
                        # A long batch can outlive its access token: the platform hands a fresh one on a new read.
                        renewed = True
                        fresh, _ = await drive_for(call)
                        if fresh is not None:
                            drive = fresh
                            continue
                    missed.append({"item_id": item_id, "error": error.message, "kind": error.kind})
                    break
        folder = moved[0]["folder"] if moved else clean_path(inputs.get("to_folder_path") or "")
        return {"folder": folder, "moved": moved, "failed": missed}, "success" if moved or not missed else "error"

    async def copy(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        inputs = call.inputs
        item_id, drive_id = ids(call)
        try:
            found, problem = await folder_of(drive, inputs.get("to_folder_id"), inputs.get("to_folder_path"),
                                             inputs.get("to_drive_id"))
            if problem:
                return problem
            target_drive = found[1] or str((await drive.drive()).get("id") or "")
            body = {"parentReference": {"driveId": target_drive, "id": found[0]}}
            if str(inputs.get("new_name") or "").strip():
                body["name"] = str(inputs["new_name"]).strip()
            address = await drive.copy(item_id, drive_id, body, str(inputs.get("on_conflict") or "rename"))
        except HttpError as error:
            return failed(error)
        if not address:
            return refused("Microsoft accepted the copy but gave no way to follow it; look in the folder in a "
                           "minute.", "unknown")
        return await self.followed(drive, address, target_drive, COPY_WAIT)

    async def copy_status(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        address = str(call.inputs["monitor"])
        if not file_host(address):
            return refused("That is not an address Microsoft gave for a copy's progress; use the monitor files_copy "
                           "returned.")
        return await self.followed(drive, address, str(call.inputs.get("to_drive_id") or ""), 0.0)

    @staticmethod
    async def followed(drive, address, target_drive, wait):
        """A copy's progress, waited for up to ``wait`` seconds; its new item once it is done."""
        started = time.monotonic()
        while True:
            try:
                status = await drive.monitor(address)
            except HttpError as error:
                return failed(error)
            state = str(status.get("status") or "")
            if state == "completed":
                result = {"status": "completed", "monitor": address}
                if status.get("resourceId"):
                    try:
                        result["item"] = item_row(await drive.item(str(status["resourceId"]), target_drive))
                    except HttpError:
                        pass
                return result, "success"
            if state == "failed":
                error = status.get("error") or {}
                return refused(f"Microsoft could not copy it: {error.get('message') or error.get('code') or state}.",
                               "conflict" if "exists" in str(error.get("code") or "").lower() else "http")
            if time.monotonic() - started >= wait:
                return {"status": "in_progress", "monitor": address,
                        "percent": float(status.get("percentageComplete") or 0)}, "success"
            await asyncio.sleep(1.0)

    async def delete(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        try:
            item = await drive.item(item_id, drive_id)
            if "root" in item:
                return refused("The top of the drive cannot be deleted.")
            await drive.remove(item_id, drive_id)
            personal = await drive.personal()
        except HttpError as error:
            return failed(error, f"'{item_id}'")
        note = ("In the OneDrive recycle bin; files_restore puts it back." if personal and not drive_id else
                "In the OneDrive recycle bin; restore it from the recycle bin in OneDrive on the web (Microsoft offers "
                "no restore to apps on a work or school account).")
        return {"deleted": True, "name": str(item.get("name") or ""), "item_id": item_id,
                **({"drive_id": drive_id} if drive_id else {}), "note": note}, "success"

    async def restore(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        try:
            if not await drive.personal():
                return refused("Microsoft restores from the recycle bin only on a personal OneDrive. On a work or "
                               "school account, restore it from the recycle bin in OneDrive on the web.",
                               "unsupported")
            item = await drive.restore(str(call.inputs["item_id"]))
        except HttpError as error:
            if error.kind == "not_found":
                return refused("That item is not in the recycle bin (restored already, or emptied).", "not_found")
            return failed(error)
        return {"item": item_row(item)}, "success"
