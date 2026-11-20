"""A loopback Dropbox: the API v2 subset the Dropbox agent calls, over real HTTP, holding a small fictional Dropbox for
Sidra Office Supplies.

It behaves the way Dropbox does where the agent depends on it, and refuses what Dropbox refuses:

- every call is a POST; an RPC call takes a JSON body (``null`` for none) with ``Content-Type: application/json``, and
  a content call takes its arguments in the ``Dropbox-API-Arg`` header, which must be ASCII, answering with
  ``Dropbox-API-Result``; a body Dropbox can't read is a 400 in plain text;
- an error is a 409 whose ``error_summary`` reads like a path; a missing token a 401; a rate limit a 429 with
  ``Retry-After``; a path ending in "/" is malformed; ``limit`` arguments are held to Dropbox's bounds (list_folder
  2000, folder members 1000, file members 300, received files 300, shared folders 1000, revisions 100, search 1000);
- a team space: ``users/get_current_account`` names a root namespace other than the home one, and only a call with
  ``Dropbox-API-Path-Root: {".tag": "root", "root": <it>}`` sees the team folders, with the person's own folder at
  ``home_path``; without it, paths are rooted at the home folder. A wrong root is a 422 ``invalid_root``;
- items carry ids ``id:…`` and revisions ``rev``; an upload in "add" mode with autorename gets a numbered name unless
  the bytes are the same; a file shared with the account but never added to it answers only to the sharing calls;
  members and shared lists page with a cursor and ``/continue``; sharing a folder may finish in the background
  (``async_job_id``), and so do unsharing and removing a member; a cursor answers with what changed after it, a page
  at a time, until Dropbox declares it stale ("reset"). One address stands in for both of Dropbox's hosts.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from tests.conftest import stands_for

PREVIEWS = (".doc", ".docx", ".ppt", ".pptx")
LIMITS = {"files/list_folder": 2000, "files/list_folder/get_latest_cursor": 2000,
          "sharing/list_folder_members": 1000, "sharing/list_file_members": 300,
          "sharing/list_received_files": 300, "sharing/list_folders": 1000, "files/list_revisions": 100}
PATH_KEYS = ("path", "from_path", "to_path", "file")


class Refused(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(str(body))
        self.status, self.body = status, body


def error(summary: str, detail: Optional[Dict[str, Any]] = None) -> Refused:
    return Refused(409, {"error_summary": summary, "error": detail or {".tag": summary.split("/")[0]}})


class DropboxStub:
    ACCOUNT = "demo@sidra.example"
    ACCOUNT_ID = "dbid:AADdemo"
    PEOPLE = {"dbid:AADdana": "dana@sidra.example"}
    TEAM = {"id": "dbtid:AAsidra", "name": "Sidra Office Supplies"}
    HOME_NS, ROOT_NS = "1001", "7007"

    def __init__(self, team: bool = True, team_space: bool = False):
        self.team = dict(self.TEAM) if team else None
        #: In a team space the person's own folder sits at /Demo under the team's root.
        self.home = "/Demo" if team_space else ""
        self.items: Dict[str, Dict[str, Any]] = {}       # path_lower (from the root) -> item
        self.history: Dict[str, List[Dict[str, Any]]] = {}  # path_lower -> revisions, newest last
        self.file_members: Dict[str, List[Dict[str, str]]] = {}
        self.folder_members: Dict[str, List[Dict[str, str]]] = {}
        self.links: Dict[str, List[Dict[str, Any]]] = {}  # item id -> links
        self.received: List[Dict[str, Any]] = []
        self.shared_folders_with_me: List[Dict[str, Any]] = []
        self.deleted_files: List[str] = []
        self.log: List[tuple] = []                        # (seq, entry)
        self.seq = 0
        self.reset_below = 0
        self.uploads: List[str] = []
        self.downloads: List[str] = []                    # content calls made
        self.calls: List[str] = []
        self.roots_sent: List[Optional[str]] = []
        self.drop_upload = False
        self.rate_limited = 0                             # answer this many calls with 429
        self.retry_after = "30"
        self.busy_folder = ""                             # a watched folder whose cursor answers 429
        self.member_page = 0                              # members per page, when less than the limit
        self.share_async = False
        self.refuse_folder_invites = False
        self.refuse_unshare = False
        self.unshared: List[str] = []
        self.jobs: Dict[str, List[Dict[str, Any]]] = {}
        self._n = 0
        self._lock = threading.RLock()
        self._server: Optional[ThreadingHTTPServer] = None
        if self.home:
            self.add_folder(self.home)

    # -- fictional data ----------------------------------------------------------------------------------------
    def _id(self) -> str:
        self._n += 1
        return f"id:a4ayc_{self._n:04d}"

    def _rev(self) -> str:
        self._n += 1
        return f"{0x5a1000000 + self._n:x}"

    def _stamp(self) -> str:
        return f"2026-09-01T10:{self.seq % 60:02d}:00Z"

    def _record(self, entry: Dict[str, Any]) -> None:
        self.seq += 1
        self.log.append((self.seq, dict(entry)))

    def _shared_folder_of(self, path_lower: str) -> str:
        for folder_path, item in self.items.items():
            sfid = (item.get("sharing_info") or {}).get("shared_folder_id")
            if sfid and path_lower.startswith(folder_path + "/"):
                return sfid
        return ""

    def at(self, path: str) -> str:
        """A path the person's own way (from their folder) as it is from the root."""
        return f"{self.home}{path}" if path.startswith("/") or not path else path

    def add_folder(self, path: str, by: str = "") -> Dict[str, Any]:
        current = ""
        item: Dict[str, Any] = {}
        for part in [p for p in path.split("/") if p]:
            current = f"{current}/{part}"
            if current.lower() not in self.items:
                item = {".tag": "folder", "id": self._id(), "name": part, "path_display": current,
                        "path_lower": current.lower()}
                parent_sf = self._shared_folder_of(current.lower())
                if parent_sf:
                    item["sharing_info"] = {"read_only": False, "parent_shared_folder_id": parent_sf}
                self.items[current.lower()] = item
                self._record(self.view(item))
            item = self.items[current.lower()]
        return item

    def add_file(self, folder: str, name: str, content: bytes, by: str = "", **extra) -> str:
        """A file someone puts in the Dropbox; ``folder`` is from the root (in a team space, "/Demo/…" is the person's
        own folder)."""
        with self._lock:
            parent = self.add_folder(folder)["path_display"] if folder.strip("/") else ""
            path = f"{parent}/{name}"
            existing = self.items.get(path.lower())
            item = existing or {".tag": "file", "id": self._id(), "name": name, "path_display": path,
                                "path_lower": path.lower()}
            item.update({"size": len(content), "_content": content, "server_modified": self._stamp(),
                         "rev": self._rev(), **extra})
            parent_sf = self._shared_folder_of(path.lower())
            if parent_sf:
                item["sharing_info"] = {"read_only": False, "parent_shared_folder_id": parent_sf,
                                        "modified_by": by or self.ACCOUNT_ID}
            self.items[path.lower()] = item
            self.history.setdefault(path.lower(), []).append(dict(item))
            self._record(self.view(item))
            return item["id"]

    def delete(self, path: str) -> None:
        """Someone deletes an item (and what is under it)."""
        lower = path.lower()
        for key in sorted([k for k in self.items if k == lower or k.startswith(lower + "/")], reverse=True):
            gone = self.items.pop(key)
            self.deleted_files.append(gone["path_display"])
            self._record({".tag": "deleted", "name": gone["name"], "path_display": gone["path_display"],
                          "path_lower": key})

    def share_folder_with(self, path: str, email: str) -> str:
        folder = self.items[path.lower()]
        sfid = self._make_shared(folder)
        self.folder_members[sfid].append({"email": email, "access": "editor"})
        return sfid

    def _make_shared(self, folder: Dict[str, Any]) -> str:
        sfid = (folder.get("sharing_info") or {}).get("shared_folder_id")
        if sfid:
            return sfid
        self._n += 1
        sfid = f"84528{self._n:04d}"
        folder["sharing_info"] = {"read_only": False, "shared_folder_id": sfid}
        self.folder_members[sfid] = []
        for key, item in self.items.items():
            if key.startswith(folder["path_lower"] + "/"):
                item.setdefault("sharing_info", {})["parent_shared_folder_id"] = sfid
        return sfid

    def _unshare(self, sfid: str) -> None:
        for item in self.items.values():
            info = item.get("sharing_info") or {}
            if info.get("shared_folder_id") == sfid or info.get("parent_shared_folder_id") == sfid:
                item.pop("sharing_info", None)
                self.unshared.append(item["path_display"])
        self.folder_members.pop(sfid, None)

    def share_with_me(self, name: str, content: bytes, by: str, when: str = "2026-08-30T09:00:00Z") -> str:
        file_id = self._id()
        self.received.append({
            "id": file_id, "name": name, "_content": content, "size": len(content),
            "preview_url": f"https://www.dropbox.com/scl/fi/{file_id[3:]}/{name}?dl=0",
            "owner_display_names": [by], "time_invited": when, "access_type": {".tag": "viewer"}, "policy": {}})
        return file_id

    def folder_shared_with_me(self, name: str, by: str, mounted: bool, when: str) -> str:
        self._n += 1
        sfid = f"99000{self._n:04d}"
        entry = {"name": name, "shared_folder_id": sfid, "access_type": {".tag": "editor"},
                 "is_inside_team_folder": False, "is_team_folder": False, "owner_display_names": [by],
                 "preview_url": f"https://www.dropbox.com/scl/fo/{sfid}/{name}?dl=0", "time_invited": when,
                 "policy": {}, "permissions": []}
        if mounted:
            entry["path_lower"] = f"/{name}".lower()
        self.shared_folders_with_me.append(entry)
        return sfid

    def invalidate_cursors(self) -> None:
        """Dropbox declares every cursor taken so far stale."""
        self.seq += 1
        self.reset_below = self.seq

    def by_path(self, path: str) -> Optional[Dict[str, Any]]:
        return self.items.get(path.lower())

    def view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _resolve(self, path: str) -> Optional[Dict[str, Any]]:
        if str(path).startswith("id:"):
            return next((i for i in self.items.values() if i["id"] == path), None)
        return self.items.get(str(path).lower())

    def _children(self, folder_lower: str) -> List[Dict[str, Any]]:
        return [i for k, i in self.items.items() if k.rsplit("/", 1)[0] == folder_lower]

    def _members(self, owner_first: List[Dict[str, str]], inherited: List[Dict[str, str]]):
        users = [{"access_type": {".tag": "owner"}, "is_inherited": False,
                  "user": {"account_id": self.ACCOUNT_ID, "email": self.ACCOUNT, "display_name": "Demo"}}]
        for member, is_inherited in [(m, False) for m in owner_first] + [(m, True) for m in inherited]:
            users.append({"access_type": {".tag": member["access"]}, "is_inherited": is_inherited,
                          "user": {"account_id": "dbid:" + member["email"], "email": member["email"],
                                   "display_name": member["email"]}})
        return users

    def _link_view(self, item: Dict[str, Any], link: Dict[str, Any]) -> Dict[str, Any]:
        audience = link["audience"]
        return {".tag": item[".tag"], "url": link["url"], "name": item["name"], "id": item["id"],
                "path_lower": item["path_lower"],
                "link_permissions": {
                    "resolved_visibility": {".tag": "team_only" if audience == "team" else "public"},
                    "effective_audience": {".tag": audience},
                    "link_access_level": {".tag": link["access"]}}}

    def _job(self, answers: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A job Dropbox finishes in the background: these answers, one per check, the last kept."""
        self._n += 1
        job = f"dbjid:job{self._n}"
        self.jobs[job] = list(answers)
        return {".tag": "async_job_id", "async_job_id": job}

    # -- the server ----------------------------------------------------------------------------------------------
    def start(self) -> "DropboxStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            prefix = ""

            def log_message(self, *_):
                pass

            # -- the person's paths and the root's ------------------------------------------------------------
            def into(self, arg: Any) -> Any:
                """Paths as the call gave them, from the root: under the person's folder unless rooted at the team."""
                if not self.prefix or not isinstance(arg, dict):
                    return arg
                return {k: (stub.at(v) if k in PATH_KEYS and isinstance(v, str) and not v.startswith(("id:", "ns:"))
                            else v) for k, v in arg.items()}

            def out(self, value: Any) -> Any:
                if not self.prefix:
                    return value
                if isinstance(value, list):
                    return [self.out(v) for v in value]
                if isinstance(value, dict):
                    found = {}
                    for key, item in value.items():
                        if key in ("path_display", "path_lower") and isinstance(item, str) and \
                                item.lower().startswith(self.prefix):
                            item = item[len(self.prefix):] or "/"
                        found[key] = self.out(item)
                    return found
                return value

            def _json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
                raw = json.dumps(self.out(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _text(self, status: int, text: str) -> None:
                raw = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _bytes(self, raw: bytes, result: Dict[str, Any]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Dropbox-API-Result", json.dumps(self.out(result)))
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                self._text(405, "Dropbox's API is called with POST.")

            def do_POST(self):  # noqa: N802
                method = self.path.split("?", 1)[0][len("/2/"):]
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.calls.append(method)
                if self.headers.get("Authorization") != "Bearer at-1":
                    return self._json(401, {"error_summary": "expired_access_token/..",
                                            "error": {".tag": "expired_access_token"}})
                if stub.rate_limited > 0:
                    stub.rate_limited -= 1
                    return self._json(429, {"error_summary": "too_many_requests/..",
                                            "error": {"reason": {".tag": "too_many_requests"},
                                                      "retry_after": int(stub.retry_after)}},
                                      {"Retry-After": stub.retry_after})
                root = self.headers.get("Dropbox-API-Path-Root")
                stub.roots_sent.append(root)
                self.prefix = stub.home.lower()
                if root is not None:
                    wanted = json.loads(root)
                    actual = stub.ROOT_NS if stub.home else stub.HOME_NS
                    if wanted.get(".tag") != "root" or str(wanted.get("root")) != actual:
                        return self._json(422, {"error_summary": "invalid_root/..", "error": {
                            ".tag": "invalid_root", "invalid_root": {
                                ".tag": "team" if stub.home else "user", "root_namespace_id": actual}}})
                    self.prefix = ""
                try:
                    with stub._lock:
                        if self.headers.get("Dropbox-API-Arg") is not None:
                            header = self.headers["Dropbox-API-Arg"]
                            if not header.isascii():
                                return self._text(400, "Error in call to API function: Dropbox-API-Arg must be "
                                                       "ASCII; escape other characters as \\uXXXX.")
                            return self._content(method, self.into(json.loads(header)), raw)
                        if self.headers.get("Content-Type") != "application/json":
                            return self._text(400, f"Error in call to API function \"{method}\": Bad HTTP "
                                                   f"\"Content-Type\" header: {self.headers.get('Content-Type')!r}.")
                        arg = json.loads(raw or b"null")
                        self._limits(method, arg)
                        return self._rpc(method, self.into(arg))
                except Refused as refused:
                    if isinstance(refused.body, str):
                        return self._text(refused.status, refused.body)
                    return self._json(refused.status, refused.body)

            @staticmethod
            def _limits(method: str, arg: Any) -> None:
                most = LIMITS.get(method)
                if most and isinstance(arg, dict) and "limit" in arg and not 1 <= int(arg["limit"]) <= most:
                    raise Refused(400, f"Error in call to API function \"{method}\": request body: limit: "
                                       f"{arg['limit']} is not within 1..{most}")
                if method == "files/search_v2" and not 1 <= int(arg["options"]["max_results"]) <= 1000:
                    raise Refused(400, "Error in call to API function \"files/search_v2\": max_results out of range")
                if isinstance(arg, dict):
                    for key in PATH_KEYS:
                        value = arg.get(key)
                        if isinstance(value, str) and len(value) > 1 and value.endswith("/"):
                            raise error("path/malformed_path/..")

            def _item(self, path: str) -> Dict[str, Any]:
                item = stub._resolve(path)
                if item is None or (self.prefix and not item["path_lower"].startswith(self.prefix + "/")):
                    raise error("path/not_found/..", {".tag": "path", "path": {".tag": "not_found"}})
                return item

            # -- content endpoints ----------------------------------------------------------------------------
            def _content(self, method: str, arg: Dict[str, Any], raw: bytes):
                if method == "files/upload":
                    if self.headers.get("Content-Type") != "application/octet-stream":
                        raise Refused(400, "Bad HTTP \"Content-Type\" header for an upload.")
                    stub.uploads.append(arg["path"].rsplit("/", 1)[-1])
                    if stub.drop_upload:
                        # The file was received; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    path = arg["path"]
                    existing = stub.items.get(path.lower())
                    if existing and arg["mode"] == "add" and existing.get("_content") != raw:
                        if not arg.get("autorename"):
                            raise error("path/conflict/file/..")
                        stem, dot, ext = path.rpartition(".")
                        n = 1
                        while stub.items.get(f"{stem} ({n}).{ext}".lower()):
                            n += 1
                        path = f"{stem} ({n}).{ext}"
                    folder, name = path.rsplit("/", 1)
                    stub.add_file(folder, name, raw)
                    item = stub.view(stub.items[path.lower()])
                    item.pop(".tag")
                    return self._json(200, item)
                stub.downloads.append(method)
                if method == "sharing/get_shared_link_file":
                    entry = next((r for r in stub.received if r["preview_url"] == arg["url"]), None)
                    if entry is None:
                        raise error("shared_link_not_found/..")
                    return self._bytes(entry["_content"], {".tag": "file", "name": entry["name"],
                                                           "size": entry["size"]})
                item = self._item(arg["path"])
                if item[".tag"] == "folder":
                    raise error("path/not_file/..")
                if method == "files/download":
                    if item.get("is_downloadable") is False:
                        raise error("unsupported_file/..")
                    return self._bytes(item["_content"], stub.view(item))
                if method == "files/get_preview":
                    if not item["name"].lower().endswith(PREVIEWS):
                        raise error("unsupported_extension/..")
                    return self._bytes(b"%PDF-1.7 preview of " + item["name"].encode(), stub.view(item))
                if method == "files/export":
                    exported = b"# " + item["name"].encode() + b"\n"
                    return self._bytes(exported, {"export_metadata": {"name": item["name"] + ".md",
                                                                      "size": len(exported)},
                                                  "file_metadata": stub.view(item)})
                raise Refused(400, f"Unknown API function: \"{method}\"")

            # -- RPC endpoints --------------------------------------------------------------------------------
            def _rpc(self, method: str, arg: Any):
                handler = getattr(self, "rpc_" + method.replace("/", "__"), None)
                if handler is None:
                    raise Refused(400, f"Unknown API function: \"{method}\"")
                return handler(arg or {})

            def rpc_users__get_current_account(self, arg):
                account = {"account_id": stub.ACCOUNT_ID, "email": stub.ACCOUNT, "name": {"display_name": "Demo"},
                           "account_type": {".tag": "business" if stub.team else "basic"},
                           "root_info": ({".tag": "team", "root_namespace_id": stub.ROOT_NS,
                                          "home_namespace_id": stub.HOME_NS, "home_path": stub.home}
                                         if stub.home else
                                         {".tag": "user", "root_namespace_id": stub.HOME_NS,
                                          "home_namespace_id": stub.HOME_NS})}
                if stub.team:
                    account["team"] = stub.team
                self._json(200, account)

            def rpc_users__get_space_usage(self, arg):
                used = sum(i.get("size", 0) for i in stub.items.values())
                self._json(200, {"used": used, "allocation": {".tag": "individual", "allocated": 2 * 1024 ** 4}})

            def rpc_users__get_account(self, arg):
                email = stub.PEOPLE.get(arg["account_id"])
                if email is None:
                    raise error("no_account/..")
                self._json(200, {"account_id": arg["account_id"], "email": email, "name": {"display_name": email}})

            def rpc_files__get_metadata(self, arg):
                if arg["path"] in ("", stub.home):
                    raise Refused(400, "Error in call to API function \"files/get_metadata\": request body: path: "
                                       "The root folder is unsupported.")
                self._json(200, stub.view(self._item(arg["path"])))

            def _folder_key(self, path: str) -> str:
                if path in ("", stub.home):
                    return path.lower()
                item = self._item(path)
                if item[".tag"] != "folder":
                    raise error("path/not_folder/..")
                return item["path_lower"]

            def rpc_files__list_folder(self, arg):
                folder = self._folder_key(arg["path"])
                self._page({"k": "list", "folder": folder, "offset": 0, "limit": int(arg.get("limit") or 2000)})

            def _page(self, cursor):
                rows = stub._children(cursor["folder"])
                start, size = cursor["offset"], cursor["limit"]
                page = rows[start:start + size]
                more = start + size < len(rows)
                nxt = {**cursor, "offset": start + size} if more else \
                    {"k": "changes", "folder": cursor["folder"], "seq": stub.seq, "limit": size}
                self._json(200, {"entries": [stub.view(i) for i in page], "cursor": json.dumps(nxt),
                                 "has_more": more})

            def rpc_files__list_folder__get_latest_cursor(self, arg):
                folder = self._folder_key(arg["path"])
                if arg.get("recursive") is not True:
                    raise Refused(400, "the agent asks for a recursive cursor")
                self._json(200, {"cursor": json.dumps({"k": "changes", "folder": folder, "seq": stub.seq,
                                                       "limit": int(arg.get("limit") or 2000)})})

            def rpc_files__list_folder__continue(self, arg):
                cursor = json.loads(arg["cursor"])
                if cursor["k"] == "list":
                    return self._page(cursor)
                if cursor["seq"] < stub.reset_below:
                    raise error("reset/..", {".tag": "reset"})
                folder = cursor["folder"]
                if stub.busy_folder and folder == stub.busy_folder.lower():
                    return self._json(429, {"error_summary": "too_many_requests/.."}, {"Retry-After": "60"})
                pending = [(seq, e) for seq, e in stub.log if seq > cursor["seq"] and (
                    not folder or e["path_lower"] == folder or e["path_lower"].startswith(folder + "/"))]
                if not pending and folder and folder not in stub.items:
                    raise error("path/not_found/..", {".tag": "path", "path": {".tag": "not_found"}})
                page = pending[:cursor["limit"]]
                more = len(pending) > len(page)
                seq = page[-1][0] if more else stub.seq
                self._json(200, {"entries": [e for _, e in page], "has_more": more,
                                 "cursor": json.dumps({**cursor, "seq": seq})})

            def _visible(self):
                return [i for i in stub.items.values()
                        if not self.prefix or i["path_lower"].startswith(self.prefix + "/")]

            def rpc_files__search_v2(self, arg):
                needle = arg["query"].lower()
                found = [i for i in self._visible() if needle in i["name"].lower()
                         or needle in i.get("_content", b"").decode("utf-8", "replace").lower()]
                self._search(found, 0, arg["options"]["max_results"], arg["query"])

            def _search(self, found, start, size, query):
                page = found[start:start + size]
                more = start + size < len(found)
                self._json(200, {"matches": [{"match_type": {".tag": "filename"},
                                              "metadata": {".tag": "metadata", "metadata": stub.view(i)}}
                                             for i in page],
                                 "has_more": more,
                                 "cursor": json.dumps({"q": query, "offset": start + size, "limit": size})
                                 if more else None})

            def rpc_files__search__continue_v2(self, arg):
                cursor = json.loads(arg["cursor"])
                needle = cursor["q"].lower()
                found = [i for i in self._visible() if needle in i["name"].lower()]
                self._search(found, cursor["offset"], cursor["limit"], cursor["q"])

            def rpc_files__create_folder_v2(self, arg):
                if arg["path"].lower() in stub.items:
                    raise error("path/conflict/folder/..")
                item = stub.view(stub.add_folder(arg["path"]))
                item.pop(".tag")
                self._json(200, {"metadata": item})

            def rpc_files__move_v2(self, arg):
                item = self._item(arg["from_path"])
                to = arg["to_path"]
                if to.lower() in stub.items:
                    raise error("to/conflict/file/..")
                old = item["path_lower"]
                for key in [k for k in list(stub.items) if k == old or k.startswith(old + "/")]:
                    moved = stub.items.pop(key)
                    stub._record({".tag": "deleted", "name": moved["name"], "path_display": moved["path_display"],
                                  "path_lower": key})
                    moved["path_display"] = to + moved["path_display"][len(old):]
                    moved["path_lower"] = moved["path_display"].lower()
                    if key == old:
                        moved["name"] = to.rsplit("/", 1)[-1]
                    stub.items[moved["path_lower"]] = moved
                    stub._record(stub.view(moved))
                self._json(200, {"metadata": stub.view(item)})

            def rpc_files__delete_v2(self, arg):
                item = self._item(arg["path"])
                stub.delete(item["path_display"])
                self._json(200, {"metadata": stub.view(item)})

            def rpc_files__get_temporary_link(self, arg):
                item = self._item(arg["path"])
                if item[".tag"] != "file":
                    raise error("path/not_file/..")
                self._json(200, {"metadata": stub.view(item),
                                 "link": f"https://uc.dl.dropboxusercontent.com/cd/0/get/{item['id'][3:]}"})

            def rpc_files__list_revisions(self, arg):
                if arg.get("mode", "path") != "path" or arg["path"].startswith("id:"):
                    raise Refused(400, "list_revisions in path mode takes a path")
                key = arg["path"].lower()
                history = stub.history.get(key)
                if not history:
                    raise error("path/not_found/..", {".tag": "path", "path": {".tag": "not_found"}})
                live = stub.items.get(key)
                entries = [{k: v for k, v in h.items() if not k.startswith("_") and k != ".tag"}
                           for h in reversed(history)][:int(arg.get("limit") or 10)]
                answer = {"is_deleted": live is None, "entries": entries}
                if live is None:
                    answer["server_deleted"] = "2026-09-02T08:00:00Z"
                self._json(200, answer)

            def rpc_files__restore(self, arg):
                path = arg["path"]
                if not path.startswith("/"):
                    raise Refused(400, "Error in call to API function \"files/restore\": request body: path: "
                                       "must be a path")
                version = next((h for h in stub.history.get(path.lower(), []) if h["rev"] == arg["rev"]), None)
                if version is None:
                    raise error("invalid_revision/..")
                folder, name = version["path_display"].rsplit("/", 1)
                stub.add_file(folder, name, version["_content"])
                restored = stub.view(stub.items[path.lower()])
                restored.pop(".tag")
                self._json(200, restored)

            # -- sharing ------------------------------------------------------------------------------------
            def _members_page(self, users, start, limit, cursor_kind, key):
                size = min(limit, stub.member_page or limit)
                page = users[start:start + size]
                body = {"users": page, "groups": [], "invitees": []}
                if start + size < len(users):
                    body["cursor"] = json.dumps({"k": cursor_kind, "key": key, "offset": start + size,
                                                 "limit": limit})
                self._json(200, body)

            def _file_users(self, item):
                parent_sf = (item.get("sharing_info") or {}).get("parent_shared_folder_id")
                inherited = stub.folder_members.get(parent_sf, []) if parent_sf else []
                return stub._members(stub.file_members.get(item["id"], []), inherited)

            def rpc_sharing__list_file_members(self, arg):
                item = stub._resolve(arg["file"])
                if item is None:
                    raise error("user_error/invalid_file/..")
                self._members_page(self._file_users(item), 0, int(arg.get("limit") or 100), "file", item["id"])

            def rpc_sharing__list_file_members__continue(self, arg):
                cursor = json.loads(arg["cursor"])
                item = stub._resolve(cursor["key"])
                self._members_page(self._file_users(item), cursor["offset"], cursor["limit"], "file", cursor["key"])

            def rpc_sharing__list_folder_members(self, arg):
                if arg["shared_folder_id"] not in stub.folder_members:
                    raise error("access_error/invalid_id/..")
                users = stub._members(stub.folder_members[arg["shared_folder_id"]], [])
                self._members_page(users, 0, int(arg.get("limit") or 1000), "folder", arg["shared_folder_id"])

            def rpc_sharing__list_folder_members__continue(self, arg):
                cursor = json.loads(arg["cursor"])
                users = stub._members(stub.folder_members.get(cursor["key"], []), [])
                self._members_page(users, cursor["offset"], cursor["limit"], "folder", cursor["key"])

            def rpc_sharing__add_file_member(self, arg):
                item = stub._resolve(arg["file"])
                if item is None:
                    raise error("user_error/invalid_file/..")
                access = arg["access_level"][".tag"]
                answers = []
                for member in arg["members"]:
                    if "@" not in member["email"]:
                        answers.append({"member": member, "result": {".tag": "member_error"}})
                        continue
                    stub.file_members.setdefault(item["id"], []).append({"email": member["email"],
                                                                          "access": access})
                    answers.append({"member": member, "result": {".tag": "success"}})
                self._json(200, answers)

            def rpc_sharing__remove_file_member_2(self, arg):
                item = stub._resolve(arg["file"])
                members = stub.file_members.get(item["id"], [])
                kept = [m for m in members if m["email"] != arg["member"].get("email")]
                if len(kept) == len(members):
                    raise error("member_error/no_explicit_access/..")
                stub.file_members[item["id"]] = kept
                self._json(200, {".tag": "success"})

            def rpc_sharing__share_folder(self, arg):
                folder = self._item(arg["path"])
                if folder[".tag"] != "folder":
                    raise error("bad_path/is_file/..")
                sfid = stub._make_shared(folder)
                done = {".tag": "complete", "shared_folder_id": sfid, "name": folder["name"],
                        "path_lower": folder["path_lower"], "access_type": {".tag": "owner"},
                        "is_inside_team_folder": False, "is_team_folder": False, "policy": {},
                        "preview_url": "https://www.dropbox.com/home", "time_invited": "2026-09-01T10:00:00Z"}
                if stub.share_async:
                    return self._json(200, stub._job([{".tag": "in_progress"}, done]))
                self._json(200, done)

            def rpc_sharing__check_share_job_status(self, arg):
                self._poll(arg)

            def rpc_sharing__check_job_status(self, arg):
                self._poll(arg)

            def rpc_sharing__check_remove_member_job_status(self, arg):
                self._poll(arg)

            def _poll(self, arg):
                answers = stub.jobs.get(arg["async_job_id"])
                if answers is None:
                    raise error("invalid_async_job_id/..")
                self._json(200, answers.pop(0) if len(answers) > 1 else answers[0])

            def rpc_sharing__unshare_folder(self, arg):
                sfid = arg["shared_folder_id"]
                if stub.refuse_unshare:
                    raise error("access_error/no_permission/..")
                stub._unshare(sfid)
                self._json(200, stub._job([{".tag": "in_progress"}, {".tag": "complete"}]))

            def rpc_sharing__add_folder_member(self, arg):
                if arg["shared_folder_id"] not in stub.folder_members:
                    raise error("access_error/invalid_id/..")
                if stub.refuse_folder_invites:
                    raise error("bad_member/too_many_members/..")
                for entry in arg["members"]:
                    stub.folder_members[arg["shared_folder_id"]].append(
                        {"email": entry["member"]["email"], "access": entry["access_level"][".tag"]})
                self._json(200, None)

            def rpc_sharing__remove_folder_member(self, arg):
                members = stub.folder_members.get(arg["shared_folder_id"], [])
                stub.folder_members[arg["shared_folder_id"]] = [
                    m for m in members if m["email"] != arg["member"].get("email")]
                self._json(200, stub._job([{".tag": "complete"}]))

            def _received_page(self, start, limit):
                rows = [stub.view(r) for r in stub.received]
                size = min(limit, stub.member_page or limit)
                body = {"entries": rows[start:start + size]}
                if start + size < len(rows):
                    body["cursor"] = json.dumps({"offset": start + size, "limit": limit})
                self._json(200, body)

            def rpc_sharing__list_received_files(self, arg):
                self._received_page(0, int(arg.get("limit") or 100))

            def rpc_sharing__list_received_files__continue(self, arg):
                cursor = json.loads(arg["cursor"])
                self._received_page(cursor["offset"], cursor["limit"])

            def _folders_page(self, start, limit):
                own = [{"name": i["name"], "shared_folder_id": i["sharing_info"]["shared_folder_id"],
                        "path_lower": i["path_lower"], "access_type": {".tag": "owner"},
                        "is_inside_team_folder": False, "is_team_folder": False, "policy": {},
                        "preview_url": "https://www.dropbox.com/home", "time_invited": "2026-01-01T00:00:00Z"}
                       for i in stub.items.values() if (i.get("sharing_info") or {}).get("shared_folder_id")]
                rows = own + stub.shared_folders_with_me
                size = min(limit, stub.member_page or limit)
                body = {"entries": rows[start:start + size]}
                if start + size < len(rows):
                    body["cursor"] = json.dumps({"offset": start + size, "limit": limit})
                self._json(200, body)

            def rpc_sharing__list_folders(self, arg):
                self._folders_page(0, int(arg.get("limit") or 1000))

            def rpc_sharing__list_folders__continue(self, arg):
                cursor = json.loads(arg["cursor"])
                self._folders_page(cursor["offset"], cursor["limit"])

            def rpc_sharing__get_file_metadata(self, arg):
                entry = next((r for r in stub.received if r["id"] == arg["file"]), None)
                if entry is None:
                    raise error("user_error/invalid_file/..")
                view = stub.view(entry)
                view.pop("size")        # Dropbox's shared-file metadata has no size
                self._json(200, view)

            def rpc_sharing__get_shared_link_metadata(self, arg):
                entry = next((r for r in stub.received if r["preview_url"] == arg["url"]), None)
                if entry is None:
                    raise error("shared_link_not_found/..")
                self._json(200, {".tag": "file", "url": entry["preview_url"], "id": entry["id"],
                                 "name": entry["name"], "size": entry["size"]})

            def rpc_sharing__list_shared_links(self, arg):
                item = self._item(arg["path"])
                self._json(200, {"links": [stub._link_view(item, link) for link in stub.links.get(item["id"], [])],
                                 "has_more": False})

            def rpc_sharing__create_shared_link_with_settings(self, arg):
                item = self._item(arg["path"])
                existing = stub.links.get(item["id"])
                if existing:
                    raise error("shared_link_already_exists/metadata/..", {
                        ".tag": "shared_link_already_exists",
                        "shared_link_already_exists": {".tag": "metadata",
                                                       "metadata": stub._link_view(item, existing[0])}})
                audience = arg["settings"]["audience"][".tag"]
                if audience == "team" and not stub.team:
                    raise error("settings_error/not_authorized/..")
                link = {"url": f"https://www.dropbox.com/scl/fi/{item['id'][3:]}/{item['name']}"
                               f"?rlkey=k{len(stub.links)}&dl=0",
                        "audience": audience, "access": arg["settings"]["access"][".tag"]}
                stub.links[item["id"]] = [link]
                self._json(200, stub._link_view(item, link))

            def rpc_sharing__modify_shared_link_settings(self, arg):
                for item_id, links in stub.links.items():
                    for link in links:
                        if link["url"] == arg["url"]:
                            link["audience"] = arg["settings"]["audience"][".tag"]
                            link["access"] = arg["settings"]["access"][".tag"]
                            item = next(i for i in stub.items.values() if i["id"] == item_id)
                            return self._json(200, stub._link_view(item, link))
                raise error("shared_link_not_found/..")

            def rpc_sharing__revoke_shared_link(self, arg):
                for item_id, links in stub.links.items():
                    if any(link["url"] == arg["url"] for link in links):
                        stub.links[item_id] = [li for li in links if li["url"] != arg["url"]]
                        return self._json(200, None)
                raise error("shared_link_not_found/..")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return stands_for("api.dropboxapi.com", self._server)

    def secret(self, access_token: str = "at-1", account: str = "") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": account or self.ACCOUNT, "access_token": access_token, "api_base_url": self.url}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
