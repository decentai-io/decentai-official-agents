"""One account's mailbox, over IMAP.

Opened for a call and closed with it: a program holds no state, and a mail server counts its open connections.
Everything here blocks on a socket, so a tool runs it with ``asyncio.to_thread``, off the agent's loop. Nothing here
changes a message: folders are opened read-only, and the one thing written is a copy of what this agent sent,
appended to the Sent folder where the server did not keep one itself.

- **Folder names** travel in IMAP's modified UTF-7 and are shown decoded: "Входящие", "الأرشيف".
- **A conversation** is every message linked to it by Message-ID, References or In-Reply-To, followed both ways
  until nothing new is found, across the folders a conversation lives in: the account's archive of everything where
  it has one (Gmail's All Mail), else its inbox and Sent folder.
- **A snippet** is read from the message's first text part (its structure asked of the server first), decoded, and
  for HTML only its words: never the CSS or markup at the top of a long HTML body.
"""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from datetime import datetime
from email.message import Message
from email.utils import format_datetime
from typing import Any, Dict, List, Optional, Tuple

from . import messages, utf7
from .servers import Account, MailError

INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
LITERAL_RE = re.compile(rb"\{(\d+)\}$")

#: How much of a message's first text part a search reads for its first words: plain text, and HTML.
SNIPPET_BYTES = {"plain": 4096, "html": 32768}
#: How many messages a conversation is read to at most, and how many rounds its links are followed.
CONVERSATION_MOST = 300
ROUNDS = 5

#: What a Sent folder is called where the server does not say which one it is.
SENT_NAMES = ("sent", "sent items", "sent mail", "sent messages", "inbox.sent")
KINDS = (("\\all", "all"), ("\\sent", "sent"), ("\\drafts", "drafts"), ("\\trash", "trash"), ("\\junk", "junk"),
         ("\\archive", "archive"), ("\\flagged", "flagged"))


class _Connection(imaplib.IMAP4):
    """imaplib, over a socket that is already open and encrypted."""

    def __init__(self, link):
        self._link = link
        super().__init__("", 0)

    def _create_socket(self, timeout=None):
        return self._link


