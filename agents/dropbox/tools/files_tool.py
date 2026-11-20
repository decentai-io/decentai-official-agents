"""Finding, fetching and filing in Dropbox.

Dropbox has real paths, so a row says the folder it sits in as a path; results carry Dropbox's own ids ("id:…"), and
every function takes either an id or a path. In a team space paths start at the team space; a path from the person's
own folder is found there too.
"""

import os

from standard import ResourceDenied, ToolBase
from standard.http import FILE_MOST, HttpError, human_size

from .account_tool import ready
from .dropbox_api import clean_path, item_rows, parent_of, summary, tag

#: What Dropbox's preview renders as PDF. Spreadsheets preview as HTML, so they are not offered as PDF at all.
PDF_PREVIEWS = (".doc", ".docx", ".docm", ".odt", ".rtf", ".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".ppsm",
                ".odp", ".ai", ".eps")
#: Shared items read for "shared with me", newest first.
SHARED_MOST = 1000


def too_large(name: str, size: int, link: str = ""):
    way_out = (f" Open or download it from Dropbox instead: {link} (this link works for four hours)." if link else
               " Open it in Dropbox instead.")
    return {"error": f"'{name}' is {human_size(size)}, and at most {human_size(FILE_MOST)} can be brought into the "
                     f"chat. It was not downloaded.{way_out}",
            "kind": "too_large", "size": size, "limit": FILE_MOST, **({"link": link} if link else {})}, "error"


def join(folder: str, name: str) -> str:
    return clean_path(f"{folder}/{name}")


def conflict(name: str):
    """A Dropbox refusal that means the name is taken, said plainly."""
    return {"error": f"That folder already has something named '{name}'.", "kind": "conflict"}, "error"


def is_folder(item) -> bool:
    return tag(item) == "folder"


def bad_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return f"'{name}' is not a name Dropbox can give a file or folder."
    return ""


