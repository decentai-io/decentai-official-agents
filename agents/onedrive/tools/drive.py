"""Microsoft Graph for one person's OneDrive, and the Word, Excel and Forms files in it.

Built on ``standard.graph`` (immutable ids, Graph's error words, 429/503/504 waited out, writes never sent twice).
What is OneDrive's own:

- **An item is (drive_id, item_id).** A file someone shared lives in their drive; it is reached at
  ``drives/{driveId}/items/{id}``, never ``me/drive/items``. Search and shared-with-me hand both ids on.
- **A download never carries the token.** Graph gives each file a pre-authenticated ``@microsoft.graph.downloadUrl``
  on another host (SharePoint, ``*.files.1drv.com``, ``my.microsoftpersonalcontent.com``; a PDF conversion on
  ``*-mediap.svc.ms``). It is fetched with no Authorization header, held to the chat's file size; failing that,
  ``/content`` is asked and its redirect followed, again without the token.
- **An upload is one PUT under 4 MiB, else an upload session:** fragments of a multiple of 320 KiB, PUT to the
  session's own address with no token, resumed from ``nextExpectedRanges`` when one is lost.
- **A copy is asynchronous:** Graph answers 202 with a monitor address (on SharePoint or OneDrive's own host), read
  with no token until the copy completes.
- **Personal and work accounts differ:** organization links, the workbook (Excel) API and Forms exist only for work
  or school accounts; restore from the recycle bin only for personal ones. ``personal()`` tells them apart.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import requests

from standard.accounts import account
from standard.graph import PERSONAL_TENANT, Graph
from standard.http import FILE_MOST, HttpError, host_of, human_size, segment

#: Hosts Graph sends file bytes, upload sessions and copy monitors to, as the manifest declares them. An address on
#: none of them is refused here with words, rather than by the way out with a bare 403.
FILE_HOSTS = (".sharepoint.com", ".1drv.com", ".microsoftpersonalcontent.com", ".svc.ms", ".storage.live.com",
              ".livefilestore.com", ".onedrive.com")

#: Under this, a file goes up in one PUT; at or above it, through an upload session.
SIMPLE_MOST = 4 * 1024 * 1024
#: An upload session's fragment: Microsoft wants a multiple of 320 KiB (and less than 60 MiB).
FRAGMENT = 10 * 320 * 1024

ITEM_FIELDS = ("id,name,size,file,folder,package,root,remoteItem,parentReference,webUrl,eTag,cTag,"
               "lastModifiedDateTime,lastModifiedBy,createdBy,shared,deleted")


def file_host(url: str) -> bool:
    host = host_of(url)
    return any(host.endswith(suffix) for suffix in FILE_HOSTS)


def where(item_id: str, drive_id: str = "") -> str:
    """The Graph path of an item: in the person's own drive, or in the drive of whoever shared it."""
    if drive_id:
        return f"drives/{segment(drive_id)}/items/{segment(item_id)}"
    return f"me/drive/items/{segment(item_id)}"


def clean_path(path: Any) -> str:
    """ "/Contracts//2026/" as "/Contracts/2026"; the top level is "/"."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p.strip()]
    return "/" + "/".join(parts)


def odata_text(text: str) -> str:
    """A text inside an OData function's quotes: a quote doubled, then escaped for the path."""
    return quote(str(text).replace("'", "''"), safe="")


