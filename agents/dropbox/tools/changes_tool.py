"""Watches: what changed in a folder since the last look.

A watch remembers how far a folder (and everything under it) has been read: Dropbox's own list_folder cursor, taken
when the watch began, with the account it was made on and the namespace it was rooted at (a team space's). new() asks
Dropbox what changed since each cursor and hands the changes on through ``standard.watch.check``:

- each watch is read on its own; one that fails keeps its cursor and is listed in ``problems``, the others are handed
  on now;
- a watch whose folder was deleted closes itself and is listed in ``closed``;
- cursors are written only once every watch was read.

Dropbox may declare a cursor stale ("reset"); the watch then starts again from now and says so in ``reset``, because
whatever changed in between was not seen.
"""

from standard import ToolBase
from standard.http import HttpError
from standard.watch import Gone, Seen, check, close, listing

from .account_tool import client_for, ready
from .dropbox_api import clean_path, item_rows, parent_of, summary, tag

#: Changes per page, fixed into the cursor. Dropbox treats it as approximate, so a page may hold a few more.
PAGE = 25
#: Seconds a check spends reading watches; those not reached wait for the next run. Well inside its 60 s timeout.
BUDGET = 35.0
#: Watch statuses: "watching" is the name earlier versions gave an active watch.
ACTIVE = ("active", "watching")


def is_active(record):
    return str(record["fields"].get("status") or "active") in ACTIVE


class ChangesTool(ToolBase):
    id = "changes"

    async def watch(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        folder_id = str(call.inputs.get("folder_id") or "")
        folder = clean_path(call.inputs.get("folder_path") or "/")
        try:
            if folder_id or folder != "/":
                found = await client.metadata(folder_id or folder)
                if tag(found) != "folder":
                    return {"error": f"'{folder_id or folder}' is a file, not a folder; watch the folder it is in.",
                            "kind": "invalid"}, "error"
                folder = str(found.get("path_display") or folder)
                folder_id = str(found.get("id") or folder_id)
            cursor = await client.latest_cursor(folder_id or folder, PAGE)
        except HttpError as failed:
            if failed.kind == "not_found":
                return {"error": f"Dropbox has no folder at '{folder_id or folder}'.", "kind": "not_found"}, "error"
            return failed.answer()
        watch_ref = await call.records.create("watch", {
            "folder": folder, "folder_id": folder_id, "cursor": cursor, "status": "active",
            "account": client.email, "root": client.root or "", "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "folder": folder}, "success"

    async def new(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            watches = [await call.records.read("watch", wanted)]
        else:
            watches = await call.records.list("watch")
        watches = [w for w in watches if is_active(w)]
        if not watches:
            return {"checked": 0, "changes": [], "reset": [], "more": False, "problems": [], "closed": []}, "success"
        clients, reset = {}, []

        async def client_of(watch):
            fields = watch["fields"]
            key = (str(fields.get("account") or ""), str(fields.get("root") or ""))
            if key not in clients:
                # A watch keeps the namespace it was made in, so its cursor stays good ("" for none).
                client, why = await client_for(call, wanted=key[0] or None, ask=False, root=key[1])
                if client is None:
                    raise HttpError("auth", why)
                clients[key] = client
            return clients[key]

        async def read(watch):
            fields = watch["fields"]
            client = await client_of(watch)
            folder = str(fields.get("folder") or "/")
            try:
                page = await client.list_folder_continue(str(fields.get("cursor") or ""))
            except HttpError as failed:
                if "reset" not in summary(failed):
                    raise
                cursor = await client.latest_cursor(str(fields.get("folder_id") or folder), PAGE)
                reset.append({"watch_ref": watch["ref"], "folder": folder,
                              "note": "Dropbox reset this watch; it starts again from now, and changes since the "
                                      "last check were not seen. List the folder to catch up."})
                return Seen([], {"cursor": cursor})
            entries = page.get("entries") or []
            watched = folder.lower()
            if watched != "/" and any(tag(e) == "deleted" and str(e.get("path_lower") or "") == watched
                                      for e in entries):
                gone = f"The folder '{folder}' was deleted or moved, so the watch has stopped."
            else:
                gone = ""
            files = [e for e in entries if tag(e) == "file"]
            shown = {r["item_id"]: r for r in await item_rows(client, files)}
            rows = []
            for entry in entries:
                row = self._row(entry, shown)
                if row is not None:
                    rows.append({**row, "watch_ref": watch["ref"]})
            return Seen(rows, {"cursor": str(page.get("cursor") or fields.get("cursor") or "")},
                        more=bool(page.get("has_more")), close=gone)

        async def guarded(watch):
            try:
                return await read(watch)
            except HttpError as failed:
                if failed.kind == "not_found" and "path" in summary(failed):
                    raise Gone(f"The folder '{watch['fields'].get('folder')}' is no longer there, so the watch has "
                               f"stopped.") from None
                raise

        found = await check(call, "watch", watches, guarded, budget=BUDGET,
                            name=lambda w: str(w["fields"].get("folder") or w["ref"]))
        return {"checked": found["checked"], "changes": found["rows"], "reset": reset, "more": found["more"],
                "problems": found["problems"], "closed": found["closed"]}, "success"

    @staticmethod
    def _row(entry, shown):
        """A file added or changed, or anything deleted. Dropbox does not say whether a file is new or edited (both
        arrive as the file as it now is), and a new folder alone is not news."""
        kind = tag(entry)
        path = str(entry.get("path_display") or entry.get("path_lower") or "")
        if kind == "deleted":
            return {"change": "deleted", "item_id": "", "name": str(entry.get("name") or ""), "path": path,
                    "folder": parent_of(path), "size": 0, "modified": "", "modified_by": ""}
        if kind != "file":
            return None
        row = shown.get(str(entry.get("id") or ""))
        if row is None:
            return None
        return {"change": "added_or_changed", "item_id": row["item_id"], "name": row["name"], "path": path,
                "folder": row["folder"], "size": row["size"], "modified": row["modified"],
                "modified_by": row["modified_by"]}


class WatchesTool(ToolBase):
    id = "watches"

    async def list(self, call):
        found = await listing(call, "watch", lambda r: {
            "folder": str(r["fields"].get("folder") or "/"), "account": str(r["fields"].get("account") or "")},
            include_closed=True)
        for row in found:
            if row["status"] == "watching":
                row["status"] = "active"
        if not call.inputs.get("include_closed"):
            found = [row for row in found if row["status"] == "active"]
        return {"watches": found}, "success"

    async def close(self, call):
        ref = str(call.inputs["watch_ref"])
        try:
            done = await close(call, "watch", ref)
        except Exception as failed:  # the runtime's refusal: no such watch
            return {"error": f"There is no watch {ref}: {failed}", "kind": "not_found"}, "error"
        return done, "success"