def quoted(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- reading what a server writes ---------------------------------------------------------------------------------

def flattened(data: List[Any]) -> bytes:
    """A response's pieces as one line, each literal written as a quoted string."""
    out = b""
    for item in data:
        if isinstance(item, tuple):
            head, literal = item[0], item[1] or b""
            out += LITERAL_RE.sub(b"", head.rstrip()) + b'"' + literal.replace(b"\\", b"\\\\").replace(
                b'"', b'\\"') + b'"'
        elif isinstance(item, bytes):
            out += item
    return out


def tokens(text: bytes) -> Any:
    """An IMAP parenthesized list as nested Python lists of str (None for NIL)."""
    position, stack, current = 0, [], []
    while position < len(text):
        char = text[position:position + 1]
        if char in (b" ", b"\r", b"\n"):
            position += 1
        elif char == b"(":
            stack.append(current)
            current = []
            position += 1
        elif char == b")":
            if not stack:
                return current  # the end of what holds the list
            done, current = current, stack.pop()
            current.append(done)
            position += 1
        elif char == b'"':
            end, value = position + 1, bytearray()
            while end < len(text) and text[end:end + 1] != b'"':
                if text[end:end + 1] == b"\\":
                    end += 1
                value += text[end:end + 1]
                end += 1
            current.append(bytes(value).decode("utf-8", "replace"))
            position = end + 1
        else:
            end = position
            while end < len(text) and text[end:end + 1] not in (b" ", b"(", b")", b"\r", b"\n"):
                end += 1
            atom = text[position:end].decode("utf-8", "replace")
            current.append(None if atom.upper() == "NIL" else atom)
            position = end
    return current


def structure_of(fetched: bytes) -> Any:
    """The BODYSTRUCTURE list of a FETCH response."""
    at = fetched.upper().find(b"BODYSTRUCTURE ")
    if at < 0:
        return None
    found = tokens(fetched[at + len(b"BODYSTRUCTURE "):])
    return found[0] if found and isinstance(found[0], list) else None


def first_text(body: Any, section: str = "") -> Optional[Dict[str, str]]:
    """The section, encoding, charset and subtype of a body's first readable part: text/plain, else text/html; never
    an attachment."""
    if not isinstance(body, list) or not body:
        return None
    if isinstance(body[0], list):  # multipart: its parts, then its subtype
        parts = [p for p in body if isinstance(p, list)]
        subtype = str(next((p for p in body if isinstance(p, str)), "") or "").lower()
        found = [first_text(part, f"{section}.{n}" if section else str(n)) for n, part in enumerate(parts, 1)]
        found = [f for f in found if f]
        if subtype == "alternative":
            return next((f for f in found if f["subtype"] == "plain"), found[0] if found else None)
        return found[0] if found else None
    kind, subtype = str(body[0] or "").lower(), str(body[1] or "").lower() if len(body) > 1 else ""
    if kind != "text" or subtype not in ("plain", "html"):
        return None
    # A text part: type, subtype, params, id, description, encoding, size, lines, md5, disposition, ...
    disposition = body[9] if len(body) > 9 else None
    if isinstance(disposition, list) and str(disposition[0] or "").lower() == "attachment":
        return None
    params = body[2] if len(body) > 2 and isinstance(body[2], list) else []
    pairs = {str(params[i]).lower(): str(params[i + 1] or "") for i in range(0, len(params) - 1, 2)}
    if "name" in pairs:
        return None
    return {"section": section or "1", "encoding": str(body[5] or "7bit") if len(body) > 5 else "7bit",
            "charset": pairs.get("charset", ""), "subtype": subtype}


class Mailbox:
    INBOX = "INBOX"

    def __init__(self, account: Account):
        self.account = account
        self.connection: Optional[_Connection] = None
        self.selected = ""
        self._folders: Optional[List[Dict[str, Any]]] = None

    # -- its life ------------------------------------------------------------------------------------------------
    def __enter__(self) -> "Mailbox":
        problems = self.account.problems()
        if problems:
            raise MailError("auth", " ".join(problems))
        link = self.account.to_imap()
        try:
            self.connection = _Connection(link)
        except (imaplib.IMAP4.error, OSError) as exc:
            link.close()
            raise MailError("network", f"{self.account.imap_host or 'The mail server'} did not greet: {exc}")
        try:
            self.connection.login(self.account.email, self.account.password)
        except imaplib.IMAP4.error as exc:
            self.close()
            raise MailError("auth", f"The mail server refused the sign-in for {self.account.email} "
                                    f"({self._said(exc)}). {self.account.hint()}")
        except (OSError, ssl.SSLError, UnicodeEncodeError) as exc:
            self.close()
            raise MailError("network", f"The mail server stopped answering: {exc}")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is None:
            return
        try:
            connection.logout()
        except Exception:
            try:
                connection.shutdown()
            except Exception:
                pass

    # -- one command ---------------------------------------------------------------------------------------------
    def ask(self, what: str, command, *arguments):
        """Run one command and return its data; a refusal and a dropped connection become a MailError."""
        try:
            status, data = command(*arguments)
        except imaplib.IMAP4.abort as exc:
            raise MailError("network", f"The mail server hung up while {what}: {self._said(exc)}")
        except imaplib.IMAP4.error as exc:
            raise MailError("server", f"The mail server refused {what}: {self._said(exc)}")
        except UnicodeEncodeError:
            raise MailError("invalid", f"What was asked while {what} can't be written to a mail server.")
        except (OSError, ssl.SSLError, socket.timeout) as exc:
            raise MailError("network", f"The mail server did not answer while {what}: {exc}")
        if status != "OK":
            raise MailError("server", f"The mail server refused {what}: {self._said(data)}")
        return data

    @staticmethod
    def _said(found) -> str:
        if isinstance(found, (list, tuple)):
            found = b" ".join(item for item in found if isinstance(item, bytes))
        if isinstance(found, bytes):
            found = found.decode("utf-8", "replace")
        return " ".join(str(found).replace("b'", "").replace("'", "").split())[:200]

    # -- folders -------------------------------------------------------------------------------------------------
    def listing(self) -> List[Dict[str, Any]]:
        """Every folder: its name as a person reads it, its name on the server, what it is, and whether it can be
        opened."""
        if self._folders is not None:
            return self._folders
        data = self.ask("listing the folders", self.connection.list)
        found = []
        lines, index = list(data), 0
        while index < len(lines):
            item = lines[index]
            index += 1
            line = flattened([item]) if isinstance(item, tuple) else item
            if not isinstance(line, bytes):
                continue
            parsed = tokens(line)
            if len(parsed) < 3 or not isinstance(parsed[0], list):
                continue
            flags = [str(f).lower() for f in parsed[0]]
            raw = str(parsed[2] or "")
            kind = next((k for flag, k in KINDS if flag in flags), "")
            if raw.upper() == "INBOX":
                raw, kind = "INBOX", "inbox"
            found.append({"name": utf7.decode(raw), "raw": raw, "kind": kind,
                          "selectable": "\\noselect" not in flags and "\\nonexistent" not in flags})
        if not any(f["kind"] == "sent" for f in found):
            for folder in found:
                lowered = folder["name"].lower()
                if folder["selectable"] and (lowered in SENT_NAMES or lowered.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
                                             in SENT_NAMES):
                    folder["kind"] = "sent"
                    break
        self._folders = found
        return found

    def folders(self) -> Dict[str, str]:
        """The folders that matter, by what they are (``all``, ``sent``, ``drafts``…): their names as shown."""
        found: Dict[str, str] = {}
        for folder in self.listing():
            if folder["kind"] and folder["selectable"]:
                found.setdefault(folder["kind"], folder["name"])
        return found

    def raw_name(self, name: str) -> str:
        """A folder's name as the server knows it: by the name shown, else as given, written in modified UTF-7."""
        if name.upper() == "INBOX":
            return "INBOX"
        for folder in self.listing():
            if folder["name"] == name or folder["raw"] == name:
                return folder["raw"]
        lowered = [f for f in self.listing() if f["name"].lower() == name.lower()]
        return lowered[0]["raw"] if len(lowered) == 1 else utf7.encode(name)

    def scope(self) -> List[str]:
        """The folders a conversation lives in."""
        folders = self.folders()
        if folders.get("all"):
            return [folders["all"]]
        return [self.INBOX] + ([folders["sent"]] if folders.get("sent") else [])

    def open(self, folder: str) -> Tuple[int, int]:
        """Open a folder to read it. Returns its validity and the next id it will give: the two numbers an inbox
        watch remembers."""
        self.ask(f"opening {folder}", self.connection.select, quoted(self.raw_name(folder)), True)
        self.selected = folder
        return (self._number("UIDVALIDITY"), self._number("UIDNEXT"))

    def status(self, folder: str) -> Dict[str, int]:
        data = self.ask(f"counting {folder}", self.connection.status, quoted(self.raw_name(folder)),
                        "(MESSAGES UNSEEN)")
        line = flattened(data)
        counts = {}
        for name in ("MESSAGES", "UNSEEN"):
            found = re.search(rb"%s (\d+)" % name.encode(), line)
            if found:
                counts[name.lower()] = int(found.group(1))
        return counts

    def _number(self, name: str) -> int:
        try:
            _, found = self.connection.response(name)
            return int(found[0]) if found and found[0] is not None else 0
        except (ValueError, TypeError, imaplib.IMAP4.error):
            return 0

    # -- finding -------------------------------------------------------------------------------------------------
    def search(self, criteria: List[str], text: str = "") -> List[int]:
        """The ids, in the open folder, of the messages that match: oldest first. ``text`` is words to find anywhere
        in a message, sent as they are whatever alphabet they are in."""
        arguments = list(criteria) or ["ALL"]
        if text:
            self.connection.literal = text.encode("utf-8")
            arguments = ["CHARSET", "UTF-8", *([] if arguments == ["ALL"] else arguments), "TEXT"]
        data = self.ask("searching", self.connection.uid, "SEARCH", *arguments)
        found = b" ".join(item for item in data if isinstance(item, bytes))
        return sorted(int(uid) for uid in found.split() if uid.isdigit())

    def after(self, last_uid: int) -> List[int]:
        """The ids that came after one. A server answers ``n:*`` with its last message even when that is older than
        n, so the answer is read, not believed."""
        return [uid for uid in self.search(["UID", f"{int(last_uid) + 1}:*"]) if uid > int(last_uid)]

    def by_id(self, message_id: str) -> Optional[Tuple[str, int]]:
        """(folder, id) of the message with this Message-ID."""
        for folder in self.scope():
            self.open(folder)
            found = self.search(["HEADER", "Message-ID", quoted(messages.bracketed(message_id))])
            if found:
                return folder, found[-1]
        return None

    @staticmethod
    def linked(ids: List[str]) -> List[str]:
        """A search for any message that is, or names, one of these ids."""
        keys: List[str] = []
        for message_id in ids:
            named = quoted(messages.bracketed(message_id))
            one = ["OR", "OR", "HEADER", "Message-ID", named, "HEADER", "References", named,
                   "HEADER", "In-Reply-To", named]
            keys = one if not keys else ["OR", *keys, *one]
        return keys

    def conversation(self, thread_id: str) -> List[Tuple[Message, str]]:
        """Every message of a conversation, oldest first, each with the time the server received it: those linked
        to it by Message-ID, References or In-Reply-To, the links followed both ways. A message in two folders is
        there once."""
        wanted, asked = {messages.bare(thread_id)}, set()
        found: Dict[str, Tuple[Message, str]] = {}
        seen_uids: set = set()
        for _ in range(ROUNDS):
            fresh = sorted(wanted - asked)
            if not fresh or len(found) >= CONVERSATION_MOST:
                break
            asked |= set(fresh)
            for folder in self.scope():
                self.open(folder)
                for start in range(0, len(fresh), 10):
                    for uid in self.search(self.linked(fresh[start:start + 10])):
                        if (folder, uid) in seen_uids or len(found) >= CONVERSATION_MOST:
                            continue
                        seen_uids.add((folder, uid))
                        message, received = self.whole(uid)
                        own = messages.message_id(message) or f"{folder}:{uid}"
                        found.setdefault(own, (message, received))
                        wanted.add(own)
                        wanted.update(messages.parents(message))
        return sorted(found.values(), key=lambda pair: messages.moment(messages.sent_at(*pair)))

    # -- reading -------------------------------------------------------------------------------------------------
    def glance(self, uid: int) -> Tuple[Message, str, bool, str]:
        """A message's headers and its first words: (headers, when the server received it, whether it was read,
        the start of its text)."""
        data = self.ask("reading a message", self.connection.uid, "FETCH", str(int(uid)),
                        "(FLAGS INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER])")
        head, about = b"", b""
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                if b"BODY[HEADER]" in item[0]:
                    head = item[1] or b""
                    about += LITERAL_RE.sub(b"", item[0].rstrip())
                else:
                    about += flattened([item])
            elif isinstance(item, bytes):
                about += item
        if not head:
            raise MailError("not_found", "The mail server no longer has that message.")
        flags = FLAGS_RE.search(about)
        seen = b"\\seen" in (flags.group(1).lower() if flags else b"")
        text = ""
        part = first_text(structure_of(about))
        if part is not None:
            most = SNIPPET_BYTES[part["subtype"]]
            body = self.ask("reading a message", self.connection.uid, "FETCH", str(int(uid)),
                            f"(BODY.PEEK[{part['section']}]<0.{most}>)")
            raw = b"".join(item[1] or b"" for item in body if isinstance(item, tuple) and len(item) == 2)
            text = messages.part_text(raw, part["encoding"], part["charset"], part["subtype"])
        return messages.parse(head), self._received(about), seen, text

    def size(self, uid: int) -> int:
        data = self.ask("reading a message", self.connection.uid, "FETCH", str(int(uid)), "(RFC822.SIZE)")
        found = SIZE_RE.search(flattened(data))
        return int(found.group(1)) if found else 0

    def whole(self, uid: int) -> Tuple[Message, str]:
        """(the message, when the server received it)."""
        data = self.ask("reading a message", self.connection.uid, "FETCH", str(int(uid)), "(INTERNALDATE BODY.PEEK[])")
        raw, about = b"", b""
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                about += item[0]
                raw = item[1] or raw
            elif isinstance(item, bytes):
                about += item
        if not raw:
            raise MailError("not_found", "The mail server no longer has that message.")
        return messages.parse(raw), self._received(about)

    @staticmethod
    def _received(about: bytes) -> str:
        found = INTERNALDATE_RE.search(about)
        if found is None:
            return ""
        try:
            when = datetime.strptime(found.group(1).decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            return ""
        return format_datetime(when)

    # -- the one thing written -----------------------------------------------------------------------------------
    def keep_sent(self, raw: bytes, message_id: str) -> bool:
        """Put a copy of a sent message in the Sent folder unless the server kept one itself. Returns whether a copy
        is there."""
        sent = self.folders().get("sent")
        if not sent:
            return False
        self.open(sent)
        if self.search(["HEADER", "Message-ID", quoted(messages.bracketed(message_id))]):
            return True
        self.ask("keeping a copy of what was sent", self.connection.append, quoted(self.raw_name(sent)), "(\\Seen)",
                 None, raw)
        return True
