"""A message as it arrives, read into what the assistant is shown.

A mail server has no conversations, only messages that name the ones they answer, in ``References`` and in
``In-Reply-To`` (some programs write only the second). A conversation is rebuilt from both. Ids are the messages' own
``Message-ID``s, written without their angle brackets: the one name a message keeps in every folder and on every
server.

Headers are read whatever they were written in: RFC 2047 encoded words, and raw 8-bit bytes (UTF-8 mostly, some
older programs' Windows-1252 or Windows-1256), which the standard library hands back undecoded.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import html
import quopri
import re
from datetime import datetime, timezone
from email.header import Header, decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Tuple

ID_RE = re.compile(r"<([^<>\s]+)>")
TAG_RE = re.compile(r"<[^>]+>")
#: Whole blocks whose words are not the message's: scripts, styles, the head; and one cut off before its end.
DROPPED_RE = re.compile(r"(?is)<(script|style|head)\b.*?(</\1\s*>|$)")
COMMENT_RE = re.compile(r"(?s)<!--.*?(-->|$)")
BREAK_RE = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6])\s*/?>")
#: What 8-bit header bytes are tried as, in order, when the header does not say.
EIGHT_BIT = ("utf-8", "cp1256", "cp1252")


def bare(message_id: Any) -> str:
    """A Message-ID without its brackets."""
    found = ID_RE.search(str(message_id or ""))
    return found.group(1) if found else str(message_id or "").strip().strip("<>")


def bracketed(message_id: str) -> str:
    return f"<{bare(message_id)}>"


def _bytes_text(raw: bytes, charset: str | None) -> str:
    for name in ([charset] if charset and charset.lower() not in ("unknown-8bit", "x-unknown") else []) + list(
            EIGHT_BIT):
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def decoded(value: Any) -> str:
    """A header as a person reads it: encoded words decoded, raw 8-bit bytes read as UTF-8 (else an older
    charset), folds undone."""
    if value is None:
        return ""
    if isinstance(value, Header):
        pieces = decode_header(value)
    else:
        text = str(value)
        if any("\udc80" <= c <= "\udcff" for c in text):
            # Raw 8-bit bytes, kept by the parser as surrogates: back to bytes, then read.
            text = _bytes_text(text.encode("ascii", "surrogateescape"), None)
        try:
            pieces = decode_header(text)
        except Exception:  # noqa: BLE001 - a malformed encoded word is shown as it is
            pieces = [(text, None)]
    words = []
    for piece, charset in pieces:
        words.append(_bytes_text(piece, charset) if isinstance(piece, bytes) else str(piece))
    # decode_header leaves the spaces between plain and encoded runs out of the runs; join as RFC 2047 says.
    text = "".join(words) if len(pieces) > 1 else (words[0] if words else "")
    return " ".join(text.split())


def parse(raw: bytes) -> Message:
    return email.message_from_bytes(raw or b"", policy=email.policy.compat32)


def header(message: Message, name: str) -> str:
    return decoded(message.get(name))


def ids_in(value: Any) -> List[str]:
    return ID_RE.findall(decoded(value) if value is not None else "")


def message_id(message: Message) -> str:
    return bare(message.get("Message-ID"))


def parents(message: Message) -> List[str]:
    """Every message this one names as what it answers, oldest first: References, then In-Reply-To."""
    found = ids_in(message.get("References"))
    for named in ids_in(message.get("In-Reply-To")):
        if named not in found:
            found.append(named)
    return found


def thread_id(message: Message) -> str:
    """The conversation a message belongs to: the first message its References names, else the one it answers,
    else itself."""
    named = parents(message)
    return named[0] if named else message_id(message)


def sent_at(message: Message, fallback: str = "") -> str:
    """When it was sent, as ISO with its offset; the server's own time for it where the message does not say."""
    for value in (message.get("Date"), fallback):
        try:
            when = parsedate_to_datetime(decoded(value) if value is not None else "")
        except (TypeError, ValueError, IndexError):
            continue
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.isoformat()
    return ""


def received_at(received: str) -> str:
    """When the server received a message, as ISO."""
    return sent_at(Message(), received)


def moment(iso: str) -> datetime:
    try:
        when = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def address(value: Any) -> str:
    """The address alone, lowercase."""
    return parseaddr(decoded(value))[1].lower()


