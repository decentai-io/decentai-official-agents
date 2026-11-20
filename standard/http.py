"""One way to speak HTTP to a service's API, for every agent that connects an account.

- **Off the agent's loop.** Each request runs in a thread (``asyncio.to_thread``), so a slow download never stalls
  the chat's other calls, and a cancel ends the wait at once.
- **Reads are tried again** on a dropped connection, a timeout, 429, 500, 502, 503 and 504, after the wait the
  service names in ``Retry-After`` (or a growing one), within ``wait_most`` seconds in all.
- **Writes are tried again only when the service said it did nothing:** 429 and 503, and a connection that was never
  made. A write that went out and got no answer is ``unknown``, and nothing tries it again.
- **One set of failures,** each with a kind the agent passes on: ``auth`` (reconnect the account), ``scope`` (the
  account was connected without a permission this needs: reconnect it), ``denied``, ``not_found``, ``conflict``
  (the thing changed since it was read), ``locked``, ``too_large``, ``rate`` (the service is busy: try later),
  ``invalid`` (it refused what was asked, its words say why), ``http`` (anything else), ``unknown``.
- **A page token is the service's own next link,** checked to point at the service before the token is sent there.
- **A download is held to a size,** and refused as ``too_large`` before its bytes are read when the service says
  its size.
"""

from __future__ import annotations

import asyncio
import email.utils
import random
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

import requests

from .errors import HttpError
from .net import BaseAddress

#: What a chat file may be at most: 8 MiB less 64 KiB (agents.md §9).
FILE_MOST = 8 * 1024 * 1024 - 64 * 1024

WRITES = {"POST", "PUT", "PATCH", "DELETE"}
RETRIED = {429, 500, 502, 503, 504}


def segment(value: Any) -> str:
    """An id as one segment of a path: one holding "/", ".." or "?" names nothing else."""
    return quote(str(value), safe="")


def human_size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} bytes"


