"""Dropbox (API v2) on the shared HTTP client: busy answers waited out, writes never sent twice, one set of failure
kinds (standard.http).

Dropbox's own rules, kept here:

- **Every call is a POST.** Whether it is a read is said by the caller (``write=False`` reads are tried again on a
  dropped connection or a 5xx). An RPC call carries its arguments as a JSON body (``null`` for none); a content call
  (download, upload) carries them in the ``Dropbox-API-Arg`` header, which must be pure ASCII, so every other
  character is escaped as ``\\uXXXX`` (an Arabic name survives), and answers with ``Dropbox-API-Result``.
- **An error is a 409 whose ``error_summary`` reads like a path** (``path/not_found/..``); that is what the kinds come
  from. A token without a permission is a 401 ``missing_scope``; a rate limit a 429 with ``Retry-After``.
- **A team space is rooted elsewhere.** ``users/get_current_account`` names the account's root namespace; where it
  differs from its home namespace (a team with a team space), every call carries ``Dropbox-API-Path-Root`` so the team
  folders are seen, and paths start at the team space, where the person's own folder is ``home_path``.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from standard.http import FILE_MOST, Http, HttpError, human_size

API_BASE = "https://api.dropboxapi.com"
CONTENT_BASE = "https://content.dropboxapi.com"
#: What one process remembers of an account's root, by token, and for how long.
ROOTS: Dict[str, Tuple[float, Dict[str, Any]]] = {}
ROOT_SECONDS = 600

DENIED = ("no_permission", "access_denied", "not_authorized", "insufficient_permissions", "no_write_permission",
          "team_folder", "disallowed", "email_unverified", "banned_member", "insufficient_plan")


def header_json(value: Dict[str, Any]) -> str:
    """JSON for the Dropbox-API-Arg header: every non-ASCII character, and DEL, escaped as \\uXXXX, because an HTTP
    header cannot carry them and Dropbox documents exactly this escaping."""
    return json.dumps(value, ensure_ascii=True).replace("\x7f", "\\u007f")


def clean_path(path: Any) -> str:
    """ "/Contracts//2026/" as "/Contracts/2026"; the top level is "/"."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p.strip()]
    return "/" + "/".join(parts)


def api_path(path: Any) -> str:
    """What Dropbox calls the top level is the empty string, not "/"."""
    path = clean_path(path)
    return "" if path == "/" else path


def address(item_id_or_path: Any) -> str:
    """An item as the person gave it: an id ("id:…"), a revision, a namespace, or a path."""
    text = str(item_id_or_path or "").strip()
    if text.startswith(("id:", "rev:", "ns:")):
        return text
    return api_path(text)


def summary(error: HttpError) -> str:
    body = error.body if isinstance(error.body, dict) else {}
    return str(body.get("error_summary") or "")


def kind_of(summary_text: str, status: int) -> str:
    if "not_found" in summary_text:
        return "not_found"
    if any(word in summary_text for word in ("conflict", "already_exists")):
        return "conflict"
    if "insufficient_space" in summary_text or "insufficient_quota" in summary_text:
        return "too_large"
    if any(word in summary_text for word in DENIED):
        return "denied"
    if "too_many_write_operations" in summary_text or "too_many_files" in summary_text:
        return "rate"
    if "in_progress" in summary_text:
        return "locked"
    return "invalid" if status in (400, 409, 422) else "http"


