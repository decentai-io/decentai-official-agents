"""Fetching a public URL for the assistant — and only a public one.

An assistant that can be told "read this link" is a server-side request
forgery waiting to happen: the link can name the cloud's metadata
service, a database on the private network, or the platform itself.
So every fetch here, including every hop of a redirect:

- accepts only http and https, with no user name or password in it;
- resolves the host and refuses an address that is loopback, private,
  link-local, multicast, reserved, unspecified or otherwise not public
  (IPv4, IPv6, and IPv4 dressed as IPv6), saying which;
- checks the address the socket actually connected to as well, so a
  name that resolves to a public address for the check and a private
  one for the connection (DNS rebinding) is still refused;
- follows at most five redirects, by hand, re-checking each;
- stops reading past a size cap (a chat file's most), and gives up on a
  slow server; a PDF larger than the cap is refused, before its bytes
  are read when the server says its size, since half a PDF is no PDF;
- ignores proxy settings from the environment, because a proxy would
  make the connected address the proxy's rather than the target's —
  all but the platform's own way out (see below);
- says who it is: a plain User-Agent naming DecentAI.

A page that answers with a sign-in (401) or a bot check (a challenge
page, often 403 or 503) fails with ``needs_browser``: a real browser,
driven by a person or the Browser agent, may get through where a plain
fetch cannot.

The body's text is decoded the way a browser would: a byte-order mark
first, then the charset the server names, then the one the page
declares (``<meta charset>``, ``http-equiv``, an XML declaration), then
UTF-8, then a guess — never failing the page on a stray byte.

``fetch()`` runs a fetch in a thread, off the agent's loop: one slow
site never stalls the chat's other calls.

TEST-ONLY: ``DECENTAI_WEB_ALLOW_LOOPBACK=1`` in the environment permits
loopback addresses (127.0.0.0/8 and ::1) and nothing else — never a
private range. It exists so the tests can serve pages from a loopback
stub; a deployment must never set it.

BEHIND THE PLATFORM'S WAY OUT. In the sandbox a program cannot look a
name up or open a connection of its own: its one way out is a proxy
named in ``HTTPS_PROXY``. The way out makes the same checks this file
makes — every address a name resolves to, and then a connection to the
address it checked — so a fetch leaves the name to it, through a
tunnel it opens (HTTP CONNECT) for http:// as for https://: a refusal
is then always the way out's answer, never a site's. What this file
still does itself: the scheme, the credentials, an address written as
an address, and every hop of a redirect, each of which is a tunnel the
way out checks again.

This file is the same in every web agent of this repository: agents
cannot import one another, so each carries its own copy, and a test
fails when the copies differ.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import os
import re
import socket
import time
import urllib.parse
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from standard import Tunnel, TunnelRefused
from standard.http import FILE_MOST, human_size
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

LOOPBACK_VARIABLE = "DECENTAI_WEB_ALLOW_LOOPBACK"
#: The sandbox's way out, where there is one.
PROXY_VARIABLE = "HTTPS_PROXY"

#: What an error page is read for, to tell a bot check from a plain refusal.
ERROR_BODY_MOST = 256 * 1024


class FetchError(Exception):
    """A fetch that did not produce a page, with a kind the assistant can
    act on: invalid_url, forbidden, too_many_redirects, timeout,
    network, http, too_large, blocked, sign_in."""

    def __init__(self, kind: str, message: str, status: int = 0, needs_browser: bool = False,
                 size: int = 0, limit: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.needs_browser = needs_browser
        self.size = size
        self.limit = limit

    def result(self) -> dict:
        out = {"error": str(self), "kind": self.kind}
        if self.status:
            out["status"] = self.status
        if self.needs_browser:
            out["needs_browser"] = True
        if self.size:
            out["size"] = self.size
        if self.limit:
            out["limit"] = self.limit
        return out


class ForbiddenAddress(FetchError):
    def __init__(self, message: str):
        super().__init__("forbidden", message)


class AddressPolicy:
    """Which addresses a fetch may reach: public ones."""

    @staticmethod
    def refusal(address: str) -> str:
        """Why this address may not be fetched, or '' when it may."""
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return f"{address} is not an IP address"
        # ::ffff:10.0.0.1 is 10.0.0.1. Older Pythons do not look inside,
        # so unwrap it before asking anything.
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            if os.environ.get(LOOPBACK_VARIABLE) == "1":
                return ""
            return f"{ip} is a loopback address (this machine)"
        if ip.is_unspecified:
            return f"{ip} is the unspecified address"
        if ip.is_link_local:
            return f"{ip} is a link-local address (such as a cloud metadata service)"
        if ip.is_multicast:
            return f"{ip} is a multicast address"
        if ip.is_reserved:
            return f"{ip} is a reserved address"
        if ip.is_private:
            return f"{ip} is a private network address"
        if not ip.is_global:
            return f"{ip} is not a public internet address"
        return ""


class _CheckedHTTPConnection(HTTPConnection):
    """A tunnel through the way out where there is one. Without it, the
    peer is checked after connecting: the name was checked when it was
    resolved, but it is resolved again to connect, and the two answers
    need not agree."""

    def _new_conn(self):
        return _connected(self, super()._new_conn)


class _CheckedHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        return _connected(self, super()._new_conn)


def _connected(connection, straight):
    """The connection's socket: a tunnel through the way out, or straight, its peer checked."""
    if not os.environ.get(PROXY_VARIABLE):
        sock = straight()
        _check_peer(sock)
        return sock
    host = connection.host
    try:
        return Tunnel.open(host, connection.port, connection.timeout)
    except TunnelRefused as refused:
        why = str(refused.reason or "").strip().rstrip(".") or "the way out refused it"
        if refused.status == 403:
            raise ForbiddenAddress(f"Refused to fetch {host}: {why}.")
        raise FetchError("network", f"{host} could not be reached: {why}.")
    except TimeoutError:
        raise FetchError("timeout", f"{host} did not answer in time.")
    except OSError as exc:
        raise FetchError("network", f"{host} could not be reached through the way out: {exc}")


