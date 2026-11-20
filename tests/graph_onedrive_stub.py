"""A loopback Microsoft Graph for the OneDrive agent: drives, Word files and Excel workbooks, over real HTTP, holding
small fictional drives for Sidra Office Supplies, a work account (Microsoft 365) or a personal one (outlook.com).

It is as strict as Graph where the agent depends on it, and stricter where a rule of Microsoft's is known:

- **Hosts.** Graph answers only at graph.microsoft.com. A download is a redirect (and ``@microsoft.graph.downloadUrl``)
  to another host: SharePoint for a work drive; my.microsoftpersonalcontent.com and *.files.1drv.com for a personal
  one; a PDF conversion goes to *-mediap.svc.ms. Upload sessions live on SharePoint or *.up.1drv.com, copy monitors on
  SharePoint or api.onedrive.com. Each name reaches this one server only through the test's way out, which opens just
  the hosts the agent's manifest declares; and every one of those hosts refuses a request that carries the token.
- **Uploads.** One PUT takes at most 4 MiB here (the agent's own threshold): more is 413. An upload session takes
  fragments in order, each a multiple of 320 KiB but the last, at most 60 MiB, with a Content-Range naming the same
  total every time; a fragment out of order is 416.
- **Versions.** The eTag moves on any change (a rename too); the cTag only when the content is saved. A PUT whose
  If-Match is neither is 412. A document open elsewhere is locked: 423.
- **Throttling.** A test can make the next requests on a path answer 429 (or 503) with Retry-After.
- **Accounts.** A personal account has no organization links (400), no workbook API (400), no copy conflictBehavior
  (400), but restores from the recycle bin, which a work account cannot (400). Its root cannot be shared (403).
- **Invitations.** recipients, roles, requireSignIn, sendInvitation and message are what invite takes; anything else
  on a personal account (expirationDateTime, retainInheritedPermissions) is 400, and requireSignIn and sendInvitation
  both false is 400. ``invite_mail_fails`` stands for the failure the owner met on a personal account: Microsoft would
  not send the invitation ("There was a problem sharing, please try again later."), its trigger unknown. A recipient
  in ``unverified`` comes back in a 207 Multi-Status asking the account to be verified.
- **Shared with me.** ``shared_mode`` plays Microsoft's retirement of sharedWithMe: "full", "one" (degraded to one
  item), "empty" (no data), or "gone" (refused).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from tests.conftest import stands_for

GRAPH = "graph.microsoft.com"
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
WORK_TENANT = "5a1b0c2d-0000-4000-8000-0000000000aa"
EPOCH = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)
SIMPLE_MOST = 4 * 1024 * 1024
FRAGMENT_UNIT = 320 * 1024
FRAGMENT_MOST = 60 * 1024 * 1024
OFFICE = (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".rtf", ".txt", ".csv")
INVITE_KEYS = {"recipients", "roles", "requireSignIn", "sendInvitation", "message", "expirationDateTime", "password",
               "retainInheritedPermissions"}


def jwt(tenant: str) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part({'tid': tenant, 'aud': 'https://graph.microsoft.com'})}.sig"


# -- workbooks -----------------------------------------------------------------------------------------------------

def letters(number: int) -> str:
    out = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(65 + rest) + out
    return out


def number(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + ord(ch) - 64
    return n


def parse(local: str) -> tuple[int, int, int, int]:
    cells = []
    for part in local.split(":"):
        match = re.match(r"^\$?([A-Z]+)\$?(\d+)$", part.strip().upper())
        if not match:
            raise ValueError(local)
        cells.append((int(match.group(2)), number(match.group(1))))
    if len(cells) == 1:
        cells.append(cells[0])
    (ra, ca), (rb, cb) = cells
    return min(ra, rb), min(ca, cb), max(ra, rb), max(ca, cb)


class Stamp:
    """A date-time cell: its value an Excel serial, its text as Excel shows it."""

    def __init__(self, when: datetime):
        self.when = when
        self.serial = (when - datetime(1899, 12, 30)).total_seconds() / 86400
        self.text = f"{when.month}/{when.day}/{when.year} {when.hour}:{when.minute:02d}"


class Formula(str):
    """A cell Excel took for a formula."""


def value_of(cell: Any) -> Any:
    if isinstance(cell, Stamp):
        return cell.serial
    if isinstance(cell, Formula):
        return 0
    return "" if cell is None else cell


def text_of(cell: Any) -> str:
    if isinstance(cell, Stamp):
        return cell.text
    if isinstance(cell, Formula) or cell is None or cell == "":
        return ""
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)


def entered(value: Any) -> Any:
    """A value as Excel takes one written through `values`: "=..." becomes a formula; a leading apostrophe makes
    the rest text."""
    if isinstance(value, str) and value.startswith("'"):
        return value[1:]
    if isinstance(value, str) and value.startswith("="):
        return Formula(value)
    return value


class Workbook:
    def __init__(self):
        self.sheets: dict[str, dict[tuple[int, int], Any]] = {}
        self.tables: dict[str, dict[str, Any]] = {}

    def qualified(self, sheet: str, local: str) -> str:
        name = sheet if re.match(r"^[A-Za-z0-9_]+$", sheet) else "'" + sheet.replace("'", "''") + "'"
        return f"{name}!{local}"

    def block(self, sheet: str, r1: int, c1: int, r2: int, c2: int) -> dict[str, Any]:
        grid = self.sheets[sheet]
        cells = [[grid.get((r, c), "") for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)]
        local = f"{letters(c1)}{r1}" if (r1, c1) == (r2, c2) else f"{letters(c1)}{r1}:{letters(c2)}{r2}"
        return {"address": self.qualified(sheet, local), "rowCount": r2 - r1 + 1, "columnCount": c2 - c1 + 1,
                "values": [[value_of(v) for v in line] for line in cells],
                "formulas": [[str(v) if isinstance(v, Formula) else value_of(v) for v in line] for line in cells],
                "text": [[text_of(v) for v in line] for line in cells]}

    def used(self, sheet: str) -> dict[str, Any]:
        filled = [k for k, v in self.sheets[sheet].items() if v not in ("", None)]
        if not filled:
            return self.block(sheet, 1, 1, 1, 1)
        rows, cols = [r for r, _ in filled], [c for _, c in filled]
        return self.block(sheet, min(rows), min(cols), max(rows), max(cols))

    def table_box(self, name: str) -> dict[str, Any]:
        t = self.tables[name]
        return self.block(t["sheet"], t["header_row"], t["first_col"], t["last_row"], t["last_col"])

    def body(self, name: str) -> list[list[Any]]:
        t = self.tables[name]
        grid = self.sheets[t["sheet"]]
        return [[grid.get((r, c), "") for c in range(t["first_col"], t["last_col"] + 1)]
                for r in range(t["header_row"] + 1, t["last_row"] + 1)]

    def append(self, name: str, lines: list[list[Any]]) -> int:
        t = self.tables[name]
        grid = self.sheets[t["sheet"]]
        body = self.body(name)
        start = t["header_row"] + 1 if len(body) == 1 and all(v in ("", None) for v in body[0]) \
            else t["last_row"] + 1
        for i, line in enumerate(lines):
            for j, value in enumerate(line):
                grid[(start + i, t["first_col"] + j)] = value
        t["last_row"] = start + len(lines) - 1
        return start - t["header_row"] - 1

    def remove(self, name: str, index: int) -> None:
        t = self.tables[name]
        grid = self.sheets[t["sheet"]]
        for r in range(t["header_row"] + 1 + index, t["last_row"] + 1):
            for c in range(t["first_col"], t["last_col"] + 1):
                grid[(r, c)] = grid.get((r + 1, c), "") if r < t["last_row"] else ""
        t["last_row"] = max(t["header_row"] + 1, t["last_row"] - 1)


# -- the stub -------------------------------------------------------------------------------------------------------

class GraphOneDriveStub:
    def __init__(self, personal: bool = False):
        self.personal = personal
        self.account = "demo@outlook.com" if personal else "demo@sidra.example"
        self.display_name = "Demo User"
        self.token = "EwB0AAN-personal-opaque-token" if personal else jwt(WORK_TENANT)
        self.my_drive = "8f3a2c9e1d0b7a65" if personal else "b!demo"
        self.drives: dict[str, dict[str, Any]] = {}
        self.permissions: dict[str, list[dict[str, Any]]] = {}
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.workbooks: dict[str, Workbook] = {}
        self.recycle: dict[str, dict[str, Any]] = {}
        self.shared: list[tuple[str, str]] = []
        self.locked: set[str] = set()
        self.throttles: list[list[Any]] = []
        self.uploads: dict[str, dict[str, Any]] = {}
        self.monitors: dict[str, dict[str, Any]] = {}
        self.wb_sessions: list[dict[str, Any]] = []
        self.session_requests: list[tuple[str, str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self.log: list[dict[str, Any]] = []
        self.shared_mode = "full"
        self.invite_mail_fails = False
        self.unverified: set[str] = set()
        self.drop_put = False
        self.before_put = None
        self.copy_polls = 1
        #: Leave @microsoft.graph.downloadUrl out, so a download goes through /content's redirect.
        self.hide_download_url = False
        #: Seconds to wait before answering a path holding the key: a slow service.
        self.delays: dict[str, float] = {}
        self.clock = 0
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self.root = self._new_drive(self.my_drive, self.account, "personal" if personal else "business")

    # -- names -------------------------------------------------------------------------------------------------
    @property
    def download_host(self) -> str:
        return "my.microsoftpersonalcontent.com" if self.personal else "sidra-my.sharepoint.com"

    @property
    def content_host(self) -> str:
        return "public.bn1302.files.1drv.com" if self.personal else "sidra-my.sharepoint.com"

    @property
    def upload_host(self) -> str:
        return "bn1302.up.1drv.com" if self.personal else "sidra-my.sharepoint.com"

    @property
    def monitor_host(self) -> str:
        return "api.onedrive.com" if self.personal else "sidra.sharepoint.com"

    MEDIA_HOST = "southcentralus1-mediap.svc.ms"

    def at(self, host: str) -> str:
        assert self._server is not None
        return stands_for(host, self._server)

    # -- data --------------------------------------------------------------------------------------------------
    def tick(self) -> str:
        self.clock += 60
        return (EPOCH + timedelta(seconds=self.clock)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def new_id(self, drive: str) -> str:
        self._n += 1
        return f"{drive.upper()}!{100 + self._n}" if self.personal and drive == self.my_drive else f"01DRV{self._n:04d}"

    def _new_drive(self, drive: str, owner: str, kind: str) -> str:
        root = f"{drive.upper()}!100" if kind == "personal" else f"01ROOT{len(self.drives)}"
        self.drives[drive] = {"owner": owner, "type": kind, "root": root, "items": {root: {
            "id": root, "name": "root", "parent": None, "folder": True, "drive": drive, "etag": 1, "ctag": 1,
            "modified": "2026-09-01T09:00:00Z", "by": owner}}}
        self.permissions[root] = [self.owner_permission(owner)]
        return root

    @staticmethod
    def owner_permission(owner: str) -> dict[str, Any]:
        return {"id": "owner", "roles": ["owner"], "grantedToV2": {"user": {"email": owner, "displayName": owner}}}

    def items(self, drive: Optional[str] = None) -> dict[str, dict[str, Any]]:
        return self.drives[drive or self.my_drive]["items"]

    def find(self, drive: Optional[str], item_id: str) -> Optional[dict[str, Any]]:
        if drive is None:
            return self.items().get(item_id)
        return self.drives.get(drive, {}).get("items", {}).get(item_id)

    def child(self, drive: str, parent: str, name: str) -> Optional[dict[str, Any]]:
        return next((i for i in self.items(drive).values()
                     if i.get("parent") == parent and i["name"].lower() == name.lower()), None)

    def stamp(self, drive: str, parent: str, name: str, folder: bool = False, content: bytes = b"",
              by: Optional[str] = None) -> dict[str, Any]:
        item_id = self.new_id(drive)
        owner = self.drives[drive]["owner"]
        item = {"id": item_id, "name": name, "parent": parent, "folder": folder, "drive": drive, "content": content,
                "etag": 1, "ctag": 1, "modified": self.tick(), "by": by or owner}
        self.items(drive)[item_id] = item
        self.permissions[item_id] = [self.owner_permission(owner)]
        self.versions[item_id] = [{"id": "1.0", "lastModifiedDateTime": item["modified"],
                                   "lastModifiedBy": {"user": {"email": item["by"]}}, "size": len(content)}]
        return item

    def add_folder(self, path: str, drive: Optional[str] = None) -> str:
        drive = drive or self.my_drive
        parent = self.drives[drive]["root"]
        for part in [p for p in path.split("/") if p]:
            found = self.child(drive, parent, part)
            parent = (found or self.stamp(drive, parent, part, folder=True))["id"]
        return parent

    def add_file(self, folder: str, name: str, content: bytes, by: Optional[str] = None,
                 drive: Optional[str] = None) -> str:
        drive = drive or self.my_drive
        return self.stamp(drive, self.add_folder(folder, drive), name, content=content, by=by)["id"]

    def add_workbook(self, folder: str, name: str, drive: Optional[str] = None) -> str:
        item_id = self.add_file(folder, name, b"PK\x03\x04 workbook " + name.encode(), drive=drive)
        self.workbooks[item_id] = Workbook()
        return item_id

    def share_with_me(self, name: str, content: bytes, by: str, role: str = "read") -> tuple[str, str]:
        """A file in someone else's drive, shared with the account: (its drive id, its id)."""
        drive = "b!" + by.split("@")[0]
        if drive not in self.drives:
            self._new_drive(drive, by, "business")
        item_id = self.add_file("/", name, content, by=by, drive=drive)
        self.permissions[item_id].append({"id": f"perm-{item_id}", "roles": [role],
                                          "grantedToV2": {"user": {"email": self.account}}})
        self.shared.append((drive, item_id))
        return drive, item_id

    def save_as(self, item_id: str, content: bytes, by: str, drive: Optional[str] = None) -> None:
        """Someone saves new content (in Word, or through the API): both tags move."""
        item = self.find(drive, item_id)
        item["content"], item["etag"], item["ctag"] = content, item["etag"] + 1, item["ctag"] + 1
        item["modified"], item["by"] = self.tick(), by
        self.versions[item_id].insert(0, {"id": f"{item['ctag']}.0", "lastModifiedDateTime": item["modified"],
                                          "lastModifiedBy": {"user": {"email": by}}, "size": len(content)})

    def rename(self, item_id: str, name: str, drive: Optional[str] = None) -> None:
        """A rename moves the eTag only."""
        item = self.find(drive, item_id)
        item["name"], item["etag"] = name, item["etag"] + 1

    def content(self, item_id: str, drive: Optional[str] = None) -> bytes:
        return self.find(drive, item_id)["content"]

    def path_of(self, item: dict[str, Any]) -> str:
        parts, drive = [], item["drive"]
        while item and item.get("parent"):
            parts.append(item["name"])
            item = self.items(drive).get(item["parent"])
        return "/" + "/".join(reversed(parts))

    def etag(self, item: dict[str, Any]) -> str:
        return f'"{{1A2B3C4D-{item["id"][-4:]:0>4}}},{item["etag"]}"'

    def ctag(self, item: dict[str, Any]) -> str:
        return f'"c:{{1A2B3C4D-{item["id"][-4:]:0>4}}},{item["ctag"]}"'

    def view(self, item: dict[str, Any], download: bool = False) -> dict[str, Any]:
        drive = item["drive"]
        out: dict[str, Any] = {
            "id": item["id"], "name": item["name"], "eTag": self.etag(item), "cTag": self.ctag(item),
            "lastModifiedDateTime": item["modified"],
            "lastModifiedBy": {"user": {"email": item["by"], "displayName": item["by"]}},
            "webUrl": f"https://sidra-my.sharepoint.com/personal/{drive}/Documents"
                      + urllib.parse.quote(self.path_of(item))}
        if item.get("parent") is None:
            out["root"] = {}
        if item.get("folder"):
            out["folder"] = {"childCount": sum(1 for i in self.items(drive).values() if i.get("parent") == item["id"])}
            out["size"] = 0
        else:
            out["file"] = {"mimeType": mimetypes.guess_type(item["name"])[0] or "application/octet-stream"}
            out["size"] = len(item["content"])
            if download and not self.hide_download_url:
                out["@microsoft.graph.downloadUrl"] = f"{self.at(self.download_host)}/download/{drive}/{item['id']}"
        parent = item.get("parent")
        prefix = "/drive/root:" if drive == self.my_drive else f"/drives/{drive}/root:"
        out["parentReference"] = {"driveId": drive, "driveType": self.drives[drive]["type"],
                                  **({"id": parent, "path": prefix + ("" if parent == self.drives[drive]["root"]
                                                                      else self.path_of(self.items(drive)[parent]))}
                                     if parent else {})}
        if len(self.permissions.get(item["id"], [])) > 1:
            out["shared"] = {"scope": "users", "owner": {"user": {"email": self.drives[drive]["owner"]}}}
        return out

    def remote(self, drive: str, item_id: str) -> dict[str, Any]:
        item = self.items(drive)[item_id]
        inner = self.view(item)
        inner["shared"] = {"sharedBy": {"user": {"email": self.drives[drive]["owner"],
                                                 "displayName": self.drives[drive]["owner"]}},
                           "sharedDateTime": "2026-09-05T08:00:00Z"}
        return {"id": item_id, "name": item["name"], "remoteItem": inner}

    def throttle(self, pattern: str, times: int = 1, retry_after: str = "1", status: int = 429,
                 method: str = "") -> None:
        """The next ``times`` requests whose path holds ``pattern`` answer ``status`` with Retry-After."""
        self.throttles.append([pattern, times, retry_after, status, method.upper()])

    def table(self, item_id: str) -> Workbook:
        return self.workbooks[item_id]

    def add_table(self, item_id: str, sheet: str, name: str, headers: list[str], rows: list[list[Any]],
                  at: str = "A1") -> None:
        book = self.workbooks[item_id]
        grid = book.sheets.setdefault(sheet, {})
        r0, c0, _, _ = parse(at)
        for i, line in enumerate([headers] + list(rows)):
            for j, value in enumerate(line):
                grid[(r0 + i, c0 + j)] = value
        book.tables[name] = {"sheet": sheet, "header_row": r0, "first_col": c0, "last_col": c0 + len(headers) - 1,
                             "last_row": r0 + max(1, len(rows))}

    def add_sheet(self, item_id: str, sheet: str, rows: list[list[Any]], at: str = "A1") -> None:
        grid = self.workbooks[item_id].sheets.setdefault(sheet, {})
        r0, c0, _, _ = parse(at)
        for i, line in enumerate(rows):
            for j, value in enumerate(line):
                grid[(r0 + i, c0 + j)] = value

    def cell(self, item_id: str, sheet: str, address: str) -> Any:
        r, c, _, _ = parse(address)
        return self.workbooks[item_id].sheets[sheet].get((r, c), "")

    def asked(self, host: str = "", path: str = "", method: str = "") -> list[dict[str, Any]]:
        return [e for e in self.log if (not host or e["host"] == host) and path in e["path"]
                and (not method or e["method"] == method)]

    # -- the server --------------------------------------------------------------------------------------------
    def start(self) -> "GraphOneDriveStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            # -- answers -------------------------------------------------------------------------------------
            def send(self, status: int, body: Any = None, headers: Optional[dict[str, str]] = None) -> None:
                raw = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                if body is not None and not isinstance(body, bytes):
                    self.send_header("Content-Type", "application/json")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def error(self, status: int, code: str, message: str, headers: Optional[dict] = None) -> None:
                self.send(status, {"error": {"code": code, "message": message}}, headers)

            def body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def payload(self) -> dict[str, Any]:
                raw = self.body()
                return json.loads(raw) if raw else {}

            # -- dispatch ------------------------------------------------------------------------------------
            def handle_any(self) -> None:
                url = urllib.parse.urlsplit(self.path)
                path, query = urllib.parse.unquote(url.path), urllib.parse.parse_qs(url.query)
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                stub.log.append({"method": self.command, "host": host, "path": path, "query": query,
                                 "headers": dict(self.headers)})
                for pattern, seconds in stub.delays.items():
                    if pattern in path:
                        time.sleep(seconds)
                if host == GRAPH:
                    if self.headers.get("Authorization") != f"Bearer {stub.token}":
                        self.body()
                        return self.error(401, "InvalidAuthenticationToken", "Access token has expired or is not "
                                                                            "yet valid.")
                    for rule in stub.throttles:
                        pattern, times, after, status, method = rule
                        if times > 0 and pattern in path and (not method or method == self.command):
                            rule[1] -= 1
                            self.body()
                            return self.error(status, "activityLimitReached" if status == 429 else
                                              "serviceNotAvailable", "Too many requests; try again later.",
                                              {"Retry-After": after})
                    if not path.startswith("/v1.0/"):
                        return self.error(400, "BadRequest", "Invalid version.")
                    return self.graph(path[len("/v1.0"):], query)
                # Every host besides Graph's is pre-authenticated: Microsoft refuses the token there.
                if self.headers.get("Authorization"):
                    self.body()
                    return self.error(401, "unauthenticated", "Do not send an Authorization header to a "
                                                              "pre-authenticated address.")
                return self.files(host, path, query)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

            # -- the pre-authenticated hosts -----------------------------------------------------------------
            def files(self, host: str, path: str, query: dict) -> None:
                method = self.command
                match = re.match(r"^/download/([^/]+)/([^/]+)$", path)
                if match and method == "GET" and host in (stub.download_host, stub.content_host):
                    item = stub.find(match.group(1), match.group(2))
                    return self.send(200, item["content"], {"Content-Type": "application/octet-stream"}) if item \
                        else self.error(404, "itemNotFound", "Gone.")
                match = re.match(r"^/transform/pdf/([^/]+)/([^/]+)$", path)
                if match and method == "GET" and host == stub.MEDIA_HOST:
                    item = stub.find(match.group(1), match.group(2))
                    return self.send(200, b"%PDF-1.7\n% converted from " + item["name"].encode("utf-8"))
                match = re.match(r"^/up/([^/]+)$", path)
                if match and host == stub.upload_host:
                    return self.upload_part(match.group(1))
                match = re.match(r"^/monitor/([^/]+)$", path)
                if match and method == "GET" and host == stub.monitor_host:
                    job = stub.monitors.get(match.group(1))
                    if job is None:
                        return self.error(404, "itemNotFound", "No such operation.")
                    if job["polls"] > 0:
                        job["polls"] -= 1
                        return self.send(202, {"status": "inProgress", "percentageComplete": 40.0})
                    return self.send(200, {"status": job["status"], "percentageComplete": 100.0,
                                           **({"resourceId": job["result"]} if job.get("result") else {}),
                                           **({"error": job["error"]} if job.get("error") else {})})
                self.body()
                self.error(404, "BadRequest", f"{host} has no {method} {path}")

            def upload_part(self, session_id: str) -> None:
                session = stub.uploads.get(session_id)
                if session is None:
                    self.body()
                    return self.error(404, "itemNotFound", "The upload session does not exist or has expired.")
                if self.command == "GET":
                    return self.send(200, {"nextExpectedRanges": [f"{len(session['received'])}-"],
                                           "expirationDateTime": "2026-09-02T09:00:00Z"})
                if self.command == "DELETE":
                    stub.uploads.pop(session_id)
                    return self.send(204)
                data = self.body()
                found = re.match(r"^bytes (\d+)-(\d+)/(\d+)$", self.headers.get("Content-Range") or "")
                if not found:
                    return self.error(400, "invalidRange", "Content-Range is required: bytes start-end/total.")
                start, end, total = (int(g) for g in found.groups())
                if session["total"] is None:
                    session["total"] = total
                if total != session["total"] or end - start + 1 != len(data):
                    return self.error(400, "invalidRange", "The Content-Range does not match the bytes or the total.")
                if start != len(session["received"]):
                    return self.error(416, "invalidRange", "The fragment overlaps or skips what was received.",
                                      {"Content-Type": "application/json"})
                last = end + 1 == total
                if len(data) > FRAGMENT_MOST or (not last and len(data) % FRAGMENT_UNIT):
                    return self.error(400, "invalidRange", "A fragment must be a multiple of 320 KiB and under "
                                                           "60 MiB.")
                session["received"] += data
                if not last:
                    return self.send(202, {"nextExpectedRanges": [f"{len(session['received'])}-"],
                                           "expirationDateTime": "2026-09-02T09:00:00Z"})
                stub.uploads.pop(session_id)
                return self.complete_upload(session, bytes(session["received"]))

            def complete_upload(self, session: dict[str, Any], data: bytes) -> None:
                drive = session["drive"]
                if session.get("item"):
                    item = stub.find(drive, session["item"])
                    stub.save_as(item["id"], data, stub.account, drive)
                    return self.send(200, stub.view(item))
                existing = stub.child(drive, session["parent"], session["name"])
                name = session["name"]
                if existing is not None:
                    if session["conflict"] == "replace":
                        stub.save_as(existing["id"], data, stub.account, drive)
                        return self.send(200, stub.view(existing))
                    if session["conflict"] == "fail":
                        return self.error(409, "nameAlreadyExists", "The specified item name already exists.")
                    name = self.free_name(drive, session["parent"], name)
                return self.send(201, stub.view(stub.stamp(drive, session["parent"], name, content=data,
                                                           by=stub.account)))

            @staticmethod
            def free_name(drive: str, parent: str, name: str) -> str:
                stem, dot, ext = name.rpartition(".")
                stem, ext = (stem, ext) if dot else (name, "")
                n = 1
                while stub.child(drive, parent, f"{stem} {n}" + (f".{ext}" if ext else "")) is not None:
                    n += 1
                return f"{stem} {n}" + (f".{ext}" if ext else "")

            # -- Graph ---------------------------------------------------------------------------------------
            def graph(self, path: str, query: dict) -> None:
                method = self.command
                if path == "/me" and method == "GET":
                    return self.send(200, {"id": "u-1", "mail": None if stub.personal else stub.account,
                                           "userPrincipalName": stub.account, "displayName": stub.display_name,
                                           "otherMails": [], "proxyAddresses": []})
                if path == "/me/drive" and method == "GET":
                    used = sum(len(i.get("content") or b"") for i in stub.items().values())
                    return self.send(200, {"id": stub.my_drive, "driveType": stub.drives[stub.my_drive]["type"],
                                           "quota": {"used": used, "total": 5 * 1024 ** 3},
                                           "owner": {"user": {"email": stub.account}}})
                if path == "/me/drive/root" and method == "GET":
                    return self.send(200, stub.view(stub.items()[stub.root]))
                if path.startswith("/me/drive/root:") and method == "GET":
                    item = self.by_path(path[len("/me/drive/root:"):].rstrip(":"))
                    return self.send(200, stub.view(item, True)) if item else self.missing()
                match = re.match(r"^/me/drive/search\(q='(.*)'\)$", path)
                if match and method == "GET":
                    return self.search(match.group(1).replace("''", "'"), query)
                if path == "/me/drive/sharedWithMe" and method == "GET":
                    if stub.shared_mode == "gone":
                        return self.error(400, "invalidRequest", "This API is no longer supported.")
                    found = [stub.remote(d, i) for d, i in stub.shared]
                    found = found[:1] if stub.shared_mode == "one" else [] if stub.shared_mode == "empty" else found
                    return self.send(200, {"value": found})
                match = re.match(r"^/(?:me/drive|drives/([^/]+))/items/([^/:]+)(.*)$", path)
                if not match:
                    self.body()
                    return self.error(400, "BadRequest", f"Resource not found for the segment in {path}.")
                drive, item_id, tail = match.group(1) or stub.my_drive, match.group(2), match.group(3)
                if drive not in stub.drives:
                    self.body()
                    return self.missing()
                if tail.startswith("/workbook"):
                    return self.workbook(drive, item_id, tail[len("/workbook"):], query)
                return self.item(drive, item_id, tail, query)

            def missing(self) -> None:
                self.error(404, "itemNotFound", "The resource could not be found.")

            def by_path(self, path: str) -> Optional[dict[str, Any]]:
                item = stub.items()[stub.root]
                for part in [p for p in path.split("/") if p]:
                    item = stub.child(stub.my_drive, item["id"], part)
                    if item is None:
                        return None
                return item

            def search(self, q: str, query: dict) -> None:
                needle = q.lower()
                hits = [stub.view(i) for i in stub.items().values()
                        if i.get("parent") and (needle == "*" or needle in i["name"].lower()
                                                or needle.encode() in (i.get("content") or b"").lower())]
                hits += [stub.remote(d, i) for d, i in stub.shared
                         if needle == "*" or needle in stub.items(d)[i]["name"].lower()]
                return self.paged(hits, query, f"/v1.0/me/drive/search(q='{urllib.parse.quote(q)}')")

            def paged(self, rows: list, query: dict, path: str) -> None:
                top = int(query.get("$top", ["200"])[0])
                skip = int(query.get("$skiptoken", ["0"])[0])
                answer: dict[str, Any] = {"value": rows[skip: skip + top]}
                if skip + top < len(rows):
                    answer["@odata.nextLink"] = f"{stub.at(GRAPH)}{path}?$top={top}&$skiptoken={skip + top}"
                self.send(200, answer)

            def item(self, drive: str, item_id: str, tail: str, query: dict) -> None:
                method = self.command
                upload = re.match(r"^:/([^/:]+):/(content|createUploadSession)$", tail)
                item = stub.find(drive, item_id)
                if item is None and not (tail == "/restore" and item_id in stub.recycle):
                    self.body()
                    return self.missing()
                if upload:
                    return self.new_file(drive, item, upload.group(1), upload.group(2), query)
                if tail == "" and method == "GET":
                    return self.send(200, stub.view(item, True))
                if tail == "" and method == "PATCH":
                    return self.patch_item(drive, item)
                if tail == "" and method == "DELETE":
                    if item.get("parent") is None:
                        return self.error(403, "accessDenied", "The root cannot be deleted.")
                    stub.recycle[item_id] = stub.items(drive).pop(item_id)
                    return self.send(204)
                if tail == "/children" and method == "GET":
                    rows = sorted((i for i in stub.items(drive).values() if i.get("parent") == item_id),
                                  key=lambda i: i["name"].lower())
                    prefix = f"/v1.0/drives/{drive}" if drive != stub.my_drive else "/v1.0/me/drive"
                    return self.paged([stub.view(i) for i in rows], query, f"{prefix}/items/{item_id}/children")
                if tail == "/children" and method == "POST":
                    body = self.payload()
                    if stub.child(drive, item_id, body.get("name", "")) is not None:
                        return self.error(409, "nameAlreadyExists", "The specified item name already exists.")
                    return self.send(201, stub.view(stub.stamp(drive, item_id, body["name"], folder=True)))
                if tail == "/content" and method == "GET":
                    if query.get("format") == ["pdf"]:
                        if not item["name"].lower().endswith(OFFICE):
                            return self.error(406, "notSupported", "Conversion to pdf is not supported for this "
                                                                   "file type.")
                        location = f"{stub.at(stub.MEDIA_HOST)}/transform/pdf/{drive}/{item_id}"
                    else:
                        location = f"{stub.at(stub.content_host)}/download/{drive}/{item_id}"
                    return self.send(302, None, {"Location": location})
                if tail == "/content" and method == "PUT":
                    return self.replace(drive, item, query)
                if tail == "/createUploadSession" and method == "POST":
                    self.payload()
                    refusal = self.precondition(item)
                    if refusal:
                        return refusal()
                    return self.session(drive, None, item["name"], "replace", item["id"])
                if tail == "/permissions" and method == "GET":
                    return self.send(200, {"value": stub.permissions.get(item_id, [])})
                match = re.match(r"^/permissions/([^/]+)$", tail)
                if match and method == "DELETE":
                    return self.drop_permission(item, match.group(1))
                if tail == "/versions" and method == "GET":
                    return self.send(200, {"value": stub.versions.get(item_id, [])})
                if tail == "/invite" and method == "POST":
                    return self.invite(drive, item)
                if tail == "/createLink" and method == "POST":
                    return self.create_link(drive, item)
                if tail == "/copy" and method == "POST":
                    return self.copy(drive, item, query)
                if tail == "/restore" and method == "POST":
                    self.payload()
                    if not stub.personal:
                        return self.error(400, "notSupported", "Restore is only supported on OneDrive personal.")
                    restored = stub.recycle.pop(item_id, None)
                    if restored is None:
                        return self.missing()
                    stub.items(restored["drive"])[item_id] = restored
                    return self.send(200, stub.view(restored))
                self.body()
                self.error(400, "BadRequest", f"No {method} {tail} on an item.")

            def precondition(self, item: dict[str, Any]):
                wanted = self.headers.get("If-Match")
                if wanted and wanted not in (stub.etag(item), stub.ctag(item)):
                    return lambda: self.error(412, "resourceModified", "ETag does not match current item's value")
                if item["id"] in stub.locked:
                    return lambda: self.error(423, "resourceLocked", "The resource you are attempting to access is "
                                                                     "locked")
                return None

            def new_file(self, drive: str, parent: dict, name: str, how: str, query: dict) -> None:
                if not parent.get("folder"):
                    self.body()
                    return self.error(400, "invalidRequest", "The parent is not a folder.")
                if how == "createUploadSession":
                    body = self.payload()
                    item = body.get("item") or {}
                    if set(item) - {"@microsoft.graph.conflictBehavior", "name", "description", "fileSize"}:
                        return self.error(400, "invalidRequest", "Unknown property in item.")
                    if item.get("name") not in (None, name):
                        return self.error(400, "invalidRequest", "The name in the body must match the path.")
                    return self.session(drive, parent["id"], name, item.get("@microsoft.graph.conflictBehavior",
                                                                            "fail"))
                data = self.body()
                if len(data) > SIMPLE_MOST:
                    return self.error(413, "requestTooLarge", "A simple upload takes at most 4 MiB here; use an "
                                                              "upload session.")
                if stub.drop_put:
                    stub.stamp(drive, parent["id"], name, content=data, by=stub.account)
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                conflict = query.get("@microsoft.graph.conflictBehavior", ["fail"])[0]
                return self.complete_upload({"drive": drive, "parent": parent["id"], "name": name, "conflict": conflict}, data)

            def session(self, drive: str, parent: Optional[str], name: str, conflict: str,
                        item: Optional[str] = None) -> None:
                stub._n += 1
                session_id = f"s{stub._n}"
                stub.uploads[session_id] = {"drive": drive, "parent": parent, "name": name, "conflict": conflict,
                                            "item": item, "total": None, "received": bytearray()}
                return self.send(200, {"uploadUrl": f"{stub.at(stub.upload_host)}/up/{session_id}",
                                       "expirationDateTime": "2026-09-02T09:00:00Z"})

            def replace(self, drive: str, item: dict[str, Any], query: dict) -> None:
                data = self.body()
                if stub.before_put is not None:
                    stub.before_put(item["id"])
                refusal = self.precondition(item)
                if refusal:
                    return refusal()
                if len(data) > SIMPLE_MOST:
                    return self.error(413, "requestTooLarge", "Use an upload session over 4 MiB.")
                stub.save_as(item["id"], data, stub.account, drive)
                if stub.drop_put:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self.send(200, stub.view(item))

            def patch_item(self, drive: str, item: dict[str, Any]) -> None:
                changes = self.payload()
                reference = changes.get("parentReference") or {}
                if reference.get("driveId") not in (None, drive):
                    return self.error(400, "invalidRequest", "Cross-drive moves are not supported; copy instead.")
                parent = reference.get("id") or item["parent"]
                name = changes.get("name") or item["name"]
                if parent not in stub.items(drive):
                    return self.missing()
                clash = stub.child(drive, parent, name)
                if clash is not None and clash["id"] != item["id"]:
                    return self.error(409, "nameAlreadyExists", "The specified item name already exists.")
                item["parent"], item["name"], item["etag"] = parent, name, item["etag"] + 1
                self.send(200, stub.view(item))

            def copy(self, drive: str, item: dict[str, Any], query: dict) -> None:
                body = self.payload()
                if stub.personal and "@microsoft.graph.conflictBehavior" in query:
                    return self.error(400, "invalidRequest", "conflictBehavior is not supported for OneDrive "
                                                             "Consumer.")
                reference = body.get("parentReference") or {}
                target_drive, target = reference.get("driveId"), reference.get("id")
                if not target_drive or target_drive not in stub.drives or target not in stub.items(target_drive):
                    return self.error(400, "invalidRequest", "parentReference needs the driveId and id of a folder.")
                name = body.get("name") or item["name"]
                stub._n += 1
                job_id = f"m{stub._n}"
                if stub.child(target_drive, target, name) is not None:
                    conflict = query.get("@microsoft.graph.conflictBehavior", ["fail"])[0]
                    if conflict == "fail":
                        stub.monitors[job_id] = {"polls": 0, "status": "failed",
                                                 "error": {"code": "nameAlreadyExists", "message": "Name already "
                                                                                                   "exists"}}
                        return self.send(202, None, {"Location": f"{stub.at(stub.monitor_host)}/monitor/{job_id}"})
                    name = self.free_name(target_drive, target, name)
                made = stub.stamp(target_drive, target, name, folder=bool(item.get("folder")),
                                  content=item.get("content") or b"", by=stub.account)
                stub.monitors[job_id] = {"polls": stub.copy_polls, "status": "completed", "result": made["id"]}
                self.send(202, None, {"Location": f"{stub.at(stub.monitor_host)}/monitor/{job_id}"})

            def invite(self, drive: str, item: dict[str, Any]) -> None:
                body = self.payload()
                unknown = set(body) - INVITE_KEYS
                if unknown:
                    return self.error(400, "invalidRequest", f"Unknown properties: {sorted(unknown)}")
                if item.get("parent") is None and stub.drives[drive]["type"] == "personal":
                    return self.error(403, "notAllowed", "Permissions can't be added to the root of a personal "
                                                         "drive.")
                if body.get("requireSignIn") is False and body.get("sendInvitation") is False:
                    return self.error(400, "invalidRequest", "RequireSignIn and SendInvitation cannot both be false")
                if stub.personal and ({"expirationDateTime", "retainInheritedPermissions"} & set(body)):
                    return self.error(400, "invalidRequest", "That property is not supported on this drive.")
                if len(str(body.get("message") or "")) > 2000:
                    return self.error(400, "invalidRequest", "The message is longer than 2,000 characters.")
                recipients = [r.get("email") for r in body.get("recipients") or []]
                roles = body.get("roles") or []
                if not recipients or not all(recipients) or not roles or set(roles) - {"read", "write"}:
                    return self.error(400, "invalidRequest", "recipients (each with an email) and roles (read or "
                                                             "write) are required.")
                if stub.personal and stub.invite_mail_fails and body.get("sendInvitation") is not False:
                    return self.error(500, "generalException", "There was a problem sharing, please try again "
                                                               "later.")
                granted, partial = [], False
                for email in recipients:
                    stub._n += 1
                    permission = {"id": f"perm-{stub._n}", "roles": roles,
                                  "invitation": {"email": email, "signInRequired": True},
                                  "grantedToV2": {"user": {"displayName": email,
                                                           **({} if stub.personal else {"email": email})}}}
                    if stub.personal:
                        permission["link"] = {"type": roles[0] if roles[0] == "write" else "view",
                                              "webUrl": f"https://1drv.ms/w/s!{stub._n}"}
                    stub.permissions[item["id"]].append(permission)
                    shown = dict(permission)
                    if email in stub.unverified and body.get("sendInvitation"):
                        partial = True
                        shown["error"] = {"code": "notAllowed",
                                          "message": "Account verification needed to unblock sending emails.",
                                          "fixItUrl": "http://g.live.com/8SESkydrive/VerifyAccount",
                                          "innererror": {"code": "accountVerificationRequired"}}
                    granted.append(shown)
                self.send(207 if partial else 200, {"value": granted})

            def create_link(self, drive: str, item: dict[str, Any]) -> None:
                body = self.payload()
                kind, scope = body.get("type"), body.get("scope")
                if kind not in ("view", "edit", "embed"):
                    return self.error(400, "invalidRequest", "type must be view, edit or embed.")
                if stub.drives[drive]["type"] == "personal":
                    if scope == "organization":
                        return self.error(400, "invalidRequest", "The organization scope is not supported for this "
                                                                 "drive.")
                    if item.get("parent") is None:
                        return self.error(403, "notAllowed", "The root of a personal drive cannot be shared.")
                    scope = scope or "anonymous"
                elif kind == "embed":
                    return self.error(400, "invalidRequest", "Embed links exist only on OneDrive personal.")
                scope = scope or "organization"
                for permission in stub.permissions[item["id"]]:
                    link = permission.get("link") or {}
                    if link.get("type") == kind and link.get("scope") == scope:
                        return self.send(200, permission)
                stub._n += 1
                permission = {"id": f"perm-{stub._n}", "roles": ["write" if kind == "edit" else "read"],
                              "link": {"type": kind, "scope": scope,
                                       "webUrl": f"https://sidra-my.sharepoint.com/:w:/g/{stub._n}"}}
                stub.permissions[item["id"]].append(permission)
                self.send(201, permission)

            def drop_permission(self, item: dict[str, Any], permission_id: str) -> None:
                granted = stub.permissions.get(item["id"], [])
                permission = next((p for p in granted if p["id"] == permission_id), None)
                if permission is None:
                    return self.missing()
                if "owner" in permission["roles"] or permission.get("inheritedFrom"):
                    return self.error(403, "accessDenied", "That permission cannot be removed here.")
                granted.remove(permission)
                self.send(204)

            # -- workbooks -----------------------------------------------------------------------------------
            def workbook(self, drive: str, item_id: str, inner: str, query: dict) -> None:
                method = self.command
                session = self.headers.get("workbook-session-id") or ""
                if session:
                    stub.session_requests.append((method, inner, session))
                payload = self.payload() if method in ("POST", "PATCH") else {}
                if stub.drives[drive]["type"] == "personal":
                    return self.error(400, "notSupported", "The workbook API is not supported for OneDrive "
                                                           "Consumer.")
                if stub.find(drive, item_id) is None:
                    return self.missing()
                book = stub.workbooks.get(item_id)
                if book is None:
                    return self.error(400, "invalidRequest", "The file is not a workbook.")
                if inner == "/createSession" and method == "POST":
                    stub.wb_sessions.append({"id": f"session-{len(stub.wb_sessions) + 1}",
                                             "persist": bool(payload.get("persistChanges")), "closed": False})
                    return self.send(201, {"id": stub.wb_sessions[-1]["id"], "persistChanges": True})
                if inner == "/closeSession" and method == "POST":
                    for found in stub.wb_sessions:
                        found["closed"] = found["closed"] or found["id"] == session
                    return self.send(204)
                if inner == "/worksheets" and method == "GET":
                    return self.send(200, {"value": [{"id": f"{{{i:08d}}}", "name": n, "position": i,
                                                      "visibility": "Visible"} for i, n in enumerate(book.sheets)]})
                if inner == "/tables" and method == "GET":
                    return self.send(200, {"value": [{"id": str(i + 1), "name": n, "showHeaders": True,
                                                      "showTotals": False} for i, n in enumerate(book.tables)]})
                match = re.match(r"^/worksheets/([^/]+)/usedRange(\(valuesOnly=true\))?$", inner)
                if match and method == "GET":
                    return self.send(200, book.used(match.group(1))) if match.group(1) in book.sheets \
                        else self.missing()
                match = re.match(r"^/worksheets/([^/]+)/range\(address='([^']*)'\)$", inner)
                if match:
                    sheet, local = match.groups()
                    if sheet not in book.sheets:
                        return self.missing()
                    try:
                        r1, c1, r2, c2 = parse(local)
                    except ValueError:
                        return self.error(400, "InvalidArgument", "The address is not valid.")
                    if method == "GET":
                        return self.send(200, book.block(sheet, r1, c1, r2, c2))
                    if method == "PATCH":
                        values = payload.get("values") or []
                        if len(values) != r2 - r1 + 1 or any(len(v) != c2 - c1 + 1 for v in values):
                            return self.error(400, "InvalidArgument", "The number of rows or columns in the input "
                                                                      "array doesn't match the range.")
                        for i, line in enumerate(values):
                            for j, value in enumerate(line):
                                if value is not None:
                                    book.sheets[sheet][(r1 + i, c1 + j)] = entered(value)
                        stub.writes.append(("range", f"{sheet}!{local}"))
                        return self.send(200, book.block(sheet, r1, c1, r2, c2))
                match = re.match(r"^/tables/([^/]+)(/range|/rows/add)?$", inner)
                if match:
                    name, tail = match.groups()
                    if name not in book.tables:
                        return self.missing()
                    if tail == "/range" and method == "GET":
                        return self.send(200, book.table_box(name))
                    if not tail and method == "GET":
                        return self.send(200, {"id": name, "name": name, "showHeaders": True, "showTotals": False})
                    if tail == "/rows/add" and method == "POST":
                        t = book.tables[name]
                        width = t["last_col"] - t["first_col"] + 1
                        if any(len(line) != width for line in payload.get("values") or []):
                            return self.error(400, "InvalidArgument", "The number of columns does not match the "
                                                                      "table.")
                        stub.writes.append(("rows/add", name))
                        index = book.append(name, [[entered(v) for v in line] for line in payload["values"]])
                        return self.send(201, {"index": index, "values": payload["values"]})
                self.error(400, "BadRequest", f"No {method} {inner} in a workbook.")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        for host in (GRAPH, self.download_host, self.content_host, self.upload_host, self.monitor_host,
                     self.MEDIA_HOST):
            self.at(host)
        return self

    def secret(self, access_token: Optional[str] = None, account: Optional[str] = None,
               ref: Optional[str] = None) -> dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {**({"ref": ref} if ref else {}), "account": account or self.account,
                "access_token": self.token if access_token is None else access_token,
                "api_base_url": self.at(GRAPH) + "/v1.0"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