class Dropbox(Http):
    SERVICE = "Dropbox"
    BASE = API_BASE + "/2"

    def __init__(self, secret: Dict[str, Any], root: Optional[str] = None):
        given = str(secret.get("api_base_url") or "")
        # Under test one loopback address stands in for both of Dropbox's hosts.
        super().__init__(token=str(secret.get("access_token") or ""),
                         api_base_url=(given.rstrip("/") + "/2") if given else None)
        self.content = (self.base if given and self.base != self.BASE else CONTENT_BASE + "/2")
        self.email = str(secret.get("account") or "")
        #: The namespace every call is rooted at: "" for the account's home (no header); None until read.
        self.root: Optional[str] = root
        self.home_path = ""
        self.team: Dict[str, Any] = {}
        self._people: Dict[str, str] = {}

    # -- what Dropbox wants on every request ---------------------------------------------------------------------
    def root_header(self) -> Dict[str, str]:
        return {"Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": self.root})} if self.root else {}

    def headers(self, method: str, path: str) -> Dict[str, str]:
        content = path.startswith(("http://", "https://"))
        return {**({} if content else {"Content-Type": "application/json"}), **self.root_header()}

    def detail(self, response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:400] or response.reason or str(response.status_code)
        if isinstance(body, dict) and body.get("error_summary"):
            return str(body["error_summary"])[:400]
        return super().detail(response)

    def refusal(self, response: requests.Response, method: str, said: str, words: str) -> HttpError:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        text = str(body.get("error_summary") or "") if isinstance(body, dict) else ""
        if status == 401:
            if "missing_scope" in text:
                return HttpError("scope", f"Dropbox refused {said}: the account was connected without a permission "
                                          f"this needs ({words}). Reconnect it from the agent's Credentials.",
                                 status, body)
            return HttpError("auth", f"The Dropbox connection has expired or was revoked: reconnect the account from "
                                     f"the agent's Credentials. ({words})", status, body)
        kind = kind_of(text, status) if text else {400: "invalid", 403: "denied", 404: "not_found"}.get(status, "http")
        if kind == "not_found":
            return HttpError(kind, f"Dropbox has no such file or folder ({said}: {words}).", status, body)
        return HttpError(kind, f"Dropbox refused {said}: {words}", status, body)

    # -- calls ---------------------------------------------------------------------------------------------------
    async def rpc(self, route: str, arguments: Any = None, *, write: bool = False) -> Any:
        """An RPC call: its JSON answer ({} for none). A root that changed under the account (a team moved to a team
        space) is taken from Dropbox's own refusal, once."""
        for attempt in (1, 2):
            try:
                answer = await self.request("POST", route, data=json.dumps(arguments).encode("utf-8"), write=write)
                return {} if answer is None else answer
            except HttpError as failed:
                moved = self._moved_root(failed)
                if moved is None or attempt == 2:
                    raise
                self.root = moved
        return {}

    @staticmethod
    def _moved_root(failed: HttpError) -> Optional[str]:
        body = failed.body if isinstance(failed.body, dict) else {}
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        if failed.status != 422 or error.get(".tag") != "invalid_root":
            return None
        return str((error.get("invalid_root") or {}).get("root_namespace_id") or "") or None

    async def prepared(self) -> "Dropbox":
        """The account's root read (once per process for a while): a team space is rooted at the team's
        namespace."""
        if self.root is not None:
            return self
        kept = ROOTS.get(self.token)
        if kept and time.monotonic() - kept[0] < ROOT_SECONDS:
            info = kept[1]
        else:
            account = await self.rpc("users/get_current_account")
            root_info = account.get("root_info") or {}
            root, home = str(root_info.get("root_namespace_id") or ""), str(root_info.get("home_namespace_id") or "")
            info = {"root": root if root and root != home else "",
                    "home_path": str(root_info.get("home_path") or "") if root and root != home else "",
                    "team": account.get("team") or {}, "email": str(account.get("email") or "")}
            ROOTS[self.token] = (time.monotonic(), info)
        self.root, self.home_path, self.team = info["root"], info["home_path"], dict(info["team"])
        self.email = self.email or info["email"]
        return self

    async def content_down(self, route: str, arguments: Dict[str, Any], most: int = FILE_MOST,
                           what: str = "The file") -> Tuple[Dict[str, Any], bytes]:
        """A content download: its metadata from Dropbox-API-Result, and its bytes, at most ``most`` (refused as
        too_large before reading when Dropbox says its length)."""
        url = f"{self.content}/{route}"

        def fetch() -> Tuple[Dict[str, Any], bytes]:
            sent = {**self.auth(), "Dropbox-API-Arg": header_json(arguments), **self.root_header()}
            with self.session.post(url, headers=sent, timeout=self.READ_TIMEOUT * 3, stream=True) as response:
                if response.status_code >= 400:
                    if self.busy(response):
                        later = self.retry_after(response)
                        raise HttpError("rate", f"Dropbox is busy: try again in {int(later) or 'a few'} seconds.",
                                        response.status_code, {"retry_after": later})
                    raise self.refusal(response, "POST", route, self.detail(response))
                result = json.loads(response.headers.get("Dropbox-API-Result") or "{}")
                told = int(response.headers.get("Content-Length") or 0)
                if told > most:
                    raise HttpError("too_large", f"{what} is {human_size(told)}; at most {human_size(most)} can be "
                                                 f"brought into the chat.")
                held = bytearray()
                for piece in response.iter_content(64 * 1024):
                    held += piece
                    if len(held) > most:
                        raise HttpError("too_large", f"{what} is more than {human_size(most)}, the most that can be "
                                                     f"brought into the chat.")
                return result, bytes(held)

        waited = 0.0
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(fetch)
            except (requests.ConnectionError, requests.Timeout) as failed:
                if attempt == self.ATTEMPTS:
                    raise HttpError("http", f"Dropbox could not be reached: {failed}") from None
            except HttpError as refused:
                if refused.kind != "rate" and refused.status not in (500, 502, 503, 504) or attempt == self.ATTEMPTS:
                    raise
                # Dropbox's own wait, when it named one: one longer than a call waits is passed on at once.
                named = refused.body.get("retry_after") if isinstance(refused.body, dict) else 0
                pause = float(named or 0) or self.pause(None, attempt)
                if waited + pause > self.WAIT_MOST:
                    raise
                waited += pause
                await asyncio.sleep(pause)
                continue
            pause = self.pause(None, attempt)
            if waited + pause > self.WAIT_MOST:
                raise HttpError("rate", "Dropbox is busy: try again later.")
            waited += pause
            await asyncio.sleep(pause)
        raise HttpError("http", "Dropbox could not be reached.")

    async def content_up(self, route: str, arguments: Dict[str, Any], raw: bytes) -> Dict[str, Any]:
        return await self.request("POST", f"{self.content}/{route}", data=raw, write=True, timeout=120,
                                  headers={"Dropbox-API-Arg": header_json(arguments),
                                           "Content-Type": "application/octet-stream"})

    # -- account -------------------------------------------------------------------------------------------------
    async def account(self) -> Dict[str, Any]:
        return await self.rpc("users/get_current_account")

    async def space(self) -> Dict[str, Any]:
        return await self.rpc("users/get_space_usage")

    async def person(self, account_id: str) -> str:
        """An account id as an address, once per id: Dropbox names who changed a file in a shared folder only by
        account id."""
        if not account_id:
            return ""
        if account_id not in self._people:
            try:
                found = await self.rpc("users/get_account", {"account_id": account_id})
                self._people[account_id] = str(found.get("email") or (found.get("name") or {}).get("display_name")
                                               or "")
            except HttpError as failed:
                if failed.kind in ("auth", "rate"):
                    raise
                self._people[account_id] = ""
        return self._people[account_id]

    # -- reads ---------------------------------------------------------------------------------------------------
    async def metadata(self, item: Any) -> Dict[str, Any]:
        """An item by id or path. In a team space a path the person gave from their own folder ("/Contracts") is
        tried under their folder (home_path) when the team space has nothing there."""
        wanted = address(item)
        try:
            return await self.rpc("files/get_metadata", {"path": wanted, "include_has_explicit_shared_members": True})
        except HttpError as failed:
            home = self.home_path.rstrip("/")
            if (failed.kind != "not_found" or not home or not wanted.startswith("/")
                    or wanted.lower() == home.lower() or wanted.lower().startswith(home.lower() + "/")):
                raise
            return await self.rpc("files/get_metadata", {"path": home + wanted,
                                                         "include_has_explicit_shared_members": True})

    async def list_folder(self, path: str, limit: int) -> Dict[str, Any]:
        return await self.rpc("files/list_folder", {"path": address(path), "limit": max(1, min(2000, limit)),
                                                    "include_has_explicit_shared_members": True})

    async def list_folder_continue(self, cursor: str) -> Dict[str, Any]:
        return await self.rpc("files/list_folder/continue", {"cursor": cursor})

    async def latest_cursor(self, path: str, limit: int) -> str:
        answer = await self.rpc("files/list_folder/get_latest_cursor", {
            "path": address(path), "recursive": True, "limit": max(1, min(2000, limit))})
        return str(answer.get("cursor") or "")

    async def search(self, query: str, limit: int, cursor: str = "") -> Dict[str, Any]:
        if cursor:
            return await self.rpc("files/search/continue_v2", {"cursor": cursor})
        return await self.rpc("files/search_v2", {"query": query, "options": {
            "max_results": max(1, min(1000, limit)), "file_status": "active", "filename_only": False}})

    async def received_files(self, most: int) -> List[Dict[str, Any]]:
        """Files shared with the account, every page up to ``most``."""
        answer = await self.rpc("sharing/list_received_files", {"limit": 300})
        found = list(answer.get("entries") or [])
        while answer.get("cursor") and len(found) < most:
            answer = await self.rpc("sharing/list_received_files/continue", {"cursor": answer["cursor"]})
            found += answer.get("entries") or []
        return found[:most]

    async def shared_folders(self, most: int) -> List[Dict[str, Any]]:
        """Every shared folder the account is a member of, mounted in its Dropbox or not, up to ``most``."""
        answer = await self.rpc("sharing/list_folders", {"limit": 1000})
        found = list(answer.get("entries") or [])
        while answer.get("cursor") and len(found) < most:
            answer = await self.rpc("sharing/list_folders/continue", {"cursor": answer["cursor"]})
            found += answer.get("entries") or []
        return found[:most]

    async def received_file(self, file_id: str) -> Dict[str, Any]:
        return await self.rpc("sharing/get_file_metadata", {"file": file_id})

    async def link_metadata(self, url: str) -> Dict[str, Any]:
        return await self.rpc("sharing/get_shared_link_metadata", {"url": url})

    async def temporary_link(self, item: str) -> str:
        """A direct download link Dropbox keeps for four hours: the way out for a file too large for the chat."""
        try:
            return str((await self.rpc("files/get_temporary_link", {"path": address(item)})).get("link") or "")
        except HttpError:
            return ""

    async def download(self, item: str, what: str) -> Tuple[Dict[str, Any], bytes]:
        return await self.content_down("files/download", {"path": address(item)}, what=what)

    async def export(self, item: str, what: str) -> Tuple[Dict[str, Any], bytes]:
        return await self.content_down("files/export", {"path": address(item)}, what=what)

    async def preview(self, item: str, what: str) -> bytes:
        """Dropbox's own PDF rendering of a Word or PowerPoint file."""
        return (await self.content_down("files/get_preview", {"path": address(item)}, what=what))[1]

    async def link_file(self, url: str, what: str) -> bytes:
        return (await self.content_down("sharing/get_shared_link_file", {"url": url}, what=what))[1]

    async def revisions(self, path: str, limit: int) -> Dict[str, Any]:
        return await self.rpc("files/list_revisions", {"path": path, "mode": "path",
                                                       "limit": max(1, min(100, limit))})

    async def file_members(self, item: str, most: int) -> Tuple[Dict[str, Any], bool]:
        """Everyone a file is shared with, every page (Dropbox gives at most 300 a page): users, groups and
        invitees, and whether more were left than ``most``."""
        answer = await self.rpc("sharing/list_file_members", {"file": address(item), "include_inherited": True,
                                                              "limit": 300})
        return await self._all_members(answer, "sharing/list_file_members/continue", most)

    async def folder_members(self, shared_folder_id: str, most: int) -> Tuple[Dict[str, Any], bool]:
        answer = await self.rpc("sharing/list_folder_members", {"shared_folder_id": shared_folder_id,
                                                                "limit": 1000})
        return await self._all_members(answer, "sharing/list_folder_members/continue", most)

    async def _all_members(self, answer: Dict[str, Any], route: str, most: int) -> Tuple[Dict[str, Any], bool]:
        found = {kind: list(answer.get(kind) or []) for kind in ("users", "groups", "invitees")}
        while answer.get("cursor") and sum(map(len, found.values())) < most:
            answer = await self.rpc(route, {"cursor": answer["cursor"]})
            for kind in found:
                found[kind] += answer.get(kind) or []
        return found, bool(answer.get("cursor"))

    async def links(self, item: str) -> List[Dict[str, Any]]:
        answer = await self.rpc("sharing/list_shared_links", {"path": address(item), "direct_only": True})
        found = list(answer.get("links") or [])
        while answer.get("has_more") and answer.get("cursor") and len(found) < 200:
            answer = await self.rpc("sharing/list_shared_links", {"path": address(item), "direct_only": True,
                                                                  "cursor": answer["cursor"]})
            found += answer.get("links") or []
        return found

    async def share_job(self, job_id: str) -> Dict[str, Any]:
        return await self.rpc("sharing/check_share_job_status", {"async_job_id": job_id})

    async def job(self, job_id: str) -> Dict[str, Any]:
        return await self.rpc("sharing/check_job_status", {"async_job_id": job_id})

    async def remove_job(self, job_id: str) -> Dict[str, Any]:
        return await self.rpc("sharing/check_remove_member_job_status", {"async_job_id": job_id})

    # -- writes --------------------------------------------------------------------------------------------------
    async def upload(self, path: str, raw: bytes, on_conflict: str) -> Dict[str, Any]:
        """One request: Dropbox takes up to 150 MB this way, and a chat file is far less. "rename" is Dropbox's own
        autorename, done on its side."""
        return await self.content_up("files/upload", {
            "path": path, "mode": "overwrite" if on_conflict == "replace" else "add",
            "autorename": on_conflict == "rename", "mute": False, "strict_conflict": False}, raw)

    async def create_folder(self, path: str) -> Dict[str, Any]:
        return (await self.rpc("files/create_folder_v2", {"path": path, "autorename": False},
                               write=True)).get("metadata") or {}

    async def move(self, from_path: str, to_path: str) -> Dict[str, Any]:
        return (await self.rpc("files/move_v2", {"from_path": from_path, "to_path": to_path, "autorename": False},
                               write=True)).get("metadata") or {}

    async def delete(self, item: str) -> Dict[str, Any]:
        """To Dropbox's deleted files, restorable for the plan's retention."""
        return (await self.rpc("files/delete_v2", {"path": address(item)}, write=True)).get("metadata") or {}

    async def restore(self, path: str, rev: str) -> Dict[str, Any]:
        return await self.rpc("files/restore", {"path": path, "rev": rev}, write=True)

    async def add_file_members(self, item: str, emails: List[str], role: str, message: str) -> List[Dict[str, Any]]:
        arguments: Dict[str, Any] = {
            "file": address(item), "members": [{".tag": "email", "email": e} for e in emails],
            "quiet": False, "access_level": {".tag": access_level(role)}, "add_message_as_comment": False}
        if message:
            arguments["custom_message"] = message
        answer = await self.rpc("sharing/add_file_member", arguments, write=True)
        return answer if isinstance(answer, list) else []

    async def share_folder(self, path: str) -> Dict[str, Any]:
        return await self.rpc("sharing/share_folder", {"path": path, "force_async": False}, write=True)

    async def unshare_folder(self, shared_folder_id: str) -> Dict[str, Any]:
        return await self.rpc("sharing/unshare_folder", {"shared_folder_id": shared_folder_id,
                                                         "leave_a_copy": False}, write=True)

    async def add_folder_members(self, shared_folder_id: str, emails: List[str], role: str, message: str) -> None:
        arguments: Dict[str, Any] = {
            "shared_folder_id": shared_folder_id, "quiet": False,
            "members": [{"member": {".tag": "email", "email": e}, "access_level": {".tag": access_level(role)}}
                        for e in emails]}
        if message:
            arguments["custom_message"] = message
        await self.rpc("sharing/add_folder_member", arguments, write=True)

    async def create_link(self, path: str, audience: str, access: str) -> Dict[str, Any]:
        return await self.rpc("sharing/create_shared_link_with_settings", {
            "path": path, "settings": {"audience": {".tag": audience}, "access": {".tag": access}}}, write=True)

    async def modify_link(self, url: str, audience: str, access: str) -> Dict[str, Any]:
        return await self.rpc("sharing/modify_shared_link_settings", {
            "url": url, "settings": {"audience": {".tag": audience}, "access": {".tag": access}}}, write=True)

    async def revoke_link(self, url: str) -> None:
        await self.rpc("sharing/revoke_shared_link", {"url": url}, write=True)

    async def remove_file_member(self, item: str, member: Dict[str, Any]) -> None:
        await self.rpc("sharing/remove_file_member_2", {"file": address(item), "member": member}, write=True)

    async def remove_folder_member(self, shared_folder_id: str, member: Dict[str, Any]) -> Dict[str, Any]:
        return await self.rpc("sharing/remove_folder_member", {
            "shared_folder_id": shared_folder_id, "member": member, "leave_a_copy": False}, write=True)


