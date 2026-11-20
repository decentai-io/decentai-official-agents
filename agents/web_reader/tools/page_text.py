"""Turning fetched bytes into readable text in numbered parts.

HTML is read with the standard library's parser: scripts, styles,
navigation, footers and asides are dropped, headings become their own
lines (Markdown-style, ``## Heading``) and start a new section, list
items keep a dash, and links are collected with absolute URLs. It is a
best effort, not a browser: a page that builds its text with
JavaScript, a bot check, or a sign-in form reads as nearly empty, and
the page says so (``needs_browser``, with why) rather than hiding it.

A PDF is read per page with pypdf, at most PDF_WINDOW pages from a
given page in one call and within PDF_SECONDS; the page count is always
the PDF's own, and the pages not read in this call are named, never
dropped. A page with no text layer is listed as unreadable. A page
longer than PAGE_CHARS_KEPT is clipped, and says so.

Plain text and JSON pass through. An HTML section or a text body is
split at line boundaries into parts of at most PART_CHARS that keep
its heading, so paging by part never has to cut text off. A PDF part is
one page, whatever its length, so that a part number is a page number.

This file is the same in every web agent that reads pages, and a test
fails when the copies differ.
"""

from __future__ import annotations

import io
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from .fetch import challenge_of

PART_CHARS = 4000
MAX_LINKS_KEPT = 500
#: The PDF pages read in one call, and the time they may take.
PDF_WINDOW = 400
PDF_SECONDS = 40.0
#: One PDF page's text kept at most; a longer page is clipped, and flagged.
PAGE_CHARS_KEPT = 100_000

TEXT_TYPES = ("application/json", "application/xml", "application/javascript")

#: Below these, a page that runs scripts may be an empty shell.
THIN_CHARS = 200
SPARSE_CHARS = 1500
SPARSE_RATIO = 0.01
SIGN_IN_PATHS = re.compile(r"/(log-?in|sign-?in|signin|sign_in|auth|sso|session/new|account/login)"
                           r"(/|\.|\?|$)", re.I)
SIGN_IN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com",
                 "appleid.apple.com", "www.facebook.com/login", "id.atlassian.com")
#: A <noscript> saying the page itself needs JavaScript (not "enable it for live chat").
NEEDS_JAVASCRIPT = re.compile(
    r"(need|needs|require|requires|required|must).{0,40}javascript"
    r"|javascript.{0,40}(required|must be enabled|to run|to use|to view|to continue)"
    r"|please (enable|turn on) javascript"
    r"|(تفعيل|تمكين|يتطلب|يجب).{0,30}(جافا ?سكري?بت|javascript)", re.I | re.S)


class Part:
    def __init__(self, number: int, heading: str, text: str, clipped: bool = False,
                 chars: int = 0):
        self.number = number
        self.heading = heading
        self.text = text
        self.clipped = clipped
        self.chars = chars or len(text)

    def row(self) -> dict:
        row = {"number": self.number, "heading": self.heading, "text": self.text}
        if self.clipped:
            row["clipped"] = True
            row["chars"] = self.chars
        return row


class Page:
    """A page as text: its kind, title, parts and links, and whether a
    browser would see more."""

    def __init__(self, kind: str, unit: str, title: str = ""):
        self.kind = kind                    # html | pdf | text | json
        self.unit = unit                    # section | page | part
        self.title = title
        self.parts: List[Part] = []
        self.links: List[dict] = []
        self.unreadable_pages: List[int] = []
        #: For a PDF: its page count, and the pages read in this call.
        self.total_pages = 0
        self.read_from = 0
        self.read_to = 0
        self.stopped = ""                   # why reading stopped before the last page
        #: A plain fetch sees less than a browser would: why (javascript,
        #: blocked, sign_in), and in words.
        self.needs_browser = False
        self.browser_reason = ""
        self.browser_note = ""
        self.html_chars = 0

    @property
    def characters(self) -> int:
        return sum(p.chars for p in self.parts)

    @property
    def total_parts(self) -> int:
        return self.total_pages if self.kind == "pdf" else len(self.parts)

    @property
    def unread_from(self) -> int:
        """The first PDF page not read in this call, or 0."""
        return self.read_to + 1 if self.kind == "pdf" and self.read_to < self.total_pages else 0

    def lines(self) -> List[str]:
        """Every line of readable text, headings included, whitespace
        collapsed — the form a watch compares."""
        out = []
        seen_heading = None
        for part in self.parts:
            if part.heading and part.heading != seen_heading:
                out.append(part.heading)
                seen_heading = part.heading
            for line in part.text.split("\n"):
                line = " ".join(line.split())
                if line:
                    out.append(line)
        return out

    def markdown(self) -> str:
        blocks, seen_heading = [], None
        for part in self.parts:
            if part.heading and part.heading != seen_heading:
                blocks.append(part.heading)
                seen_heading = part.heading
            if part.text:
                blocks.append(part.text)
        return "\n\n".join(blocks)


