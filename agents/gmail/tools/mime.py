"""Mail as Gmail holds it, read and written.

Reading: Gmail hands a message as a tree of parts. Each leaf's ``body.data`` is its content with the transfer encoding
undone but **not** its charset: the bytes are in whatever the part's ``Content-Type`` says (ISO-8859-6, Windows-1256,
ISO-2022-JP, UTF-8...), so each part is decoded by its own declared charset, with a guess only when none is declared
or the declared one is wrong. A long text part comes as an ``attachmentId`` with no data, to be fetched like an
attachment. HTML becomes readable text when a message has no text part. Inline images (a ``Content-ID`` inside
``multipart/related``) are part of the HTML, not attachments, and are left out of the list.

Writing: a draft is one RFC 5322 message built here: ``multipart/alternative`` (the text, and the same text as safe
HTML with the signature), inside ``multipart/mixed`` when files are attached, each attachment's non-ASCII name per
RFC 2231.
"""

from __future__ import annotations

import base64
import codecs
import html
import mimetypes
import re
from email.header import decode_header, make_header
from email.headerregistry import Address
from email.message import EmailMessage, Message
from email.policy import SMTP
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any, Iterable

try:  # shipped with requests; the agent pins it
    from charset_normalizer import from_bytes as _guess_charset
except ImportError:  # pragma: no cover
    _guess_charset = None


# -- base64url ------------------------------------------------------------------------------------------------------

def b64url_decode(data: Any) -> bytes:
    data = str(data or "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


# -- charsets -------------------------------------------------------------------------------------------------------

#: Labels mail software writes that Python names otherwise, or that mean a superset in practice (WHATWG).
ALIASES = {
    "ks_c_5601-1987": "cp949", "ks_c_5601": "cp949", "euc-kr": "cp949",
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "gb_2312-80": "gb18030",
    "iso-8859-8-i": "iso-8859-8", "iso-8859-6-i": "iso-8859-6", "iso-8859-6-e": "iso-8859-6",
    "windows-874": "cp874", "tis-620": "cp874",
    "shift_jis": "cp932", "shift-jis": "cp932", "x-sjis": "cp932", "sjis": "cp932", "ms_kanji": "cp932",
    "unicode-1-1-utf-7": "utf-7", "utf8": "utf-8", "us-ascii": "utf-8", "ascii": "utf-8", "ansi_x3.4-1968": "utf-8",
    "x-user-defined": "cp1252", "big5": "big5hkscs",
}
LATIN1 = {"iso-8859-1", "iso8859-1", "latin1", "latin-1", "l1", "iso_8859-1", "cp819"}
JIS = {"iso-2022-jp", "iso2022_jp", "csiso2022jp"}


def _known(name: str) -> bool:
    try:
        codecs.lookup(name)
        return True
    except LookupError:
        return False


def decode_bytes(raw: bytes, charset: str = "") -> str:
    """Text from bytes in their declared charset; a guess when none is declared, or the declared one is wrong."""
    label = str(charset or "").strip().strip("\"'").lower()
    name = ALIASES.get(label, label)
    if name in LATIN1:
        candidates = ["cp1252", "latin-1"]          # what "ISO-8859-1" mail is written in, in practice
    elif name in JIS:
        candidates = ["iso2022_jp", "iso2022_jp_2", "iso2022_jp_ext"]
    elif name == "utf-8":
        candidates = ["utf-8-sig"]
    else:
        candidates = [name] if name else []
    for candidate in candidates:
        if not _known(candidate):
            continue
        try:
            return raw.decode(candidate)
        except UnicodeDecodeError:
            continue
    if "utf-8-sig" not in candidates:
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    if _guess_charset is not None and raw:
        best = _guess_charset(raw).best()
        if best is not None:
            return str(best)
    usable = next((c for c in candidates if _known(c)), "cp1252")
    return raw.decode(usable, "replace")


def header_text(value: Any) -> str:
    """A header's words: RFC 2047 encoded-words decoded where Gmail left them."""
    value = str(value or "")
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError, UnicodeDecodeError):
        return value


def content_param(content_type: str, name: str) -> str:
    message = Message()
    message["Content-Type"] = content_type or "text/plain"
    value = message.get_param(name)
    if isinstance(value, tuple):  # RFC 2231
        value = codecs.decode(bytes(value[2], "latin-1"), value[0] or "us-ascii", "replace") if value[2] else ""
    return str(value or "")


# -- HTML to text -----------------------------------------------------------------------------------------------------

BLOCKS = {"p", "div", "br", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "section",
          "article", "header", "footer", "hr", "pre", "address", "dl", "dt", "dd", "center", "form", "fieldset"}