class Drive(Graph):
    """Graph over one connected Microsoft account, with OneDrive's ways."""

    def __init__(self, secret: dict[str, Any]):
        super().__init__(secret)
        self._drive: dict[str, Any] | None = None

    # -- who and what ------------------------------------------------------------------------------------------
    async def drive(self) -> dict[str, Any]:
        if self._drive is None:
            self._drive = await self.get("me/drive", params={"$select": "id,driveType,quota,owner,webUrl"})
        return self._drive

    async def personal(self) -> bool:
        """A personal Microsoft account (outlook.com, hotmail.com, live.com): its token names Microsoft's consumer
        tenant, or is not a JWT at all, and its drive is of type "personal"."""
        tenant = self.tenant()
        if tenant:
            return tenant == PERSONAL_TENANT
        return str((await self.drive()).get("driveType") or "") == "personal"

    # -- reading items -----------------------------------------------------------------------------------------
    async def item(self, item_id: str, drive_id: str = "") -> dict[str, Any]:
        """One item, with its pre-authenticated download address when it is a file."""
        return await self.get(where(item_id, drive_id))

    async def by_path(self, path: str) -> dict[str, Any]:
        path = clean_path(path)
        if path == "/":
            return await self.get("me/drive/root")
        return await self.get("me/drive/root:" + quote(path, safe="/"))

    async def children(self, folder_id: str, drive_id: str, top: int, page: str = "") -> dict[str, Any]:
        if page:
            return await self.get("", absolute=page)
        return await self.get(f"{where(folder_id, drive_id)}/children", params={"$top": top})

    async def search(self, query: str, top: int, page: str = "") -> dict[str, Any]:
        """The drive's search, which also finds what others shared with the account (as remoteItem)."""
        if page:
            return await self.get("", absolute=page)
        return await self.get(f"me/drive/search(q='{odata_text(query)}')", params={"$top": top})

    async def shared_with_me(self, most: int, query: str = "") -> tuple[list[dict[str, Any]], str]:
        """What others shared with the account, and how it was found. Microsoft is retiring ``sharedWithMe``
        (it answers one item, or nothing, or refuses): whatever it gives is kept, and the drive's search, which
        returns shared files as remoteItems, fills the rest."""
        found: list[dict[str, Any]] = []
        listed, failed_list = 0, False
        try:
            found = await self.pages("me/drive/sharedWithMe", most=most)
            listed = len(found)
        except HttpError as failed:
            if failed.kind in ("auth", "scope", "rate"):
                raise
            failed_list = True
        if len(found) < most:
            seen = {(remote_of(i).get("id") or i.get("id"), drive_of(i)) for i in found}
            answer, pages = await self.search(query or "*", min(200, max(most * 4, 50))), 0
            while True:
                for entry in answer.get("value") or []:
                    key = (remote_of(entry).get("id"), drive_of(entry))
                    if "remoteItem" in entry and key not in seen:
                        seen.add(key)
                        found.append(entry)
                following = str(answer.get("@odata.nextLink") or "")
                pages += 1
                if len(found) >= most or not following or pages >= 5:
                    break
                answer = await self.get("", absolute=following)
        how = ("search" if failed_list or not listed else
               "sharedWithMe" if len(found) == listed else "sharedWithMe and search")
        return found[:most], how

    async def versions(self, item_id: str, drive_id: str = "") -> list[dict[str, Any]]:
        return await self.pages(f"{where(item_id, drive_id)}/versions")

    async def permissions(self, item_id: str, drive_id: str = "") -> list[dict[str, Any]]:
        return await self.pages(f"{where(item_id, drive_id)}/permissions")

    # -- bytes in ----------------------------------------------------------------------------------------------
    async def fetch(self, item: dict[str, Any], drive_id: str = "", most: int = FILE_MOST, as_pdf: bool = False,
                    what: str = "The file") -> bytes:
        """A file's bytes, never with the token: from its pre-authenticated address, or from the address
        ``/content`` redirects to. A PDF conversion always goes through ``/content?format=pdf``."""
        address = "" if as_pdf else str(item.get("@microsoft.graph.downloadUrl") or "")
        if not address:
            address = await self.redirect(f"{where(str(item.get('id') or ''), drive_id)}/content",
                                          {"format": "pdf"} if as_pdf else None)
        if not file_host(address):
            raise HttpError("http", f"Microsoft sent the download to {host_of(address)}, a host this agent does not "
                                    f"declare; nothing was fetched. Report it to the agent's publisher.")
        return await self.download(absolute=address, most=most, authorized=False, what=what)

    async def redirect(self, path: str, params: Any = None) -> str:
        """Where ``/content`` sends the download: its Location, read without following it (following would carry
        the request on to a host the token must not reach, and read the bytes with no size held)."""
        for attempt in range(1, self.ATTEMPTS + 1):
            def ask() -> requests.Response:
                headers = {**self.auth(), **self.headers("GET", path)}
                return self.session.get(self.url(path), params=params, headers=headers, timeout=self.READ_TIMEOUT,
                                        allow_redirects=False)
            try:
                response = await asyncio.to_thread(ask)
            except (requests.ConnectionError, requests.Timeout) as failed:
                if attempt == self.ATTEMPTS:
                    raise HttpError("http", f"Microsoft could not be reached: {failed}") from None
                await asyncio.sleep(self.pause(None, attempt))
                continue
            if 300 <= response.status_code < 400 and response.headers.get("Location"):
                return str(response.headers["Location"])
            if response.status_code in (429, 503, 500, 502, 504) and attempt < self.ATTEMPTS:
                await asyncio.sleep(min(self.pause(response, attempt), 10.0))
                continue
            if response.status_code >= 400:
                raise self.refusal(response, "GET", path, self.detail(response))
            raise HttpError("http", f"Microsoft answered GET {path} with {response.status_code} and no address to "
                                    f"download from.", response.status_code)
        raise HttpError("rate", "Microsoft is busy: try again later.")

    # -- bytes out ---------------------------------------------------------------------------------------------
    async def upload(self, parent_id: str, drive_id: str, name: str, data: bytes, conflict: str) -> dict[str, Any]:
        """A new file in a folder: one PUT under 4 MiB, an upload session at or above it."""
        target = f"{where(parent_id, drive_id)}:/{segment(name)}:"
        if len(data) < SIMPLE_MOST:
            return await self.put(f"{target}/content", params={"@microsoft.graph.conflictBehavior": conflict},
                                  data=data, headers={"Content-Type": "application/octet-stream"})
        session = await self.post(f"{target}/createUploadSession",
                                  json={"item": {"@microsoft.graph.conflictBehavior": conflict}})
        return await self.send_session(str(session.get("uploadUrl") or ""), data)

    async def replace(self, item_id: str, drive_id: str, data: bytes, if_match: str,
                      content_type: str = "application/octet-stream") -> dict[str, Any]:
        """New bytes for an existing file, only while it is still at ``if_match`` (its cTag: a rename does not
        move it, a save does). Someone else's save in between is refused by Microsoft (412), an open lock (423)."""
        headers = {"If-Match": if_match} if if_match else {}
        if len(data) < SIMPLE_MOST:
            return await self.put(f"{where(item_id, drive_id)}/content", data=data,
                                  headers={"Content-Type": content_type, **headers})
        session = await self.post(f"{where(item_id, drive_id)}/createUploadSession", headers=headers,
                                  json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        return await self.send_session(str(session.get("uploadUrl") or ""), data)

    async def send_session(self, address: str, data: bytes) -> dict[str, Any]:
        """The bytes PUT to an upload session in fragments, with no token (Microsoft refuses one there); a lost
        fragment resumed from what the session says it still expects."""
        if not file_host(address):
            raise HttpError("http", f"Microsoft gave an upload address on {host_of(address) or 'no host'}, which "
                                    f"this agent does not declare; nothing was uploaded.")
        total, start, retried = len(data), 0, 0
        while True:
            end = min(start + FRAGMENT, total)
            last = end == total
            try:
                response = await self.request(
                    "PUT", address, data=data[start:end], authorized=False, raw=True,
                    headers={"Content-Range": f"bytes {start}-{end - 1}/{total}",
                             "Content-Type": "application/octet-stream"})
            except HttpError as failed:
                if failed.kind not in ("unknown", "http", "rate") or retried >= 3:
                    if failed.kind == "unknown" and last:
                        raise HttpError("unknown", "The last part of the upload got no answer from Microsoft: the file "
                                                   "may have been saved. Look in the folder before uploading it "
                                                   "again.") from None
                    raise
                retried += 1
                status = await self.request("GET", address, authorized=False, allow=(404,), raw=True)
                if status.status_code == 404:
                    if last:
                        raise HttpError("unknown", "The upload session ended while its last part was unanswered: the "
                                                   "file may have been saved. Look in the folder before uploading "
                                                   "it again.") from None
                    raise HttpError("http", "Microsoft dropped the upload session part way; nothing was saved. Try "
                                            "the upload again.") from None
                start = expected(status.json() if status.content else {}, start)
                continue
            if response.status_code in (200, 201):
                return response.json()
            start = expected(response.json() if response.content else {}, end)
            if start >= total:
                raise HttpError("http", "Microsoft took every byte of the upload but did not say it saved the file.")

    # -- changing items ----------------------------------------------------------------------------------------
    async def create_folder(self, parent_id: str, drive_id: str, name: str) -> dict[str, Any]:
        return await self.post(f"{where(parent_id, drive_id)}/children", json={
            "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    async def update(self, item_id: str, drive_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return await self.patch(where(item_id, drive_id), json=patch)

    async def copy(self, item_id: str, drive_id: str, body: dict[str, Any], conflict: str) -> str:
        """The copy started: the address its progress is read at."""
        params = None if await self.personal() else {"@microsoft.graph.conflictBehavior": conflict}
        response = await self.request("POST", f"{where(item_id, drive_id)}/copy", params=params, json=body,
                                      raw=True)
        return str(response.headers.get("Location") or "")

    async def monitor(self, address: str) -> dict[str, Any]:
        """A copy's progress, read with no token (the monitor is pre-authenticated). A monitor that redirects once
        the copy is done points at the new item: its id is read from the address, never followed with the token."""
        if not file_host(address):
            raise HttpError("invalid", "That is not an address Microsoft gave for a copy's progress.")

        def ask() -> requests.Response:
            return self.session.get(address, timeout=self.READ_TIMEOUT, allow_redirects=False)
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                response = await asyncio.to_thread(ask)
            except (requests.ConnectionError, requests.Timeout) as failed:
                if attempt == self.ATTEMPTS:
                    raise HttpError("http", f"Microsoft could not be reached: {failed}") from None
                await asyncio.sleep(self.pause(None, attempt))
                continue
            if 300 <= response.status_code < 400:
                place = str(response.headers.get("Location") or "")
                found = place.split("/items/", 1)[1].split("?")[0].split("/")[0] if "/items/" in place else ""
                return {"status": "completed", **({"resourceId": found} if found else {})}
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.ATTEMPTS:
                await asyncio.sleep(min(self.pause(response, attempt), 10.0))
                continue
            if response.status_code >= 400:
                raise self.refusal(response, "GET", "the copy's progress", self.detail(response))
            try:
                return response.json() if response.content else {}
            except ValueError:
                return {}
        raise HttpError("rate", "Microsoft is busy: try again later.")

    async def remove(self, item_id: str, drive_id: str = "") -> None:
        """To the recycle bin, not gone."""
        await self.delete(where(item_id, drive_id))

    async def restore(self, item_id: str) -> dict[str, Any]:
        return await self.post(f"me/drive/items/{segment(item_id)}/restore", json={})

    async def invite(self, item_id: str, drive_id: str, body: dict[str, Any]) -> requests.Response:
        return await self.request("POST", f"{where(item_id, drive_id)}/invite", json=body, raw=True)

    async def create_link(self, item_id: str, drive_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.post(f"{where(item_id, drive_id)}/createLink", json=body)

    async def delete_permission(self, item_id: str, drive_id: str, permission_id: str) -> None:
        await self.delete(f"{where(item_id, drive_id)}/permissions/{segment(permission_id)}")


def expected(answer: dict[str, Any], otherwise: int) -> int:
    """Where an upload session wants the next fragment to start, from its nextExpectedRanges ("26-", "12345-55232")."""
    ranges = answer.get("nextExpectedRanges") or []
    try:
        return int(str(ranges[0]).split("-", 1)[0]) if ranges else otherwise
    except ValueError:
        return otherwise


async def drive_for(call: Any, wanted: Any = None, ask: bool = True) -> tuple[Drive | None, str]:
    """The client over the connected Microsoft account wanted (the call's ``account``, or one a record remembers),
    or None and why."""
    secret, why = await account(call, "microsoft", wanted, ask, "Microsoft")
    return (Drive(secret), "") if secret is not None else (None, why)


# -- what Graph writes, read --------------------------------------------------------------------------------------

def person(identity: Any) -> str:
    identity = identity or {}
    user = identity.get("user") or identity.get("siteUser") or identity.get("application") or {}
    return str(user.get("email") or user.get("displayName") or "")


def remote_of(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("remoteItem") or {}


def drive_of(item: dict[str, Any]) -> str:
    """The drive an item lives in, when it is not the account's own: a shared item's."""
    remote = remote_of(item)
    return str(((remote or {}).get("parentReference") or {}).get("driveId") or "")


def item_row(item: dict[str, Any], own_drive: str = "") -> dict[str, Any]:
    """A drive item as the assistant reads it. A shared item is its remoteItem, in the sharer's drive: its drive_id
    goes with it, so the next call reaches it there."""
    remote = remote_of(item)
    source = remote or item
    parent = source.get("parentReference") or {}
    path = str(parent.get("path") or "")
    row: dict[str, Any] = {
        "item_id": str(source.get("id") or item.get("id") or ""),
        "name": str(source.get("name") or item.get("name") or ""),
        "kind": "folder" if "folder" in source else "file",
        "size": int(source.get("size") or 0),
        "mime_type": str((source.get("file") or {}).get("mimeType") or ""),
        "modified": str(source.get("lastModifiedDateTime") or item.get("lastModifiedDateTime") or ""),
        "modified_by": person(source.get("lastModifiedBy") or item.get("lastModifiedBy")),
        "folder": (path.split("root:", 1)[1] or "/") if "root:" in path else "",
        "link": str(source.get("webUrl") or item.get("webUrl") or ""),
        "shared": "shared" in source or bool(remote),
    }
    drive_id = str(parent.get("driveId") or "")
    if remote or (drive_id and own_drive and drive_id != own_drive):
        row["drive_id"] = drive_id
        sharer = person(((source.get("shared") or {}).get("sharedBy")) or (source.get("shared") or {}).get("owner"))
        if sharer:
            row["shared_by"] = sharer
    return row


def permission_row(permission: dict[str, Any]) -> dict[str, Any]:
    roles = permission.get("roles") or []
    role = "owner" if "owner" in roles else "write" if "write" in roles else "read"
    link = permission.get("link") or {}
    invitation = permission.get("invitation") or {}
    who = person(permission.get("grantedToV2") or permission.get("grantedTo")) or str(invitation.get("email") or "")
    if link and not invitation:
        who = {"anonymous": "anyone with the link", "organization": "people in your organization"}.get(
            str(link.get("scope") or ""), who or "specific people with the link")
        via = "link"
    else:
        via = "owner" if role == "owner" else "invite"
    if permission.get("inheritedFrom"):
        via = "inherited"
    row = {"permission_id": str(permission.get("id") or ""), "who": who, "role": role, "via": via}
    if link.get("webUrl"):
        row["link"] = str(link["webUrl"])
    if permission.get("expirationDateTime"):
        row["expires"] = str(permission["expirationDateTime"])
    return row


def too_large(name: str, size: int, link: str) -> tuple[dict[str, Any], str]:
    """The refusal of a file bigger than a chat file may be, with its size and the way to it instead."""
    return {"error": f"'{name}' is {human_size(size)}; at most {human_size(FILE_MOST)} can be brought into the chat, "
                     f"so it was not downloaded. Open it at its link instead.",
            "kind": "too_large", "size": size, "most": FILE_MOST, **({"link": link} if link else {})}, "error"


__all__ = ["Drive", "FILE_HOSTS", "FRAGMENT", "SIMPLE_MOST", "clean_path", "drive_for", "drive_of", "file_host",
           "item_row", "odata_text", "permission_row", "person", "remote_of", "too_large", "where"]