class Http:
    """A client for one service. Subclasses name the service and its base address, and may read its own error
    bodies (``detail``), tell its own rate limits apart (``busy``), and add headers (``headers``)."""

    SERVICE = "The service"
    BASE = ""
    READ_TIMEOUT = 40
    WRITE_TIMEOUT = 60
    #: How long, in all, a call may spend waiting to try again.
    WAIT_MOST = 30.0
    ATTEMPTS = 4

    def __init__(self, token: str = "", base: str = "", api_base_url: Any = None):
        self.token = str(token or "")
        self.base = (BaseAddress.given(api_base_url) or base or self.BASE).rstrip("/")
        self.session = requests.Session()

    # -- what a subclass may change ------------------------------------------------------------------------------
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {}

    def auth(self) -> dict[str, str]:
        if not self.token:
            raise HttpError("auth", f"No {self.SERVICE} account is connected: connect it from the agent's "
                                    f"Credentials.")
        return {"Authorization": f"Bearer {self.token}"}

    def detail(self, response: requests.Response) -> str:
        """The service's own words for a refusal."""
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:400] or response.reason or str(response.status_code)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)[:400]
        if isinstance(body, dict):
            for key in ("error_description", "message", "error_summary", "error"):
                if body.get(key):
                    return str(body[key])[:400]
        return str(body)[:400]

    def busy(self, response: requests.Response) -> bool:
        """Whether a refusal means "too many requests, later" (Google says so with 403)."""
        return response.status_code in (429, 503)

    def scope_refused(self, response: requests.Response, words: str) -> bool:
        lowered = words.lower()
        return response.status_code == 403 and any(w in lowered for w in (
            "scope", "insufficient", "permission", "not authorized", "access is denied", "accessdenied"))

    # -- one request ---------------------------------------------------------------------------------------------
    def url(self, path: str, absolute: str = "") -> str:
        if absolute:
            if not (absolute.startswith(self.base + "/") or absolute.startswith(self.base + "?")):
                raise HttpError("invalid", f"That page token is not one {self.SERVICE} gave: ask again from the "
                                           f"first page.")
            return absolute
        return path if path.startswith(("https://", "http://")) else f"{self.base}/{path.lstrip('/')}"

    async def request(self, method: str, path: str = "", *, params: Any = None, json: Any = None, data: Any = None,
                      headers: dict[str, str] | None = None, absolute: str = "", write: bool | None = None,
                      timeout: float | None = None, raw: bool = False, authorized: bool = True,
                      allow: Iterable[int] = ()) -> Any:
        """The answer's JSON (``{}`` when it has none), or the response itself with ``raw``. A status in ``allow``
        is returned rather than refused."""
        method = method.upper()
        write = method in WRITES if write is None else write
        url = self.url(path, absolute)
        timeout = timeout or (self.WRITE_TIMEOUT if write else self.READ_TIMEOUT)
        waited, attempt, said = 0.0, 0, path or url
        while True:
            attempt += 1
            sent = {**(self.auth() if authorized else {}), **self.headers(method, path), **(headers or {})}
            try:
                response = await asyncio.to_thread(self.session.request, method, url, params=params, json=json,
                                                   data=data, headers=sent, timeout=timeout,
                                                   allow_redirects=not write)
            except (requests.ConnectionError, requests.Timeout) as failed:
                never_sent = isinstance(failed, requests.ConnectTimeout) or "NewConnectionError" in repr(failed)
                if write and not never_sent:
                    raise HttpError("unknown", f"No answer from {self.SERVICE} for {method} {said}: whether it was "
                                               f"done is unknown, and it was not sent again ({failed}).") from None
                pause = self.pause(None, attempt)
                if attempt >= self.ATTEMPTS or waited + pause > self.WAIT_MOST:
                    raise HttpError("http", f"{self.SERVICE} could not be reached: {failed}") from None
                waited += pause
                await asyncio.sleep(pause)
                continue
            except requests.RequestException as failed:
                raise HttpError("http", f"{self.SERVICE} could not be asked: {failed}") from None
            status = response.status_code
            if status in allow or status < 400:
                if raw:
                    return response
                if status in (202, 204) or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    raise HttpError("http", f"{self.SERVICE} answered {method} {said} with something that is not "
                                            f"JSON.", status) from None
            words = self.detail(response)
            if write and status in (502, 504):
                raise HttpError("unknown", f"{self.SERVICE}'s gateway gave up on {method} {said} ({status}): whether "
                                           f"it was done is unknown, and it was not sent again.", status)
            # A busy service did nothing, so even a write may go again; any other 5xx is tried again for a read only.
            if self.busy(response) or (status in RETRIED and not write):
                pause, later = self.pause(response, attempt), self.retry_after(response)
                if attempt < self.ATTEMPTS and waited + max(pause, later) <= self.WAIT_MOST:
                    waited += pause
                    await asyncio.sleep(pause)
                    continue
                raise HttpError("rate", f"{self.SERVICE} is busy and asked to wait"
                                        f"{f' {int(later)} seconds' if later else ''}: try again later. ({words})",
                                status)
            raise self.refusal(response, method, said, words)

    def refusal(self, response: requests.Response, method: str, said: str, words: str) -> HttpError:
        status = response.status_code
        if status == 401:
            return HttpError("auth", f"The {self.SERVICE} connection has expired or was revoked: reconnect the "
                                     f"account from the agent's Credentials. ({words})", status)
        if self.scope_refused(response, words):
            return HttpError("scope", f"{self.SERVICE} refused {method} {said}: {words}. The account may have been "
                                      f"connected without the permission this needs: reconnect it from the agent's "
                                      f"Credentials.", status)
        kind = {403: "denied", 404: "not_found", 409: "conflict", 412: "conflict", 413: "too_large",
                423: "locked", 400: "invalid", 422: "invalid"}.get(status, "http")
        return HttpError(kind, f"{self.SERVICE} refused {method} {said}: {words}", status,
                         _json_or_none(response))

    @classmethod
    def retry_after(cls, response: requests.Response | None) -> float:
        value = (response.headers.get("Retry-After") or "").strip() if response is not None else ""
        if not value:
            return 0.0
        try:
            return max(0.0, float(value))
        except ValueError:
            when = email.utils.parsedate_to_datetime(value) if value else None
            return max(0.0, when.timestamp() - time.time()) if when else 0.0

    @classmethod
    def pause(cls, response: requests.Response | None, attempt: int) -> float:
        named = cls.retry_after(response)
        return named if named else min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25)

    # -- shorthands ----------------------------------------------------------------------------------------------
    async def get(self, path: str = "", **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def patch(self, path: str, **kw: Any) -> Any:
        return await self.request("PATCH", path, **kw)

    async def put(self, path: str, **kw: Any) -> Any:
        return await self.request("PUT", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def pages(self, path: str, *, params: Any = None, items: str = "value",
                    next_link: Callable[[dict], str] | None = None, most: int = 0, **kw: Any) -> list[Any]:
        """Every page's ``items`` until there is no next page, or ``most`` are held. ``next_link`` reads the next
        page's address from an answer; by default Microsoft's ``@odata.nextLink``."""
        found: list[Any] = []
        answer = await self.get(path, params=params, **kw)
        while True:
            found.extend(answer.get(items) or [])
            following = (next_link or (lambda a: str(a.get("@odata.nextLink") or "")))(answer)
            if not following or (most and len(found) >= most):
                return found[:most] if most else found
            answer = await self.get("", absolute=following, **kw)

    async def download(self, path: str = "", *, absolute: str = "", most: int = FILE_MOST, params: Any = None,
                       headers: dict[str, str] | None = None, authorized: bool = True,
                       what: str = "The file") -> bytes:
        """A file's bytes, at most ``most``: refused as too_large before reading when the service says its size."""
        url = absolute if absolute.startswith(("https://", "http://")) else self.url(path)

        def fetch() -> bytes:
            sent = {**(self.auth() if authorized else {}), **(headers or {})}
            with self.session.get(url, params=params, headers=sent, timeout=self.READ_TIMEOUT * 3,
                                  stream=True) as response:
                if response.status_code >= 400:
                    raise self.refusal(response, "GET", path or "the file", self.detail(response))
                told = int(response.headers.get("Content-Length") or 0)
                if told > most:
                    raise HttpError("too_large", f"{what} is {human_size(told)}; at most {human_size(most)} can be "
                                                 f"brought into the chat.")
                held = bytearray()
                for piece in response.iter_content(64 * 1024):
                    held += piece
                    if len(held) > most:
                        raise HttpError("too_large", f"{what} is more than {human_size(most)}, the most that can "
                                                     f"be brought into the chat.")
                return bytes(held)

        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(fetch)
            except (requests.ConnectionError, requests.Timeout) as failed:
                if attempt == self.ATTEMPTS:
                    raise HttpError("http", f"{self.SERVICE} could not be reached: {failed}") from None
                await asyncio.sleep(self.pause(None, attempt))
            except HttpError as refused:
                if refused.kind != "rate" and refused.status not in RETRIED or attempt == self.ATTEMPTS:
                    raise
                await asyncio.sleep(self.pause(None, attempt))
        raise HttpError("http", f"{self.SERVICE} could not be reached.")

    def close(self) -> None:
        self.session.close()


def _json_or_none(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
