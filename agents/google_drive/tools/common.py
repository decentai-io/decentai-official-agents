"""What every tool of the Google Drive agent shares: the clients of the five Google APIs over one connected account,
the account picked by address, Drive's file rows and query language, and the shape of a refusal.

- **Five bases, one token.** Drive (``www.googleapis.com/drive/v3``), Drive uploads (``/upload/drive/v3``), Docs,
  Sheets and Forms answer to the same access token. Each is a ``standard.google.Google``, so 429 and Google's 403 rate
  reasons are waited out and tried again, and a 403 for a missing permission says to reconnect.
- **Under test** a secret's ``api_base_url`` names the stub's scheme and port; each API keeps its own host and path, so
  the stub tells the services apart as Google does, and the way out holds the agent to the hosts it declared.
- **Shared drives everywhere:** every request that names a file says ``supportsAllDrives``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

from standard.accounts import account, listed
from standard.google import ALL_DRIVES, Google
from standard.http import FILE_MOST, HttpError, human_size, segment
from standard.net import BaseAddress

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
USERINFO = "https://www.googleapis.com/oauth2/v3"
DOCS = "https://docs.googleapis.com/v1"
SHEETS = "https://sheets.googleapis.com/v4"
FORMS = "https://forms.googleapis.com/v1"

#: On a request that names one file: shared-drive files are found too.
ONE = {"supportsAllDrives": "true"}

FOLDER = "application/vnd.google-apps.folder"
DOCUMENT = "application/vnd.google-apps.document"
SPREADSHEET = "application/vnd.google-apps.spreadsheet"
PRESENTATION = "application/vnd.google-apps.presentation"
FORM = "application/vnd.google-apps.form"
DRAWING = "application/vnd.google-apps.drawing"
SHORTCUT = "application/vnd.google-apps.shortcut"
KINDS = {FOLDER: "folder", DOCUMENT: "document", SPREADSHEET: "spreadsheet", PRESENTATION: "presentation",
         FORM: "form", DRAWING: "drawing", SHORTCUT: "shortcut"}
#: What a person names when they look for one kind of file.
WANTED = {"folder": FOLDER, "document": DOCUMENT, "spreadsheet": SPREADSHEET, "presentation": PRESENTATION,
          "form": FORM, "pdf": "application/pdf"}

PERSON = "emailAddress,displayName,me"
FIELDS = (f"id,name,mimeType,size,modifiedTime,createdTime,parents,driveId,webViewLink,shared,trashed,"
          f"owners({PERSON}),lastModifyingUser({PERSON}),sharingUser({PERSON}),shortcutDetails")


class Api(Google):
    """One Google API, at its own base; under test, at the stub's port under the same host and path."""

    def __init__(self, secret: dict[str, Any], base: str, service: str):
        super().__init__(secret, base)
        self.SERVICE = service
        given = BaseAddress.given(secret.get("api_base_url"))
        if given:
            where, real = urlsplit(given), urlsplit(base)
            port = f":{where.port}" if where.port else ""
            self.base = f"{where.scheme}://{real.hostname}{port}{real.path}"


class Clients:
    """The account's five APIs, each made when first used."""

    BASES = {"drive": (DRIVE, "Google Drive"), "upload": (UPLOAD, "Google Drive"), "info": (USERINFO, "Google"),
             "docs": (DOCS, "Google Docs"), "sheets": (SHEETS, "Google Sheets"), "forms": (FORMS, "Google Forms")}

    def __init__(self, secret: dict[str, Any]):
        self.secret = secret
        self.address = str(secret.get("account") or "")
        self._made: dict[str, Api] = {}

    def __getattr__(self, name: str) -> Api:
        if name not in self.BASES:
            raise AttributeError(name)
        if name not in self._made:
            base, service = self.BASES[name]
            self._made[name] = Api(self.secret, base, service)
        return self._made[name]

    async def domain(self) -> str:
        """The account's own organization (Google's ``hd``), or "" for a personal account. Never guessed from the
        address: a Workspace user's address may be on a secondary domain, and a personal one on any."""
        try:
            info = await self.info.get("userinfo")
        except HttpError as failed:
            if failed.kind in ("auth", "rate"):
                raise
            return ""
        return str(info.get("hd") or "").strip().lower()

    def close(self) -> None:
        for client in self._made.values():
            client.close()


async def connect(call: Any, ask: bool = True, wanted: Any = None) -> tuple[Clients | None, str]:
    """The clients over the account named by ``account`` (or ``wanted``), or None and why. A function that changes
    nothing asks nobody to connect one (``ask`` False) and points at account_connect instead."""
    secret, why = await account(call, "google", wanted, ask, "Google")
    if secret is None:
        if not ask and "No Google account is connected" in why:
            why += " Connect one with account_connect, or from the agent's Credentials."
        return None, why
    return Clients(secret), ""


async def addresses(call: Any) -> list[str]:
    return [a for a in await listed(call, "google") if a]