def _check_peer(sock) -> None:
    try:
        peer = sock.getpeername()[0]
    except OSError:
        return
    why = AddressPolicy.refusal(str(peer))
    if why:
        sock.close()
        raise ForbiddenAddress(f"Refused: the connection reached {why}.")


class _CheckedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _CheckedHTTPConnection


class _CheckedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _CheckedHTTPSConnection


class _CheckedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _CheckedHTTPPool, "https": _CheckedHTTPSPool}


# -- text: which charset ---------------------------------------------------

BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
#: What a browser reads these labels as (the WHATWG Encoding standard).
AS_WINDOWS_1252 = {"iso-8859-1", "iso8859-1", "iso_8859-1", "latin1", "latin-1", "l1", "ascii",
                   "us-ascii", "cp819", "ibm819", "windows-1252", "cp1252", "x-cp1252"}
META_CHARSET = re.compile(rb"<meta[^>]{0,400}?charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-:.]+)", re.I)
XML_ENCODING = re.compile(rb"^\s*<\?xml[^>]{0,200}?encoding\s*=\s*[\"']([A-Za-z0-9_\-.]+)", re.I)


def charset_named(label: str) -> str:
    """A charset label as a codec Python knows, or '' for one it does not."""
    label = str(label or "").strip().strip("\"'").lower()
    if not label:
        return ""
    if label in AS_WINDOWS_1252:
        return "cp1252"
    try:
        return codecs.lookup(label).name
    except LookupError:
        return ""


def bom_of(body: bytes) -> str:
    for mark, name in BOMS:
        if body.startswith(mark):
            return name
    return ""


