"""Reading RSS 2.0, RSS 1.0 (RDF), Atom 1.0 and JSON Feed, finding the
feed a site has, and reading an OPML list of feeds.

Real feeds are untidy, so the XML is read leniently but never
dangerously:

- the text is decoded first (a BOM, the server's charset, the XML
  declaration, UTF-8, then a guess), so a BOM, ``text/xml`` and a feed
  in windows-1256 are all read;
- a document that declares entities (``<!ENTITY``) is refused unread:
  entity expansion ("billion laughs") never reaches the parser;
- any ``<!DOCTYPE>`` is dropped, and HTML's named entities (``&nbsp;``,
  ``&eacute;``…) become the characters they name, outside CDATA, by a
  table lookup: nothing is expanded recursively;
- a bare ``&`` becomes ``&amp;``, and characters XML forbids are dropped.

Namespaces are dropped to local names before anything is looked up, so
a feed that borrows Dublin Core's ``dc:date`` or ``dc:creator`` is read.
Dates are normalised to UTC ``YYYY-MM-DDTHH:MM:SSZ``; a date that
cannot be read is no date, never a guess.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.entities import html5
from html.parser import HTMLParser
from typing import List, Optional

from .fetch import decoded

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml",
              "application/feed+json")
XML_TYPES = ("text/xml", "application/xml")
#: Where a site's feed usually is, when its pages advertise none.
PROBES = ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml", "/rss", "/feed.json")
SUMMARY_CHARS = 300
TEXT_CHARS = 5000
MAX_ITEMS = 500
MAX_ID_CHARS = 300
MAX_OUTLINES = 200


class FeedProblem(Exception):
    pass


class Item:
    def __init__(self, item_id: str, title: str, link: str, published: str,
                 summary: str, author: str, text: str = ""):
        self.id = item_id
        self.title = title
        self.link = link
        self.published = published      # normalised UTC, or ""
        self.summary = summary
        self.author = author
        #: Its words unclipped (to a bound), for keyword filters.
        self.text = text or summary

    @property
    def key(self) -> str:
        return seen_key(self.id)

    def row(self) -> dict:
        return {"id": self.id, "title": self.title, "link": self.link,
                "published": self.published, "summary": self.summary,
                "author": self.author}


class Feed:
    def __init__(self, url: str, kind: str, title: str, items: List[Item]):
        self.url = url
        self.format = kind               # rss | atom | rdf | json
        self.title = title
        self.items = items


def seen_key(item_id: str) -> str:
    """An item's id as 12 hex characters: 1,500 of them fit one record field."""
    return hashlib.sha1(item_id.encode("utf-8")).hexdigest()[:12]


# -- dates ------------------------------------------------------------------

def normalise_date(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    parsed: Optional[datetime] = None
    try:
        parsed = parsedate_to_datetime(text)            # RFC 822 / 2822
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        iso = text.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso)        # ISO 8601
        except ValueError:
            match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)"
                             r"(?:\.\d+)?([+-]\d{2}:?\d{2})?$", iso)
            if not match:
                return ""
            zone = (match.group(3) or "+00:00")
            if ":" not in zone:
                zone = zone[:3] + ":" + zone[3:]
            seconds = match.group(2) if match.group(2).count(":") == 2 else match.group(2) + ":00"
            try:
                parsed = datetime.fromisoformat(f"{match.group(1)}T{seconds}{zone}")
            except ValueError:
                return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):
        return ""


# -- what a body is ----------------------------------------------------------

FIRST_TAG = re.compile(r"<([A-Za-z_][\w:.-]*)")
PROLOGUE = re.compile(r"<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^\[>]*(\[.*?\])?\s*>", re.S | re.I)


def sniff(body: bytes) -> str:
    """'xml' for an RSS, Atom or RDF document, 'json' for a JSON Feed, else ''."""
    head = decoded(body[:4096])[0].lstrip("﻿ \t\r\n")
    if head.startswith("{"):
        return "json" if "jsonfeed.org/version" in decoded(body[:16384])[0] else ""
    found = FIRST_TAG.search(PROLOGUE.sub("", head))
    if found and found.group(1).lower().split(":")[-1] in ("rss", "feed", "rdf"):
        return "xml"
    return ""


def is_feed(fetched) -> bool:
    """A feed by its type or by its first bytes (a BOM and ``text/xml`` included)."""
    kind = sniff(fetched.body)
    if kind:
        return True
    return fetched.content_type in FEED_TYPES and fetched.content_type != "application/feed+json"


# -- lenient, safe XML -------------------------------------------------------