def no_account(why: str) -> tuple[dict[str, Any], str]:
    return {"error": why, "kind": "auth"}, "error"


def refused(message: str, kind: str = "invalid", **more: Any) -> tuple[dict[str, Any], str]:
    return {"error": message, "kind": kind, **more}, "error"


def failed(error: HttpError) -> tuple[dict[str, Any], str]:
    return error.answer()


def literal(text: Any) -> str:
    """A value inside a Drive query: quoted, with quote and backslash escaped."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def person(user: Any) -> str:
    user = user or {}
    return str(user.get("emailAddress") or user.get("displayName") or "")


def kind_of(mime: str) -> str:
    return KINDS.get(mime, "file")


def item_row(item: dict[str, Any], folder: str | None = None) -> dict[str, Any]:
    """One file or folder as the assistant reads it. A value Drive does not give is left out, never made up."""
    mime = str(item.get("mimeType") or "")
    row: dict[str, Any] = {"item_id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
                           "kind": kind_of(mime), "mime_type": mime}
    if item.get("size") not in (None, ""):
        row["size"] = int(item["size"])
    for key, field in (("modified", "modifiedTime"), ("link", "webViewLink"), ("drive_id", "driveId")):
        if item.get(field):
            row[key] = str(item[field])
    if item.get("lastModifyingUser"):
        row["modified_by"] = person(item["lastModifyingUser"])
    owners = [person(o) for o in item.get("owners") or [] if person(o)]
    if owners:
        row["owner"] = owners[0]
    parents = item.get("parents") or []
    if parents:
        row["folder_id"] = str(parents[0])
    if folder:
        row["folder"] = folder
    if item.get("shared") is not None:
        row["shared"] = bool(item.get("shared"))
    if item.get("sharingUser"):
        row["shared_by"] = person(item["sharingUser"])
    if item.get("trashed"):
        row["trashed"] = True
    if mime == SHORTCUT and item.get("shortcutDetails"):
        row["target_id"] = str((item["shortcutDetails"] or {}).get("targetId") or "")
    return row


async def folder_names(drive: Api, items: list[dict[str, Any]], most: int = 20) -> dict[str, str]:
    """The names of the folders a page of items sits in: each parent asked once, together, at most ``most``; one
    that can't be read is left out."""
    parents = list(dict.fromkeys(str((i.get("parents") or [""])[0]) for i in items if i.get("parents")))[:most]

    async def name(folder_id: str) -> tuple[str, str]:
        try:
            found = await drive.get(f"files/{segment(folder_id)}", params={**ONE, "fields": "name"})
            return folder_id, str(found.get("name") or "")
        except HttpError:
            return folder_id, ""

    return {k: v for k, v in await asyncio.gather(*(name(p) for p in parents)) if v}


async def get_item(drive: Api, item_id: str, fields: str = FIELDS) -> dict[str, Any]:
    return await drive.get(f"files/{segment(item_id)}", params={**ONE, "fields": fields})


async def find_folder(drive: Api, folder_id: Any, folder_path: Any) -> dict[str, Any]:
    """The folder named by id (any drive), or by path from the top of My Drive, one segment at a time. Raises
    not_found naming the first segment that isn't there."""
    if folder_id:
        item = await get_item(drive, str(folder_id))
        if item.get("mimeType") != FOLDER:
            raise HttpError("invalid", f"'{item.get('name')}' is not a folder.")
        return item
    current = await get_item(drive, "root")
    parts = [p.strip() for p in str(folder_path or "/").replace("\\", "/").split("/") if p.strip()]
    for index, part in enumerate(parts):
        answer = await drive.get("files", params={
            **ALL_DRIVES, "corpora": "user", "pageSize": 10, "fields": f"files({FIELDS})",
            "q": f"{literal(current['id'])} in parents and name = {literal(part)} and mimeType = '{FOLDER}' "
                 f"and trashed = false"})
        found = answer.get("files") or []
        if not found:
            raise HttpError("not_found", f"Google Drive has no folder at '/{'/'.join(parts[:index + 1])}'.")
        current = found[0]
    return current


def listing_params(folder: dict[str, Any]) -> dict[str, Any]:
    """Where a folder's children are looked for: its shared drive, or the person's own files."""
    if folder.get("driveId"):
        return {**ALL_DRIVES, "corpora": "drive", "driveId": str(folder["driveId"])}
    return {**ALL_DRIVES, "corpora": "user"}


def u16(text: str) -> int:
    """Google Docs counts UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

__all__ = ["ALL_DRIVES", "Api", "Clients", "DOCUMENT", "FIELDS", "FILE_MOST", "FOLDER", "FORM", "HttpError", "ONE",
           "PRESENTATION", "SPREADSHEET", "WANTED", "addresses", "connect", "failed", "find_folder", "folder_names",
           "get_item", "human_size", "item_row", "kind_of", "listing_params", "literal", "no_account", "person",
           "refused", "segment", "u16"]