def declared_charset(body: bytes) -> str:
    """The charset a document declares in its first bytes: an XML
    declaration's encoding, or an HTML ``<meta>``'s."""
    head = body[:4096]
    found = XML_ENCODING.search(head) or META_CHARSET.search(head)
    if not found:
        return ""
    name = charset_named(found.group(1).decode("ascii", "replace"))
    # A declaration readable as ASCII cannot be UTF-16 (the HTML rule).
    return "utf-8" if name.startswith("utf-16") or name.startswith("utf_16") else name


def decoded(body: bytes, header_charset: str = "") -> tuple:
    """(text, charset): a BOM, the server's charset, the declared one,
    UTF-8, then a guess; undecodable bytes replaced, never failing."""
    bom = bom_of(body)
    if bom:
        return body[len(next(m for m, n in BOMS if n == bom)):].decode(bom, "replace"), bom
    for name in (charset_named(header_charset), declared_charset(body)):
        if name:
            return body.decode(name, "replace"), name
    try:
        return body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        if exc.start >= len(body) - 3:          # cut mid-character at the size cap
            return body.decode("utf-8", "replace"), "utf-8"
    try:
        from charset_normalizer import from_bytes
        found = from_bytes(body[:200_000])
        best = found.best()
        # Western text fits several Latin code pages about equally; a
        # browser reads it as windows-1252, so that one wins a near tie.
        latin = next((m for m in found if m.encoding == "cp1252"), None)
        if best is not None and latin is not None and latin.chaos <= best.chaos + 0.05:
            best = latin
        if best is not None and best.encoding:
            name = charset_named(best.encoding)
            if name:
                return body.decode(name, "replace"), name
    except Exception:
        pass
    return body.decode("cp1252", "replace"), "cp1252"


class Fetched:
    """What came back: where it ended up, what it says it is, its bytes."""

    def __init__(self, url: str, final_url: str, status: int, content_type: str,
                 body: bytes, truncated: bool, redirects: List[str]):
        self.url = url
        self.final_url = final_url
        self.status = status
        self.content_type = content_type        # lower-case, no parameters
        self.header_charset = ""
        self.body = body
        self.truncated = truncated              # stopped reading at the cap
        self.redirects = redirects
        self.charset = ""

    def text(self) -> str:
        """The body as text, in the charset a browser would choose."""
        text, self.charset = decoded(self.body, self.header_charset)
        return text

    @property
    def is_pdf(self) -> bool:
        return self.body[:5] == b"%PDF-" or self.content_type == "application/pdf"


# -- bot checks -------------------------------------------------------------

#: Words a bot check's page carries, by who serves it.
CHALLENGES = (
    ("Cloudflare", ("cf-browser-verification", "cf_chl_opt", "/cdn-cgi/challenge-platform",
                    "attention required! | cloudflare", "cf-turnstile", "<title>just a moment...</title>")),
    ("DataDome", ("captcha-delivery.com", "geo.captcha-delivery")),
    ("PerimeterX", ("px-captcha", "_pxcaptcha", "perimeterx")),
    ("Imperva", ("_incapsula_resource", "incapsula incident id")),
    ("Akamai", ("/_sec/cp_challenge", "akamai bot manager")),
    ("AWS WAF", ("awswaf", "aws-waf-token")),
    ("DDoS-Guard", ("ddos-guard",)),
    ("Sucuri", ("sucuri website firewall", "sucuri_cloudproxy")),
    ("a captcha", ("g-recaptcha", "h-captcha", "hcaptcha.com/1/api.js", "recaptcha/api.js")),
    ("a bot check", ("checking your browser", "verify you are human", "verify you are a human",
                     "are you a robot", "unusual traffic from your", "please enable js and disable",
                     "please turn javascript on and reload", "enable javascript and cookies to continue")),
)


def challenge_of(markup: str) -> str:
    """Who serves the bot check this page is, or ''."""
    lowered = markup[:ERROR_BODY_MOST].lower()
    for who, marks in CHALLENGES:
        if any(mark in lowered for mark in marks):
            return who
    return ""