DOCTYPE = re.compile(r"<!DOCTYPE[^\[>]*(\[.*?\]\s*)?>", re.S | re.I)
DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
CDATA = re.compile(r"(<!\[CDATA\[.*?\]\]>)", re.S)
REFERENCE = re.compile(r"&(?:(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{0,31});)?")
FORBIDDEN_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
XML_OWN = {"amp", "lt", "gt", "quot", "apos"}


def _legal(code: int) -> bool:
    return code in (0x9, 0xA, 0xD) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD \
        or 0x10000 <= code <= 0x10FFFF


def _reference(match) -> str:
    name = match.group(1)
    if name is None:
        return "&amp;"                          # a bare ampersand
    if name.startswith("#"):
        try:
            code = int(name[2:], 16) if name[1:2] in ("x", "X") else int(name[1:])
        except ValueError:
            return ""
        return f"&#{code};" if _legal(code) else ""
    if name in XML_OWN:
        return f"&{name};"
    named = html5.get(name + ";")
    if named is None:
        return f"&amp;{name};"                  # not an entity: the words as written
    return "".join(f"&#{ord(ch)};" for ch in named)


def xml_text(body: bytes, header_charset: str, url: str) -> str:
    """The document as text XML can parse, or FeedProblem."""
    text, _ = decoded(body, header_charset)
    text = text.lstrip("﻿")
    if re.search(r"<!ENTITY", text, re.I):
        raise FeedProblem(f"{url} declares XML entities; it is not read.")
    text = DECLARATION.sub("", text, count=1)
    text = DOCTYPE.sub("", text, count=1)
    pieces = CDATA.split(text)
    for at in range(0, len(pieces), 2):        # the even pieces are outside CDATA
        pieces[at] = REFERENCE.sub(_reference, pieces[at])
    return FORBIDDEN_CHARS.sub("", "".join(pieces)).strip()


def parse_xml(body: bytes, header_charset: str, url: str):
    try:
        return ElementTree.fromstring(xml_text(body, header_charset, url))
    except ElementTree.ParseError as exc:
        raise FeedProblem(f"{url} is not readable XML: {exc}")


# -- parsing -----------------------------------------------------------------

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element, *names):
    for child in element:
        if _local(child.tag) in names:
            return child
    return None


def _text(element, *names) -> str:
    child = _child(element, *names)
    if child is None:
        return ""
    return " ".join("".join(child.itertext()).split())


def parse_feed(body: bytes, url: str, header_charset: str = "") -> Feed:
    """A feed from its bytes. Blocking: call it in a thread."""
    if sniff(body) == "json":
        return _json_feed(body, url, header_charset)
    root = parse_xml(body, header_charset, url)
    name = _local(root.tag)
    if name == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedProblem(f"{url} is RSS without a channel.")
        entries = [c for c in channel if _local(c.tag) == "item"]
        return Feed(url, "rss", _words(_text(channel, "title")),
                    [_rss_item(e, url) for e in entries[:MAX_ITEMS]])
    if name == "RDF":
        channel = _child(root, "channel")
        title = _text(channel, "title") if channel is not None else ""
        entries = [c for c in root if _local(c.tag) == "item"]
        return Feed(url, "rdf", _words(title), [_rss_item(e, url) for e in entries[:MAX_ITEMS]])
    if name == "feed":
        entries = [c for c in root if _local(c.tag) == "entry"]
        return Feed(url, "atom", _words(_text(root, "title")),
                    [_atom_item(e, url) for e in entries[:MAX_ITEMS]])
    raise FeedProblem(f"{url} is XML but not RSS or Atom (its root is <{name}>).")


def _rss_item(entry, base: str) -> Item:
    link = urllib.parse.urljoin(base, _text(entry, "link")) if _text(entry, "link") else ""
    about = ""
    for key, value in entry.attrib.items():
        if _local(key) == "about":
            about = value
    guid = _text(entry, "guid") or about
    published = normalise_date(_text(entry, "pubDate") or _text(entry, "date")
                               or _text(entry, "published") or _text(entry, "updated"))
    summary = _text(entry, "description") or _text(entry, "encoded")
    author = _text(entry, "author") or _text(entry, "creator")
    title = _words(_text(entry, "title"))
    plain = _plain(summary)
    return Item(_identity(guid, link, title, summary), title, link, published,
                _clip(plain), author, plain[:TEXT_CHARS])


def _atom_item(entry, base: str) -> Item:
    link = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        rel = child.attrib.get("rel", "alternate")
        if rel == "alternate" and child.attrib.get("href"):
            link = urllib.parse.urljoin(base, child.attrib["href"])
            break
        if not link and child.attrib.get("href"):
            link = urllib.parse.urljoin(base, child.attrib["href"])
    published = normalise_date(_text(entry, "published") or _text(entry, "updated")
                               or _text(entry, "date"))
    summary = _text(entry, "summary") or _text(entry, "content")
    author_element = _child(entry, "author")
    author = _text(author_element, "name") if author_element is not None else ""
    title = _words(_text(entry, "title"))
    plain = _plain(summary)
    return Item(_identity(_text(entry, "id"), link, title, summary), title, link,
                published, _clip(plain), author, plain[:TEXT_CHARS])