HIDDEN = {"script", "style", "head", "title", "template", "noscript", "xml", "o:p"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden = 0
        self.pre = 0
        self.links: list[tuple[str, int]] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in HIDDEN:
            self.hidden += 1
            return
        if tag == "pre":
            self.pre += 1
        if tag == "li":
            self.out.append("\n• ")
        elif tag in ("td", "th"):
            self.out.append("\t")
        elif tag in BLOCKS:
            self.out.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append((href, len(self.out)))
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.out.append(f"[{alt}]")

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in ("br", "hr"):
            self.out.append("\n")
        else:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in HIDDEN:
            self.hidden = max(0, self.hidden - 1)
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in BLOCKS or tag == "li":
            self.out.append("\n")
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            words = "".join(self.out[start:]).strip()
            if href.lower().startswith(("http://", "https://")) and href not in words and words:
                self.out.append(f" ({href})")

    def handle_data(self, data):
        if self.hidden:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(markup: str) -> str:
    parser = _Text()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:  # a broken page still gives what was read of it
        pass
    text = "".join(parser.out).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+$", "", re.sub(r"^[ ]+", "", line)) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def text_to_html(text: str) -> str:
    """Plain text as HTML that shows exactly that text: escaped, its lines kept, its direction found per paragraph."""
    escaped = html.escape(text or "", quote=False).replace("\r\n", "\n").replace("\n", "<br>\n")
    return f'<div dir="auto">{escaped}</div>'


# -- a Gmail message, read ------------------------------------------------------------------------------------------

def headers_of(part: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for entry in part.get("headers") or []:
        name = str(entry.get("name") or "").lower()
        if name and name not in found:
            found[name] = str(entry.get("value") or "")
    return found


def header(message: dict[str, Any], name: str) -> str:
    return header_text(headers_of(message.get("payload") or {}).get(name.lower(), ""))


def walk(payload: dict[str, Any]) -> tuple[list[dict], list[dict], list[dict], int]:
    """(text/plain parts, text/html parts, attachments, how many inline images were left out)."""
    plain: list[dict] = []
    rich: list[dict] = []
    attachments: list[dict] = []
    inline = 0

    def visit(part: dict[str, Any], parent: str) -> None:
        nonlocal inline
        mime = str(part.get("mimeType") or "").lower()
        heads = headers_of(part)
        body = part.get("body") or {}
        filename = header_text(part.get("filename") or "")
        disposition = heads.get("content-disposition", "").strip().lower()
        if mime.startswith("multipart/"):
            for child in part.get("parts") or []:
                visit(child, mime)
            return
        # A picture the HTML shows by its Content-ID; Apple Mail's pictures in multipart/mixed are attachments.
        if (parent == "multipart/related" and heads.get("content-id") and not mime.startswith("text/")
                and not disposition.startswith("attachment")):
            inline += 1
            return
        if filename or disposition.startswith("attachment"):
            attachments.append(attachment_row(part, filename))
            return
        if mime == "text/plain":
            plain.append(part)
        elif mime == "text/html":
            rich.append(part)
        elif body.get("attachmentId") or body.get("data"):
            attachments.append(attachment_row(part, filename))

    visit(payload or {}, "")
    return plain, rich, attachments, inline


def attachment_row(part: dict[str, Any], filename: str) -> dict[str, Any]:
    body = part.get("body") or {}
    mime = str(part.get("mimeType") or "application/octet-stream").lower()
    if not filename:
        extension = mimetypes.guess_extension(mime) or ".bin"
        filename = f"part-{part.get('partId') or 'body'}{extension}"
    return {"part_id": str(part.get("partId") or ""), "attachment_id": str(body.get("attachmentId") or ""),
            "filename": filename, "mime_type": mime, "size": int(body.get("size") or 0)}


def find_part(payload: dict[str, Any], part_id: str = "", attachment_id: str = "",
              filename: str = "") -> dict[str, Any] | None:
    """A leaf by its part id, else by its attachment id, else by its filename when only one has it. Gmail gives an
    attachment a new attachmentId each time a message is read, so the part id is what stays."""
    leaves: list[dict] = []

    def visit(part):
        if part.get("parts"):
            for child in part["parts"]:
                visit(child)
        else:
            leaves.append(part)

    visit(payload or {})
    if part_id:
        found = [p for p in leaves if str(p.get("partId") or "") == part_id]
        if found:
            return found[0]
    if attachment_id:
        found = [p for p in leaves if str((p.get("body") or {}).get("attachmentId") or "") == attachment_id]
        if found:
            return found[0]
    if filename:
        found = [p for p in leaves if header_text(p.get("filename") or "") == filename]
        if len(found) == 1:
            return found[0]
    return None


def part_text(part: dict[str, Any], raw: bytes) -> str:
    """One text part's words, decoded by its own charset; HTML made readable."""
    heads = headers_of(part)
    content_type = heads.get("content-type", str(part.get("mimeType") or "text/plain"))
    charset = content_param(content_type, "charset")
    is_html = str(part.get("mimeType") or "").lower() == "text/html"
    if is_html and not charset:
        meta = re.search(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-:.]+)""", raw[:4096], re.I)
        charset = meta.group(1).decode("ascii", "replace") if meta else ""
    text = decode_bytes(raw, charset)
    return html_to_text(text) if is_html else text.replace("\r\n", "\n")


# -- addresses ------------------------------------------------------------------------------------------------------

def addresses(value: Any) -> list[tuple[str, str]]:
    """(name, address) pairs from a header or a person's list: commas or semicolons."""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    text = header_text(str(value or "")).replace(";", ",")
    return [(name.strip(), address.strip()) for name, address in getaddresses([text]) if address.strip()]


def bare(value: Any) -> str:
    return parseaddr(header_text(str(value or "")))[1].strip().lower()


def formatted(pairs: Iterable[tuple[str, str]]) -> str:
    return ", ".join(str(Address(display_name=name, addr_spec=address)) if name else address
                     for name, address in pairs)


def unique(pairs: Iterable[tuple[str, str]], leave_out: set[str]) -> list[tuple[str, str]]:
    seen, kept = set(leave_out), []
    for name, address in pairs:
        if address.lower() not in seen:
            seen.add(address.lower())
            kept.append((name, address))
    return kept


ADDRESS = re.compile(r"^[^@\s<>(),;:\"\[\]]+@[^@\s<>(),;:\"\[\]]+\.[^@\s<>(),;:\"\[\]]+$")


def wrong_addresses(pairs: Iterable[tuple[str, str]]) -> list[str]:
    return [address for _, address in pairs if not ADDRESS.match(address)]


# -- a message, written -----------------------------------------------------------------------------------------------

def _cte(text: str) -> str:
    """7bit for short ASCII lines, quoted-printable for mostly Latin text, base64 for the rest (Arabic, CJK)."""
    if text.isascii() and all(len(line) <= 900 for line in text.splitlines()):
        return "7bit"
    return "quoted-printable" if sum(c.isascii() for c in text) >= 0.8 * max(1, len(text)) else "base64"


def build(*, sender: tuple[str, str], to: list[tuple[str, str]], cc: list[tuple[str, str]],
          bcc: list[tuple[str, str]], subject: str, plain: str, rich: str,
          files: list[tuple[str, str, bytes]], in_reply_to: str = "", references: str = "") -> bytes:
    """The message's bytes. ``files`` are (filename, mime type, bytes)."""
    message = EmailMessage()
    message["From"] = formatted([sender])
    if to:
        message["To"] = formatted(to)
    if cc:
        message["Cc"] = formatted(cc)
    if bcc:
        message["Bcc"] = formatted(bcc)
    message["Subject"] = " ".join(str(subject or "").split())
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(plain, subtype="plain", charset="utf-8", cte=_cte(plain))
    message.add_alternative(rich, subtype="html", charset="utf-8", cte=_cte(rich))
    for filename, mime, data in files:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                               filename=filename)
        attached = message.get_payload()[-1]
        attached.set_param("name", filename)  # older readers look at the type's name; RFC 2231 when not ASCII
    return message.as_bytes(policy=SMTP)


def mime_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def reply_subject(subject: str) -> str:
    subject = " ".join(str(subject or "").split())
    return subject if re.match(r"^(re|aw|sv|رد)\s*:", subject, re.I) else f"Re: {subject}".strip()


def forward_subject(subject: str) -> str:
    subject = " ".join(str(subject or "").split())
    return subject if re.match(r"^(fwd?|wg|tr|إعادة توجيه)\s*:", subject, re.I) else f"Fwd: {subject}".strip()


def quoted_plain(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in (text or "").split("\n"))


def references_for(original_references: str, original_id: str) -> str:
    ids = re.findall(r"<[^<>\s]+>", original_references or "")
    if original_id and original_id not in ids:
        ids.append(original_id)
    # RFC 5322 keeps the first and the latest when the list is long.
    if len(ids) > 20:
        ids = ids[:1] + ids[-19:]
    return " ".join(ids)