class SafeFetcher:
    MAX_BYTES = FILE_MOST
    MAX_REDIRECTS = 5
    CONNECT_SECONDS = 10
    READ_SECONDS = 20
    TOTAL_SECONDS = 45          # a trickling server cannot hold a call forever
    REDIRECT_STATUSES = (301, 302, 303, 307, 308)

    def __init__(self, user_agent: str, max_bytes: Optional[int] = None):
        self.user_agent = user_agent
        self.max_bytes = int(max_bytes or self.MAX_BYTES)

    def get(self, url: str, accept: str = "*/*") -> Fetched:
        original = url
        redirects: List[str] = []
        proxy = os.environ.get(PROXY_VARIABLE) or ""
        session = requests.Session()
        session.trust_env = False
        session.mount("http://", _CheckedAdapter())
        session.mount("https://", _CheckedAdapter())
        try:
            while True:
                if proxy:
                    self.check_url(url, resolve=False)
                else:
                    self.check_url(url)
                try:
                    response = session.get(
                        url, stream=True, allow_redirects=False,
                        timeout=(self.CONNECT_SECONDS, self.READ_SECONDS),
                        headers={"User-Agent": self.user_agent, "Accept": accept,
                                 "Accept-Language": "*"})
                except requests.exceptions.Timeout:
                    raise FetchError("timeout", f"{self._host(url)} did not answer in time.")
                except requests.exceptions.RequestException as exc:
                    raise FetchError("network", f"{self._host(url)} could not be reached: "
                                                f"{self._short(exc)}")
                with response:
                    if response.status_code in self.REDIRECT_STATUSES:
                        location = response.headers.get("Location") or ""
                        if not location:
                            raise FetchError("http", f"{url} redirected without saying where.",
                                             response.status_code)
                        if len(redirects) >= self.MAX_REDIRECTS:
                            raise FetchError("too_many_redirects",
                                             f"{original} redirected more than "
                                             f"{self.MAX_REDIRECTS} times; not followed further.")
                        url = urllib.parse.urljoin(url, location)
                        redirects.append(url)
                        continue
                    if response.status_code >= 400:
                        self._refused(response, url)
                    raw_type = response.headers.get("Content-Type") or ""
                    content_type = raw_type.split(";", 1)[0].strip().lower()
                    pdf = content_type == "application/pdf" or (
                        not content_type and urllib.parse.urlsplit(url).path.lower().endswith(".pdf"))
                    size = self._announced(response)
                    if pdf and size > self.max_bytes:
                        raise self._pdf_too_large(url, size, exact=True)
                    body, truncated = self._read(response, url)
                    if truncated and (pdf or body[:5] == b"%PDF-"):
                        raise self._pdf_too_large(url, len(body), exact=False)
                    fetched = Fetched(original, url, response.status_code, content_type,
                                      body, truncated, redirects)
                    for part in raw_type.split(";")[1:]:
                        name, _, value = part.partition("=")
                        if name.strip().lower() == "charset":
                            fetched.header_charset = value.strip().strip("\"'")
                    return fetched
        finally:
            session.close()

    def check_url(self, url: str, resolve: bool = True) -> None:
        """Refuse a URL before any byte is sent: its scheme, its
        credentials, and every address its host resolves to. With
        ``resolve`` off the name is left to the way out, and only an
        address written as an address is checked here."""
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise FetchError("invalid_url", f"{url!r} is not a valid URL: {exc}")
        if parts.scheme.lower() not in ("http", "https"):
            raise FetchError("invalid_url", f"Only http and https URLs can be fetched, "
                                            f"not {parts.scheme or 'a URL without a scheme'}.")
        if parts.username is not None or parts.password is not None:
            raise FetchError("invalid_url", "A URL with a user name or password in it "
                                            "is not fetched.")
        host = parts.hostname or ""
        if not host:
            raise FetchError("invalid_url", f"{url!r} names no host.")
        port = port or (443 if parts.scheme.lower() == "https" else 80)
        if not resolve:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return
            why = AddressPolicy.refusal(host)
            if why:
                raise ForbiddenAddress(f"Refused to fetch {host}: {why}. Only public "
                                       f"internet addresses are fetched.")
            return
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise FetchError("network", f"{host} could not be resolved: {exc}")
        addresses = sorted({str(answer[4][0]) for answer in answers})
        if not addresses:
            raise FetchError("network", f"{host} resolved to no address.")
        # One forbidden answer refuses the host: which one a connection
        # would use is not ours to choose.
        for address in addresses:
            why = AddressPolicy.refusal(address)
            if why:
                raise ForbiddenAddress(f"Refused to fetch {host}: {why}. Only public "
                                       f"internet addresses are fetched.")

    def _refused(self, response, url: str) -> None:
        """An error status as a FetchError: a sign-in and a bot check
        are told apart from other refusals, since a browser may pass."""
        status = response.status_code
        if status == 401:
            raise FetchError("sign_in", f"{url} answered HTTP 401: it needs a sign-in, which this "
                                        f"agent does not do.", status, needs_browser=True)
        if status in (403, 429, 503):
            try:
                head, _ = self._read(response, url, most=ERROR_BODY_MOST)
            except FetchError:
                head = b""
            who = challenge_of(decoded(head, "")[0]) if head else ""
            if who:
                raise FetchError("blocked", f"{url} answered HTTP {status} with a bot check "
                                            f"({who}): it lets browsers through, not this "
                                            f"reader.", status, needs_browser=True)
            if status == 403:
                raise FetchError("http", f"{url} answered HTTP 403 (forbidden): the site refuses "
                                         f"this reader, or the page needs a sign-in.", status,
                                 needs_browser=True)
        raise FetchError("http", f"{url} answered HTTP {status}.", status)

    def _pdf_too_large(self, url: str, size: int, exact: bool) -> FetchError:
        size_words, limit_words = human_size(size), human_size(self.max_bytes)
        if size_words == limit_words:           # "7.9 MB, at most 7.9 MB" says nothing
            size_words, limit_words = f"{size:,} bytes", f"{self.max_bytes:,} bytes"
        amount = size_words if exact else f"more than {size_words}"
        return FetchError(
            "too_large", f"The PDF at {url} is {amount}; this agent reads PDFs of at most "
                         f"{limit_words} (the most a chat file can be), so it was not "
                         f"read. Open it from its link instead: {url}", size=size, limit=self.max_bytes)

    @staticmethod
    def _announced(response) -> int:
        try:
            return max(0, int(response.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _read(self, response, url: str, most: Optional[int] = None):
        most = self.max_bytes if most is None else most
        chunks, size, started = [], 0, time.monotonic()
        try:
            for chunk in response.iter_content(64 * 1024):
                if time.monotonic() - started > self.TOTAL_SECONDS:
                    raise FetchError("timeout", f"{self._host(url)} was too slow to send the page.")
                chunks.append(chunk)
                size += len(chunk)
                if size > most:
                    return b"".join(chunks)[:most], True
        except requests.exceptions.RequestException as exc:
            raise FetchError("network", f"Reading {url} failed: {self._short(exc)}")
        return b"".join(chunks), False

    @staticmethod
    def _host(url: str) -> str:
        try:
            return urllib.parse.urlsplit(url).hostname or url
        except ValueError:
            return url

    @staticmethod
    def _short(exc: Exception) -> str:
        text = str(exc)
        return text if len(text) <= 200 else text[:200] + "…"


async def fetch(url: str, user_agent: str, accept: str = "*/*",
                max_bytes: Optional[int] = None) -> Fetched:
    """A fetch in a thread, off the agent's loop."""
    return await asyncio.to_thread(SafeFetcher(user_agent, max_bytes).get, url, accept)
