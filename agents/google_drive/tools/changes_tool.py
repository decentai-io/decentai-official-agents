"""A changes watch on the drive: what was added, changed, trashed or removed since the last check, from Drive's own
change log (``changes``), from a start page token taken when the watch began.

Across My Drive and every shared drive (``includeItemsFromAllDrives``), or one shared drive; optionally narrowed to the
files directly in one folder. Changes the account made itself are left out unless asked for, so an assistant that
files things is not told about its own filing. A file changed several times between checks is handed on once, as it
is now. A shared drive that is gone closes its watch.
"""

from __future__ import annotations

from standard import ToolBase

from .common import (ALL_DRIVES, FIELDS, HttpError, connect, failed, find_folder, item_row, kind_of, no_account,
                     person, segment)
from .watching import Seen, run

PAGE = 100


class ChangesTool(ToolBase):
    id = "changes"

    async def watch(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        drive_id = str(inputs.get("drive_id") or "")
        try:
            label = "My Drive and shared drives"
            if drive_id:
                drive = await clients.drive.get(f"drives/{segment(drive_id)}", params={"fields": "id,name"})
                label = str(drive.get("name") or drive_id)
            folder = None
            if inputs.get("folder_id") or inputs.get("folder_path"):
                folder = await find_folder(clients.drive, inputs.get("folder_id"), inputs.get("folder_path"))
                label = f"{label} / {folder.get('name')}"
            params = {"supportsAllDrives": "true", **({"driveId": drive_id} if drive_id else {})}
            token = str((await clients.drive.get("changes/startPageToken", params=params)).get("startPageToken") or "")
        except HttpError as error:
            return failed(error)
        if not token:
            return {"error": "Google Drive gave no start page token.", "kind": "http"}, "error"
        watch_ref = await call.records.create("watch", {
            "kind": "changes", "status": "active", "account": clients.address, "target_id": drive_id, "label": label,
            "folder_id": str((folder or {}).get("id") or ""), "cursor": token,
            "include_mine": "yes" if inputs.get("include_mine") is True else "no",
            "note": str(inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "label": label}, "success"

    async def new(self, call):
        return await run(call, "changes", self._read), "success"

    @staticmethod
    async def _read(clients, watch, most):
        fields = watch["fields"]
        drive_id, folder_id = str(fields.get("target_id") or ""), str(fields.get("folder_id") or "")
        mine = fields.get("include_mine") == "yes"
        token, latest, more = str(fields.get("cursor") or ""), {}, False
        params = {**ALL_DRIVES, "includeRemoved": "true", "pageSize": PAGE, **({"driveId": drive_id} if drive_id else {}),
                  "fields": f"nextPageToken,newStartPageToken,changes(changeType,removed,fileId,time,driveId,"
                            f"file({FIELDS}))"}
        while True:
            answer = await clients.drive.get("changes", params={**params, "pageToken": token})
            for change in answer.get("changes") or []:
                if change.get("changeType") not in (None, "file"):
                    continue
                file = change.get("file") or {}
                if folder_id and not change.get("removed") and folder_id not in (file.get("parents") or []):
                    continue
                if folder_id and change.get("removed"):
                    continue
                if not mine and (file.get("lastModifyingUser") or {}).get("me") and not change.get("removed"):
                    continue
                latest[str(change.get("fileId") or "")] = change
            if answer.get("newStartPageToken"):
                token = str(answer["newStartPageToken"])
                break
            token = str(answer.get("nextPageToken") or "")
            if not token:
                break
            if len(latest) >= most:
                more = True
                break
        rows = []
        for file_id, change in latest.items():
            file = change.get("file") or {}
            what = "removed" if change.get("removed") else ("trashed" if file.get("trashed") else "changed")
            row = {"watch_ref": watch["ref"], "label": str(fields.get("label") or ""), "change": what,
                   "item_id": file_id, "time": str(change.get("time") or "")}
            if file:
                shown = item_row(file)
                row.update({k: shown[k] for k in ("name", "kind", "mime_type", "link", "drive_id", "folder_id")
                            if k in shown})
                if file.get("lastModifyingUser"):
                    row["modified_by"] = person(file["lastModifyingUser"])
                row["kind"] = kind_of(str(file.get("mimeType") or ""))
            rows.append(row)
        return Seen(sorted(rows, key=lambda r: r["time"]), {"cursor": token} if token else None, more=more)