class UnsupportedContent(Exception):
    pass


def kind_of(content_type: str, body: bytes) -> str:
    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if body[:5] == b"%PDF-" or content_type == "application/pdf":
        return "pdf"
    if content_type in ("text/html", "application/xhtml+xml") or \
            head.startswith((b"<!doctype html", b"<html")):
        return "html"
    if content_type == "application/json" or content_type.endswith("+json"):
        return "json"
    if content_type.startswith("text/") or content_type in TEXT_TYPES or \
            content_type.endswith("+xml"):
        return "text"
    if not content_type:
        try:
            body[:4096].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "unsupported"


def read_page(fetched, first: int = 1) -> Page:
    """The page as text; a PDF from page ``first``. Blocking (pypdf, the
    HTML parser): call it in a thread."""
    kind = kind_of(fetched.content_type, fetched.body)
    if kind == "pdf":
        return _pdf(fetched, first)
    if kind == "html":
        return _html(fetched.text(), fetched.final_url)
    if kind in ("text", "json"):
        page = Page(kind, "part")
        _add_parts(page, "", fetched.text().replace("\r\n", "\n"))
        return page
    raise UnsupportedContent(
        f"{fetched.final_url} is {fetched.content_type or 'of an unknown type'}, "
        f"which cannot be read as text.")


# -- splitting ------------------------------------------------------------

def _add_parts(page: Page, heading: str, text: str) -> None:
    text = text.strip("\n")
    if not text and not heading:
        return
    for chunk in _chunks(text):
        page.parts.append(Part(len(page.parts) + 1, heading, chunk))


def _chunks(text: str) -> List[str]:
    if len(text) <= PART_CHARS:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > PART_CHARS:          # one enormous line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:PART_CHARS])
            line = line[PART_CHARS:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > PART_CHARS:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# -- PDF ------------------------------------------------------------------

def _pdf(fetched, first: int = 1) -> Page:
    if fetched.truncated:
        # The fetch refuses a PDF over its cap; this is the last guard.
        raise UnsupportedContent(
            f"Only the first {len(fetched.body):,} bytes of the PDF at {fetched.final_url} "
            f"arrived, and part of a PDF cannot be read.")
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(fetched.body))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                raise UnsupportedContent(f"The PDF at {fetched.final_url} is encrypted (it may "
                                         f"need a password), so its text cannot be read.")
        pages = reader.pages
        count = len(pages)
    except UnsupportedContent:
        raise
    except Exception as exc:
        raise UnsupportedContent(f"The PDF at {fetched.final_url} could not be opened: {exc}")
    title = ""
    try:
        title = str((reader.metadata or {}).get("/Title") or "")
    except Exception:
        title = ""
    page = Page("pdf", "page", title)
    page.total_pages = count
    first = max(1, min(int(first or 1), max(count, 1)))
    page.read_from, page.read_to = first, first - 1
    started = time.monotonic()
    for number in range(first, min(count, first + PDF_WINDOW - 1) + 1):
        if number > first and time.monotonic() - started > PDF_SECONDS:
            page.stopped = "time"
            break
        try:
            text = pages[number - 1].extract_text() or ""
        except Exception:
            text = ""
        text = "\n".join(" ".join(line.split()) for line in text.split("\n")).strip()
        if not text:
            page.unreadable_pages.append(number)
        # One part per page, numbered by page; a page too long to keep
        # is clipped, and says so with its length.
        page.parts.append(Part(number, "", text[:PAGE_CHARS_KEPT], len(text) > PAGE_CHARS_KEPT,
                               len(text)))
        page.read_to = number
    if not page.stopped and page.read_to < count:
        page.stopped = "window"
    return page


# -- HTML -----------------------------------------------------------------