def access_level(role: str) -> str:
    return "editor" if role == "write" else "viewer"


def tag(value: Any) -> str:
    return str((value or {}).get(".tag") or "") if isinstance(value, dict) else str(value or "")


def parent_of(path_display: str) -> str:
    if not path_display:
        return ""
    head = path_display.rsplit("/", 1)[0]
    return head or "/"


async def item_rows(client: Dropbox, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Items as rows, with who last changed each in a shared folder named by address."""
    for item in items:
        await client.person(str((item.get("sharing_info") or {}).get("modified_by") or ""))
    return [item_row(item, client._people) for item in items]


def item_row(item: Dict[str, Any], people: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A file or folder as the agent shows it. Dropbox has no web address in its metadata, so ``link`` is filled only
    where Dropbox gave one; a made-up URL would be an invented path."""
    kind = tag(item)
    sharing = item.get("sharing_info") or {}
    modified_by = str(sharing.get("modified_by") or "")
    return {
        "item_id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "kind": "folder" if kind == "folder" else "file",
        "size": int(item.get("size") or 0),
        # Dropbox keeps no content type; the name's extension is all there is.
        "mime_type": "" if kind == "folder" else (mimetypes.guess_type(str(item.get("name") or ""))[0] or ""),
        "modified": str(item.get("server_modified") or ""),
        "modified_by": (people or {}).get(modified_by, "") if modified_by else "",
        "folder": parent_of(str(item.get("path_display") or "")),
        "path": str(item.get("path_display") or ""),
        "link": str(item.get("preview_url") or ""),
        "shared": bool(sharing) or bool(item.get("has_explicit_shared_members")),
    }


ROLES = {"owner": "owner", "editor": "write", "viewer": "read", "viewer_no_comment": "read", "traverse": "read"}


def member_rows(members: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Dropbox's users, groups and invitees as permission rows. The id says how to take the access away again:
    ``member:<address>``, ``account:<dbid>`` or ``group:<id>``."""
    rows = []
    for user in members.get("users") or []:
        person = user.get("user") or {}
        role = ROLES.get(tag(user.get("access_type")), "read")
        email = str(person.get("email") or "")
        rows.append({
            "permission_id": f"member:{email}" if email else f"account:{person.get('account_id') or ''}",
            "who": email or str(person.get("display_name") or ""),
            "role": "owner" if role == "owner" else role,
            "via": "owner" if role == "owner" else "inherited" if user.get("is_inherited") else "invite",
            "link": ""})
    for group in members.get("groups") or []:
        info = group.get("group") or {}
        rows.append({"permission_id": f"group:{info.get('group_id') or ''}",
                     "who": str(info.get("group_name") or ""),
                     "role": ROLES.get(tag(group.get("access_type")), "read"),
                     "via": "inherited" if group.get("is_inherited") else "invite", "link": ""})
    for invitee in members.get("invitees") or []:
        email = str((invitee.get("invitee") or {}).get("email") or "")
        rows.append({"permission_id": f"member:{email}", "who": email,
                     "role": ROLES.get(tag(invitee.get("access_type")), "read"),
                     "via": "inherited" if invitee.get("is_inherited") else "invite", "link": ""})
    return rows


REACH = {"public": "anyone with the link", "team_only": "people in your team", "team": "people in your team",
         "password": "anyone with the link and its password", "no_one": "no one (the link is disabled)",
         "members": "the people the item is shared with"}


def link_row(link: Dict[str, Any]) -> Dict[str, Any]:
    settings = link.get("link_permissions") or {}
    visibility = tag(settings.get("effective_audience")) or tag(settings.get("resolved_visibility")) or "public"
    access = tag(settings.get("link_access_level"))
    url = str(link.get("url") or "")
    return {"permission_id": f"link:{url}", "who": REACH.get(visibility, visibility),
            "role": "write" if access == "editor" else "read", "via": "link", "link": url}