def addresses(*values: Any) -> List[str]:
    return [found.lower() for _, found in getaddresses(
        [decoded(value) for value in values if value]) if found and "@" in found]


def people(*values: Any) -> List[Tuple[str, str]]:
    """(name, address) of everyone in these headers, each address once, in order."""
    seen, found = set(), []
    for name, addr in getaddresses([decoded(value) for value in values if value]):
        if addr and "@" in addr and addr.lower() not in seen:
            seen.add(addr.lower())
            found.append((name, addr))
    return found


def _text_of(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    return _bytes_text(raw, part.get_content_charset())


def plain(markup: str) -> str:
    """The words of an HTML body: no styles, scripts or comments, even one cut off before its end."""
    text = COMMENT_RE.sub(" ", DROPPED_RE.sub(" ", markup))
    text = BREAK_RE.sub("\n", text)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"<[^>]*$", " ", text)  # a tag cut off at the end
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def is_attachment(part: Message) -> bool:
    if part.is_multipart():
        return False
    disposition = str(part.get("Content-Disposition") or "").lower()
    if disposition.startswith("attachment"):
        return True
    return bool(part.get_filename()) and not part.get_content_type().startswith("text/")


def filename(part: Message, number: int) -> str:
    """An attachment's name: RFC 2231 (filename*=utf-8''…), RFC 2047 in the parameter, or raw 8-bit."""
    try:
        name = part.get_filename()
    except (LookupError, UnicodeError):
        name = None
    return decoded(name) or f"attachment-{number}"


def body_and_attachments(message: Message) -> Tuple[str, List[Dict[str, Any]]]:
    """(the body as text, the attachments). An attachment's id is its place among the message's attachments, which
    does not change."""
    texts, markups, attached = [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        if is_attachment(part):
            raw = part.get_payload(decode=True) or b""
            attached.append({"attachment_id": f"a{len(attached) + 1}", "filename": filename(part, len(attached) + 1),
                             "mime_type": part.get_content_type(), "size": len(raw)})
        elif part.get_content_type() == "text/plain":
            texts.append(_text_of(part))
        elif part.get_content_type() == "text/html":
            markups.append(_text_of(part))
    text = "\n".join(t.strip() for t in texts if t.strip())
    if not text and markups:
        text = plain("\n".join(markups))
    return text.replace("\r\n", "\n").strip(), attached


def attachments_of(message: Message) -> List[Tuple[str, str, bytes]]:
    """(filename, mime type, bytes) of every attachment, in order."""
    found = []
    for part in message.walk():
        if is_attachment(part):
            found.append((filename(part, len(found) + 1), part.get_content_type(),
                          part.get_payload(decode=True) or b""))
    return found


def attachment_bytes(message: Message, attachment_id: str) -> bytes:
    wanted, seen = str(attachment_id or ""), 0
    for part in message.walk():
        if is_attachment(part):
            seen += 1
            if wanted == f"a{seen}":
                return part.get_payload(decode=True) or b""
    raise KeyError(attachment_id)


def snippet(text: str, length: int = 160) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= length else words[:length].rstrip() + "…"


def part_text(raw: bytes, encoding: str, charset: str, subtype: str) -> str:
    """The start of one text part as a server sent it (BODY[n]<0.size>): its transfer encoding undone, cut where
    it was cut, read in its charset; an HTML part's words only."""
    encoding = (encoding or "7bit").lower()
    if encoding == "base64":
        packed = re.sub(rb"[^A-Za-z0-9+/=]", b"", raw)
        packed = packed[:len(packed) - len(packed) % 4]
        try:
            raw = base64.b64decode(packed)
        except (binascii.Error, ValueError):
            raw = b""
    elif encoding == "quoted-printable":
        raw = quopri.decodestring(re.sub(rb"=[0-9A-Fa-f]?$", b"", raw))
    text = _bytes_text(raw, charset or None)
    if subtype == "html":
        return plain(text)
    return text


def row(message: Message, received: str = "", text: str | None = None) -> Dict[str, Any]:
    """One message as a search shows it. ``text`` is the start of its words where only that was fetched."""
    if text is None:
        text, _ = body_and_attachments(message)
    return {
        "message_id": message_id(message),
        "thread_id": thread_id(message),
        "from": header(message, "From"),
        "to": header(message, "To"),
        "date": sent_at(message, received),
        "subject": header(message, "Subject"),
        "snippet": snippet(text),
    }