def _json_feed(body: bytes, url: str, header_charset: str) -> Feed:
    try:
        data = json.loads(decoded(body, header_charset or "utf-8")[0].lstrip("﻿"))
    except ValueError as exc:
        raise FeedProblem(f"{url} is not readable JSON: {exc}")
    if not isinstance(data, dict) or not str(data.get("version") or "").startswith(
            "https://jsonfeed.org/version/"):
        raise FeedProblem(f"{url} is JSON but not a JSON Feed.")
    items = []
    for entry in (data.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        link = str(entry.get("url") or entry.get("external_url") or "")
        link = urllib.parse.urljoin(url, link) if link else ""
        title = _words(str(entry.get("title") or ""))
        content = str(entry.get("summary") or entry.get("content_text") or "") or \
            _plain(str(entry.get("content_html") or ""))
        plain = _plain(content)
        authors = entry.get("authors") or ([entry["author"]] if isinstance(entry.get("author"), dict) else [])
        author = next((str(a.get("name") or "") for a in authors if isinstance(a, dict)), "")
        published = normalise_date(str(entry.get("date_published") or entry.get("date_modified") or ""))
        items.append(Item(_identity(str(entry.get("id") or ""), link, title, content), title, link,
                          published, _clip(plain), author, plain[:TEXT_CHARS]))
    return Feed(url, "json", _words(str(data.get("title") or "")), items)


def _identity(item_id: str, link: str, title: str, summary: str) -> str:
    """guid or id, else link, else a digest of the words — and never too long."""
    ident = item_id or link
    if not ident:
        ident = "sha1:" + hashlib.sha1(f"{title}\n{summary}".encode("utf-8")).hexdigest()
    if len(ident) > MAX_ID_CHARS:
        ident = "sha1:" + hashlib.sha1(ident.encode("utf-8")).hexdigest()
    return ident


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "li", "div"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _plain(markup: str) -> str:
    """Words without markup: descriptions are often HTML."""
    if "<" in markup or "&" in markup:
        parser = _Plain()
        try:
            parser.feed(markup)
            parser.close()
        except Exception:
            pass
        markup = "".join(parser.parts)
    return " ".join(markup.split())


def _words(text: str) -> str:
    return _plain(text) if ("<" in text or "&" in text) else text


def _clip(text: str) -> str:
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS].rstrip() + "…"


# -- discovery --------------------------------------------------------------

class _FeedLinks(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.feeds: List[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and attributes.get("href"):
            self.base = urllib.parse.urljoin(self.base, attributes["href"])
        if tag != "link":
            return
        rels = attributes.get("rel", "").lower().split()
        kind = attributes.get("type", "").lower().split(";")[0].strip()
        if "alternate" in rels and kind in FEED_TYPES and attributes.get("href"):
            url = urllib.parse.urljoin(self.base, attributes["href"].strip())
            if url not in self.feeds:
                self.feeds.append(url)


def advertised_feeds(markup: str, base: str) -> List[str]:
    parser = _FeedLinks(base)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    return parser.feeds


def probes(page_url: str) -> List[str]:
    """Where a site's feed may be: beside the page, then at the site's root."""
    parts = urllib.parse.urlsplit(page_url)
    root = f"{parts.scheme}://{parts.netloc}"
    found = []
    folder = parts.path.rstrip("/")
    if folder and "." not in folder.rsplit("/", 1)[-1]:
        found.append(f"{root}{folder}/feed")
    for path in PROBES:
        url = root + path
        if url not in found:
            found.append(url)
    return found


# -- OPML --------------------------------------------------------------------

def opml_feeds(body: bytes, name: str = "the OPML file") -> List[dict]:
    """[{url, title}] of an OPML list's feeds (outlines with an xmlUrl), in order."""
    root = parse_xml(body, "", name)
    if _local(root.tag).lower() != "opml":
        raise FeedProblem(f"{name} is XML but not OPML (its root is <{_local(root.tag)}>).")
    found, seen = [], set()
    for outline in root.iter():
        if _local(outline.tag) != "outline":
            continue
        attributes = {_local(k).lower(): v for k, v in outline.attrib.items()}
        url = (attributes.get("xmlurl") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = " ".join((attributes.get("title") or attributes.get("text") or "").split())
        found.append({"url": url, "title": title[:120]})
    return found