class FilesTool(ToolBase):
    id = "files"

    @staticmethod
    async def _folder(client, folder_id, folder_path):
        """The folder named, as (path, None), or ("", result) when there is no folder there to use. Writes need a
        path, so a folder given by id is looked up once."""
        if not folder_id and clean_path(folder_path or "/") == "/":
            return "/", None
        wanted = str(folder_id or clean_path(folder_path))
        if wanted.startswith("ns:") and "/" not in wanted:
            # A shared folder's own top, by its namespace: Dropbox has no metadata for a root, but lists it.
            return wanted, None
        try:
            folder = await client.metadata(wanted)
        except HttpError as failed:
            if failed.kind == "not_found":
                return "", ({"error": f"Dropbox has no folder at '{wanted}'.", "kind": "not_found"}, "error")
            raise
        if not is_folder(folder):
            return "", ({"error": f"'{wanted}' is a file, not a folder.", "kind": "invalid"}, "error")
        return str(folder.get("path_display") or wanted), None

    @staticmethod
    async def _item(client, item_id):
        """An item in the account's Dropbox, or a file someone shared that was never added to it: Dropbox answers
        for those only through its sharing calls. The second value says which it was."""
        try:
            return await client.metadata(item_id), False
        except HttpError as failed:
            if failed.kind != "not_found" or not str(item_id).startswith("id:"):
                raise
        try:
            return await client.received_file(str(item_id)), True
        except HttpError as failed:
            if failed.kind in ("invalid", "denied"):
                raise HttpError("not_found", f"Dropbox has no file or folder {item_id} that this account can "
                                             f"open.") from None
            raise

    @staticmethod
    def _received_row(entry):
        return {"item_id": str(entry.get("id") or ""), "name": str(entry.get("name") or ""), "kind": "file",
                "size": 0, "mime_type": "", "modified": "", "modified_by": "", "folder": "",
                "path": str(entry.get("path_display") or ""), "link": str(entry.get("preview_url") or ""),
                "shared": True, "shared_by": ", ".join(entry.get("owner_display_names") or []),
                "shared_at": str(entry.get("time_invited") or ""), "mounted": bool(entry.get("path_lower"))}

    @staticmethod
    def _folder_row(entry):
        """A shared folder: in the account's Dropbox (mounted, with a path) or not yet added to it."""
        path = str(entry.get("path_display") or entry.get("path_lower") or "")
        # One not yet added has no path; Dropbox addresses it by its namespace ("ns:<shared folder id>").
        sfid = str(entry.get("shared_folder_id") or "")
        return {"item_id": path or (f"ns:{sfid}" if sfid else ""), "name": str(entry.get("name") or ""),
                "kind": "folder", "size": 0,
                "mime_type": "", "modified": "", "modified_by": "", "folder": parent_of(path), "path": path,
                "link": str(entry.get("preview_url") or ""), "shared": True,
                "shared_by": ", ".join(entry.get("owner_display_names") or []),
                "shared_at": str(entry.get("time_invited") or ""), "mounted": bool(path),
                "shared_folder_id": str(entry.get("shared_folder_id") or ""),
                "team_folder": bool(entry.get("is_team_folder") or entry.get("is_inside_team_folder"))}

    # -- reads ---------------------------------------------------------------------------------------------------
    async def search(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        try:
            answer = await client.search(str(call.inputs["query"]), int(call.inputs.get("max_results") or 20),
                                         str(call.inputs.get("page_token") or ""))
            found = [((m.get("metadata") or {}).get("metadata") or {}) for m in answer.get("matches") or []]
            rows = await item_rows(client, [i for i in found if i])
        except HttpError as failed:
            return failed.answer()
        result = {"items": rows}
        if answer.get("has_more") and answer.get("cursor"):
            result["next_page_token"] = str(answer["cursor"])
        return result, "success"

    async def list(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        page_token = str(call.inputs.get("page_token") or "")
        try:
            if page_token:
                answer = await client.list_folder_continue(page_token)
            else:
                folder, problem = await self._folder(client, call.inputs.get("folder_id"),
                                                     call.inputs.get("folder_path"))
                if problem:
                    return problem
                answer = await client.list_folder(folder, int(call.inputs.get("max_results") or 25))
            rows = await item_rows(client, [i for i in answer.get("entries") or [] if tag(i) in ("file", "folder")])
        except HttpError as failed:
            return failed.answer()
        # Dropbox lists in no promised order; folders first, then by name.
        rows.sort(key=lambda r: (r["kind"] != "folder", r["name"].lower()))
        result = {"items": rows}
        if answer.get("has_more") and answer.get("cursor"):
            result["next_page_token"] = str(answer["cursor"])
        return result, "success"

    async def shared_with_me(self, call):
        """Files and folders other people shared with the account, newest share first: folders already in its
        Dropbox (mounted) with their path, and those not yet added with their link."""
        client, refused = await ready(call)
        if client is None:
            return refused
        limit = int(call.inputs.get("max_results") or 25)
        try:
            files = await client.received_files(SHARED_MOST)
            folders = await client.shared_folders(SHARED_MOST)
        except HttpError as failed:
            return failed.answer()
        rows = [self._received_row(e) for e in files]
        rows += [self._folder_row(e) for e in folders if tag(e.get("access_type")) != "owner"]
        rows.sort(key=lambda r: r["shared_at"], reverse=True)
        return {"items": rows[:limit], "more": len(rows) > limit}, "success"

    async def get(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        try:
            item, received = await self._item(client, str(call.inputs["item_id"]))
            row = self._received_row(item) if received else (await item_rows(client, [item]))[0]
        except HttpError as failed:
            return failed.answer()
        return {"item": row}, "success"

    async def versions(self, call):
        """A file's earlier versions, newest first, each with the rev that files_restore takes; a deleted file's
        too, by its path."""
        client, refused = await ready(call)
        if client is None:
            return refused
        wanted = str(call.inputs["item_id"]).strip()
        most = int(call.inputs.get("max_results") or 10)
        try:
            try:
                item = await client.metadata(wanted)
                if is_folder(item):
                    return {"error": f"'{item.get('name')}' is a folder; Dropbox keeps versions of files only.",
                            "kind": "invalid"}, "error"
                path = str(item.get("path_lower") or item.get("path_display") or "")
            except HttpError as failed:
                # A deleted file has no metadata, but its path still has its versions.
                if failed.kind != "not_found" or wanted.startswith(("id:", "rev:", "ns:")):
                    raise
                path = clean_path(wanted)
            answer = await client.revisions(path, most)
        except HttpError as failed:
            return failed.answer()
        entries = answer.get("entries") or []
        rows = [{"rev": str(e.get("rev") or ""), "modified": str(e.get("server_modified") or ""),
                 "size": int(e.get("size") or 0), "name": str(e.get("name") or ""),
                 "path": str(e.get("path_display") or path)} for e in entries]
        result = {"path": rows[0]["path"] if rows else path, "deleted": bool(answer.get("is_deleted")),
                  "versions": rows}
        if answer.get("server_deleted"):
            result["deleted_at"] = str(answer["server_deleted"])
        return result, "success"

    async def download(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        item_id = str(call.inputs["item_id"])
        as_pdf = bool(call.inputs.get("as_pdf"))
        try:
            item, received = await self._item(client, item_id)
            if received:
                return await self._download_received(call, client, item, as_pdf)
        except HttpError as failed:
            return failed.answer()
        name = str(item.get("name") or "")
        if is_folder(item):
            return {"error": f"'{name}' is a folder; download the files in it one by one.",
                    "kind": "invalid"}, "error"
        stem, ext = os.path.splitext(name)
        exported = item.get("is_downloadable") is False
        convert = as_pdf and ext.lower() != ".pdf" and not exported
        if as_pdf and exported:
            return {"error": f"'{name}' is a Dropbox document that is exported in Dropbox's own format; download it "
                             f"without as_pdf.", "kind": "invalid"}, "error"
        if convert and ext.lower() not in PDF_PREVIEWS:
            return {"error": f"Dropbox renders only Word and PowerPoint files (and similar documents) as PDF; "
                             f"download '{name}' as it is.", "kind": "invalid"}, "error"
        size = int(item.get("size") or 0)
        where = str(item.get("id") or item.get("path_lower") or item_id)
        if not exported and size > FILE_MOST:
            return too_large(name, size, await client.temporary_link(where))
        filename = stem + ".pdf" if convert else name
        await call.progress(f"Downloading {filename}")
        try:
            if exported:
                result, raw = await client.export(where, f"'{name}'")
                filename = str((result.get("export_metadata") or {}).get("name") or name)
            elif convert:
                raw = await client.preview(where, f"'{filename}'")
            else:
                _, raw = await client.download(where, f"'{name}'")
        except HttpError as failed:
            if failed.kind == "too_large":
                return too_large(filename, size, await client.temporary_link(where))
            return failed.answer()
        return self._save(call, filename, raw)

    async def _download_received(self, call, client, item, as_pdf):
        """A shared file outside the account's Dropbox is fetched through its link; the link's metadata is what
        carries its size."""
        name = str(item.get("name") or "")
        url = str(item.get("preview_url") or "")
        if as_pdf and not name.lower().endswith(".pdf"):
            return {"error": f"'{name}' was shared with the account, and Dropbox converts only files in it; download "
                             f"it as it is.", "kind": "invalid"}, "error"
        if not url:
            return {"error": f"Dropbox gave no way to fetch '{name}'.", "kind": "not_found"}, "error"
        try:
            size = int((await client.link_metadata(url)).get("size") or 0)
            if size > FILE_MOST:
                return too_large(name, size, url)
            await call.progress(f"Downloading {name}")
            raw = await client.link_file(url, f"'{name}'")
        except HttpError as failed:
            if failed.kind == "too_large":
                return too_large(name, 0, url)
            return failed.answer()
        return self._save(call, name, raw)

    @staticmethod
    def _save(call, filename, raw):
        return {"file_ref": call.give(filename, raw), "filename": filename, "size": len(raw)}, "success"

    # -- writes --------------------------------------------------------------------------------------------------
    async def upload(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        inputs = call.inputs
        file_ref = str(inputs["file_ref"])
        try:
            path = await call.file(file_ref)
        except ResourceDenied:
            return {"error": f"Unknown file_ref '{file_ref}'.", "kind": "not_found"}, "error"
        size = path.stat().st_size
        name = str(inputs.get("name") or path.name).strip()
        wrong = bad_name(name)
        if wrong:
            return {"error": wrong, "kind": "invalid"}, "error"
        if size > FILE_MOST:
            return {"error": f"The file is {human_size(size)}, over the {human_size(FILE_MOST)} a chat file can be.",
                    "kind": "too_large", "size": size, "limit": FILE_MOST}, "error"
        raw = path.read_bytes()
        on_conflict = str(inputs.get("on_conflict") or "rename")
        try:
            folder, problem = await self._folder(client, inputs.get("folder_id"), inputs.get("folder_path"))
            if problem:
                return problem
            await call.progress(f"Uploading {name} ({human_size(len(raw))})")
            item = await client.upload(join(folder, name), raw, on_conflict)
            row = (await item_rows(client, [{".tag": "file", **item}]))[0]
        except HttpError as failed:
            if "conflict" in summary(failed):
                return conflict(name)
            return failed.answer()
        return {"item": row}, "success"

    async def create_folder(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        name = str(call.inputs["name"]).strip()
        wrong = bad_name(name)
        if wrong:
            return {"error": wrong, "kind": "invalid"}, "error"
        try:
            parent, problem = await self._folder(client, call.inputs.get("folder_id"), call.inputs.get("folder_path"))
            if problem:
                return problem
            item = await client.create_folder(join(parent, name))
        except HttpError as failed:
            if "conflict" in summary(failed):
                return conflict(name)
            return failed.answer()
        return {"item": (await item_rows(client, [{".tag": "folder", **item}]))[0]}, "success"

    async def move(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        inputs = call.inputs
        new_name = str(inputs.get("new_name") or "").strip()
        if new_name and bad_name(new_name):
            return {"error": bad_name(new_name), "kind": "invalid"}, "error"
        try:
            item = await client.metadata(str(inputs["item_id"]))
            current = str(item.get("path_display") or "")
            target = parent_of(current) or "/"
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                target, problem = await self._folder(client, inputs.get("to_folder_id"), inputs.get("to_folder_path"))
                if problem:
                    return problem
            destination = join(target, new_name or str(item.get("name") or ""))
            if destination == current:
                return {"error": "Nothing to change: give new_name, or a folder to move to.",
                        "kind": "invalid"}, "error"
            moved = await client.move(current, destination)
            row = (await item_rows(client, [moved]))[0]
        except HttpError as failed:
            if "conflict" in summary(failed):
                return conflict(new_name or destination.rsplit("/", 1)[-1])
            return failed.answer()
        return {"item": row}, "success"

    async def restore(self, call):
        """A file put back as it was at one of its versions (a rev from files_versions); a deleted file comes back
        at its newest version when no rev is given."""
        client, refused = await ready(call)
        if client is None:
            return refused
        wanted = str(call.inputs["item_id"]).strip()
        rev = str(call.inputs.get("rev") or "").strip()
        try:
            try:
                item = await client.metadata(wanted)
                if is_folder(item):
                    return {"error": "Dropbox restores files, not folders; restore the files in it one by one, or "
                                     "restore the folder from Dropbox's deleted files on the web.",
                            "kind": "invalid"}, "error"
                path = str(item.get("path_lower") or "")
            except HttpError as failed:
                if failed.kind != "not_found" or wanted.startswith(("id:", "rev:", "ns:")):
                    raise
                path = clean_path(wanted)
            history = await client.revisions(path, 10)
            revs = [str(e.get("rev") or "") for e in history.get("entries") or []]
            if not rev:
                if not history.get("is_deleted") or not revs:
                    return {"error": "Name the version to restore (a rev from files_versions); the file is not "
                                     "deleted, so there is no newest one to bring back.", "kind": "invalid"}, "error"
                rev = revs[0]
            restored = await client.restore(path, rev)
            row = (await item_rows(client, [{".tag": "file", **restored}]))[0]
        except HttpError as failed:
            return failed.answer()
        return {"item": row, "rev": rev}, "success"

    async def delete(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        try:
            item = await client.metadata(str(call.inputs["item_id"]))
            gone = await client.delete(str(item.get("id") or item.get("path_lower")))
        except HttpError as failed:
            return failed.answer()
        return {"deleted": True, "name": str(gone.get("name") or item.get("name") or ""),
                "path": str(item.get("path_display") or ""),
                "note": "In Dropbox's deleted files; a file can be put back with files_restore, and anything from "
                        "Dropbox on the web, for as long as the account's plan keeps deleted files. In a shared "
                        "folder it is gone for everyone in it until then."}, "success"