class _Reader(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer",
            "aside", "iframe", "canvas", "select", "button"}
    SKIP_ROLES = {"navigation", "contentinfo", "complementary", "banner", "search"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "br", "tr", "table",
             "ul", "ol", "li", "blockquote", "pre", "dd", "dt", "dl", "figure",
             "figcaption", "hr", "form", "fieldset", "address", "details", "summary"}
    HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "source", "track", "wbr"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.blocks: List[Tuple[int, str]] = []   # (heading level or 0, text)
        self.links: List[dict] = []
        self._link_urls = set()
        self._skip_stack: List[str] = []
        self._in_title = False
        self._heading = 0
        self._buffer: List[str] = []
        self._pre = 0
        self._anchor: Optional[dict] = None
        # What says a browser would see more.
        self.scripts = 0
        self.passwords = 0
        self.noscript: List[str] = []

    # the parser's callbacks
    def handle_starttag(self, tag, attrs):
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            self.scripts += 1
        if tag == "input" and attributes.get("type", "").lower() == "password":
            self.passwords += 1
        if self._skip_stack:
            if tag not in self.VOID:
                self._skip_stack.append(tag)
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "base" and attributes.get("href"):
            self.base_url = urllib.parse.urljoin(self.base_url, attributes["href"])
            return
        if tag in self.SKIP or attributes.get("role", "").lower() in self.SKIP_ROLES \
                or "hidden" in attributes or attributes.get("aria-hidden") == "true":
            if tag not in self.VOID:
                self._skip_stack.append(tag)
            return
        if tag in self.HEADINGS:
            self._flush()
            self._heading = self.HEADINGS[tag]
        elif tag in self.BLOCK:
            self._flush()
            if tag == "li":
                self._buffer.append("- ")
            if tag == "pre":
                self._pre += 1
        elif tag in ("td", "th"):
            self._buffer.append(" | ")
        if tag == "a" and attributes.get("href"):
            self._anchor = {"href": attributes["href"], "text": []}

    def handle_endtag(self, tag):
        if self._skip_stack:
            # Close back to the matching opener; a stray end tag inside a
            # skipped element is ignored rather than ending the skip.
            if tag in self._skip_stack:
                while self._skip_stack and self._skip_stack.pop() != tag:
                    pass
            return
        if tag == "title":
            self._in_title = False
        elif tag in self.HEADINGS:
            self._flush()
            self._heading = 0
        elif tag in self.BLOCK:
            self._flush()
            if tag == "pre" and self._pre:
                self._pre -= 1
        if tag == "a" and self._anchor is not None:
            self._keep_link(self._anchor)
            self._anchor = None

    def handle_data(self, data):
        if self._in_title and not self._skip_stack:
            self.title += data
            return
        if self._skip_stack:
            if self._skip_stack[0] == "noscript" and sum(map(len, self.noscript)) < 2000:
                self.noscript.append(data)
            return
        self._buffer.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def close(self):
        super().close()
        self._flush()

    # helpers
    def _flush(self):
        raw = "".join(self._buffer)
        self._buffer = []
        if self._pre:
            lines = [line.rstrip() for line in raw.split("\n")]
            text = "\n".join(lines).strip("\n")
        else:
            text = " ".join(raw.split())
            text = text.strip(" |")
        if text and text != "-":
            self.blocks.append((self._heading, text))

    def _keep_link(self, anchor):
        if len(self.links) >= MAX_LINKS_KEPT:
            return
        url = urllib.parse.urljoin(self.base_url, anchor["href"].strip())
        url = urllib.parse.urldefrag(url)[0]
        if not url.lower().startswith(("http://", "https://")) or url in self._link_urls:
            return
        self._link_urls.add(url)
        text = " ".join("".join(anchor["text"]).split())[:200]
        self.links.append({"text": text, "url": url})


def _html(markup: str, base_url: str) -> Page:
    reader = _Reader(base_url)
    try:
        reader.feed(markup)
        reader.close()
    except Exception:
        # html.parser is forgiving; whatever it managed before giving up
        # is still the page's text.
        reader._flush()
    page = Page("html", "section", " ".join(reader.title.split()))
    heading, lines = "", []
    for level, text in reader.blocks:
        if level:
            _add_parts(page, heading, "\n".join(lines))
            heading, lines = "#" * level + " " + text, []
        else:
            lines.append(text)
    _add_parts(page, heading, "\n".join(lines))
    page.links = reader.links
    page.html_chars = len(markup)
    _judge(page, reader, markup, base_url)
    return page


def _judge(page: Page, reader: _Reader, markup: str, url: str) -> None:
    """Whether a browser would see more than this reader did, and why."""
    chars = page.characters
    sparse = chars < SPARSE_CHARS
    noscript = " ".join(" ".join(reader.noscript).split()).lower()
    where = urllib.parse.urlsplit(url)
    signing_in = (where.hostname or "") in SIGN_IN_HOSTS or bool(SIGN_IN_PATHS.search(where.path or ""))
    who = challenge_of(markup) if sparse else ""
    if who:
        page.needs_browser, page.browser_reason = True, "blocked"
        page.browser_note = (f"The page is a bot check ({who}), not the page itself: a browser "
                             f"gets through where this reader cannot.")
    elif reader.passwords and (sparse or signing_in):
        page.needs_browser, page.browser_reason = True, "sign_in"
        page.browser_note = "The page asks for a sign-in; what is behind it was not read."
    elif reader.scripts and (
            chars < THIN_CHARS
            or (sparse and page.html_chars > 20_000 and chars / page.html_chars < SPARSE_RATIO)
            or (sparse and NEEDS_JAVASCRIPT.search(noscript))):
        page.needs_browser, page.browser_reason = True, "javascript"
        page.browser_note = ("Almost no readable text: the page builds its content with "
                             "JavaScript, which this reader does not run.")


def normalized(text: str) -> str:
    return " ".join(text.split())


def phrase_pattern(phrase: str):
    """A phrase as a case-insensitive pattern that matches across any run
    of whitespace, so a line break in the page does not hide it."""
    words = [re.escape(word) for word in phrase.split()]
    return re.compile(r"\s+".join(words), re.IGNORECASE)
