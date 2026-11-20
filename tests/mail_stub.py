"""A loopback mail server for the Mail agent: one account's folders over IMAP and its outgoing mail over SMTP, speaking
as much of each protocol as the agent uses, in the clear, and as strictly as the RFCs where the agent depends on it.

    stub = MailStub().start()
    stub.add("INBOX", "dana@harbourline.example", "New office", "We are moving…")
    stub.secret()         the credential a person would have typed
    stub.sent             what was handed to the SMTP server

IMAP (RFC 3501): folder names travel in modified UTF-7 both ways, and a raw non-ASCII name is refused (BAD); a search
string other than ASCII needs CHARSET; FETCH answers FLAGS, INTERNALDATE, RFC822.SIZE, BODYSTRUCTURE, BODY.PEEK[HEADER],
BODY.PEEK[TEXT], BODY.PEEK[] and BODY.PEEK[section]<0.n>, and refuses anything else; STATUS (MESSAGES UNSEEN).

SMTP (RFC 5321, 6152, 1870): sign in before MAIL; the sender must be the account; no DATA without a recipient; no
8-bit bytes (8BITMIME is not offered) and no line over 998 octets; SIZE is offered and held to.

What a test can make it do: refuse the sign-in, refuse a recipient, refuse DATA itself, refuse the message after its
bytes, take a message and never answer, keep what it sends in the Sent folder as Gmail does, or renumber the inbox.
"""

from __future__ import annotations

import base64
import email
import email.policy
import os
import re
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Dict, List, Optional

DUBAI = timezone(timedelta(hours=4))
SIZE_MOST = 25 * 1024 * 1024


class utf7:
    """IMAP's modified UTF-7, the stub's own: Python's UTF-7 with "&" for "+" and "," for "/"."""

    @staticmethod
    def encode(name: str) -> str:
        out = ""
        for run in re.findall(r"[ -~]+|[^ -~]+", name):
            if run[0] >= " " and run[0] <= "~":
                out += run.replace("&", "&-")
            else:
                packed = run.encode("utf-7").decode("ascii")[1:].rstrip("-")
                out += "&" + packed.replace("/", ",") + "-"
        return out

    @staticmethod
    def decode(name: str) -> str:
        return re.sub(r"&([^-]*)-", lambda m: "&" if not m.group(1) else
                      ("+" + m.group(1).replace(",", "/") + "-").encode("ascii").decode("utf-7"), name)


class Stored:
    def __init__(self, uid: int, raw: bytes, flags, received: datetime):
        self.uid = uid
        self.raw = raw
        self.flags = set(flags)
        self.received = received

    @property
    def message(self):
        return email.message_from_bytes(self.raw, policy=email.policy.compat32)

    @property
    def head(self) -> bytes:
        return self.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"

    @property
    def text(self) -> bytes:
        parts = self.raw.split(b"\r\n\r\n", 1)
        return parts[1] if len(parts) > 1 else b""

    def words(self) -> str:
        found = [self.head.decode("utf-8", "replace")]
        for part in self.message.walk():
            if part.is_multipart():
                continue
            raw = part.get_payload(decode=True) or b""
            found.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
        return "\n".join(found).lower()


class Folder:
    def __init__(self, name: str, flags=()):
        self.name = name
        self.flags = list(flags)
        self.validity = 1700000000
        self.next = 1
        self.messages: List[Stored] = []

    def put(self, raw: bytes, flags=(), received: Optional[datetime] = None) -> Stored:
        stored = Stored(self.next, raw, flags, received or datetime.now(timezone.utc))
        self.next += 1
        self.messages.append(stored)
        return stored


class MailStub:
    ACCOUNT = "sara@sidra.example"
    PASSWORD = "app-pass-1"

    def __init__(self, archive: bool = False):
        self.folders: Dict[str, Folder] = {
            "INBOX": Folder("INBOX"),
            "Sent Items": Folder("Sent Items", ["\\Sent"]),
        }
        if archive:
            self.folders["[Mail]/All Mail"] = Folder("[Mail]/All Mail", ["\\All"])
        #: What was handed to the SMTP server and taken: (from, [to], raw).
        self.sent: List[tuple] = []
        #: Every message whose bytes reached the SMTP server, taken or not.
        self.received_data: List[bytes] = []
        self.refused_recipients: set = set()
        self.never_answers = False
        self.refuse_data = False
        self.reject_after_data = False
        self.keeps_sent = False
        #: Seconds the IMAP server waits before its greeting: a slow server.
        self.slow = 0.0
        self.logins = 0
        self.commands: List[str] = []
        self.bad: List[str] = []
        self._servers = []

    # -- what it holds -----------------------------------------------------------------------------------------------
    def add(self, folder: str, sender: str, subject: str, body: str, date: str = "", to: str = "",
            message_id: str = "", in_reply_to: str = "", references: str = "", attachments=(), html: str = "",
            seen: bool = False, cc: str = "", only_in_reply_to: bool = False) -> str:
        """Put a message in a folder. Returns its Message-ID, without brackets. ``date`` is ISO with an offset."""
        when = datetime.fromisoformat(date) if date else datetime.now(timezone.utc)
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to or self.ACCOUNT
        if cc:
            message["Cc"] = cc
        message["Subject"] = subject
        message["Date"] = format_datetime(when)
        message["Message-ID"] = f"<{message_id}>" if message_id else make_msgid(
            domain=sender.rsplit("@", 1)[-1].strip(">"))
        if in_reply_to:
            message["In-Reply-To"] = f"<{in_reply_to}>"
            if not only_in_reply_to:
                message["References"] = " ".join(f"<{found}>" for found in (references or in_reply_to).split())
        if html and not body:
            message.set_content(html, subtype="html")
        else:
            message.set_content(body)
            if html:
                message.add_alternative(html, subtype="html")
        for filename, mime_type, content in attachments:
            maintype, subtype = mime_type.split("/", 1)
            message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
        self.put(folder, message.as_bytes(policy=email.policy.SMTP), when, seen)
        return str(message["Message-ID"]).strip("<>")

    def put(self, folder: str, raw: bytes, when: Optional[datetime] = None, seen: bool = False) -> None:
        """Raw bytes as a message, as a server keeps whatever it was handed (8-bit headers included)."""
        self.folders[folder].put(raw, ["\\Seen"] if seen else [], when)
        archive = self.folders.get("[Mail]/All Mail")
        if archive is not None and folder != archive.name:
            archive.put(raw, ["\\Seen"] if seen else [], when)

    def renumber(self, folder: str = "INBOX") -> None:
        found = self.folders[folder]
        found.validity += 1
        for position, stored in enumerate(found.messages, 1):
            stored.uid = position
        found.next = len(found.messages) + 1

    # -- the credential -------------------------------------------------------------------------------------------
    def secret(self, password: str = "") -> dict:
        """As a person types it for another provider: each server with its port."""
        return {"account": self.ACCOUNT, "password": password or self.PASSWORD, "provider": "other",
                "imap_host": f"imap.sidra.example:{self.imap_port}",
                "smtp_host": f"smtp.sidra.example:{self.smtp_port}"}

    # -- the servers ----------------------------------------------------------------------------------------------
    def start(self) -> "MailStub":
        stub = self

        class Imap(ImapSession):
            mail = stub

        class Smtp(SmtpSession):
            mail = stub

        for handler in (Imap, Smtp):
            server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self._servers.append(server)
        self.imap_port = self._servers[0].server_address[1]
        self.smtp_port = self._servers[1].server_address[1]
        # Spoken to in the clear: said in the environment, which the runtime gives no program.
        os.environ["MAIL_TEST_PLAIN"] = "1"
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        os.environ.pop("MAIL_TEST_PLAIN", None)


# ----------------------------------------------------------------------
# IMAP
# ----------------------------------------------------------------------

TOKEN_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"|(\([^)]*\))|([^\s]+)')
MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
ITEM_RE = re.compile(r"^(FLAGS|INTERNALDATE|UID|RFC822\.SIZE|BODYSTRUCTURE|BODY\.PEEK\[(HEADER|TEXT|[\d.]*)\]"
                     r"(<0\.(\d+)>)?)$")


class Bad(Exception):
    pass


def imap_string(value) -> str:
    if value is None:
        return "NIL"
    text = str(value)
    if not text.isascii():
        text = text.encode("ascii", "replace").decode()
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def structure(part) -> str:
    """A part's BODYSTRUCTURE, as RFC 3501 §7.4.2 writes one (extension data: disposition only)."""
    if part.is_multipart():
        return ("(" + "".join(structure(p) for p in part.get_payload()) + " "
                + imap_string(part.get_content_subtype().upper()) + ")")
    maintype, subtype = part.get_content_maintype(), part.get_content_subtype()
    params = []
    if part.get_param("charset"):
        params += ["CHARSET", str(part.get_param("charset"))]
    if part.get_param("name"):
        params += ["NAME", str(part.get_param("name"))]
    listed = "(" + " ".join(imap_string(p) for p in params) + ")" if params else "NIL"
    encoding = str(part.get("Content-Transfer-Encoding") or "7bit").upper()
    payload = section_bytes(part)
    fields = [imap_string(maintype.upper()), imap_string(subtype.upper()), listed, "NIL", "NIL",
              imap_string(encoding), str(len(payload))]
    if maintype == "text":
        fields.append(str(payload.count(b"\n")))
    disposition = str(part.get("Content-Disposition") or "").split(";")[0].strip().upper()
    fields += ["NIL", f"({imap_string(disposition)} NIL)" if disposition else "NIL"]
    return "(" + " ".join(fields) + ")"


def section_bytes(part) -> bytes:
    payload = part.get_payload(decode=False)
    if not isinstance(payload, str):
        return b""
    try:
        return payload.encode("ascii", "surrogateescape")
    except UnicodeEncodeError:  # 8-bit text, handed back already read in its charset
        return payload.encode(part.get_content_charset() or "utf-8")


def section_of(message, number: str):
    part = message
    for step in [int(n) for n in number.split(".") if n]:
        if part.is_multipart():
            children = part.get_payload()
            if not 1 <= step <= len(children):
                raise Bad(f"no part {number}")
            part = children[step - 1]
        elif step != 1:
            raise Bad(f"no part {number}")
    return part


class ImapSession(socketserver.StreamRequestHandler):
    mail: MailStub

    def say(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8") + b"\r\n")
        self.wfile.flush()

    def handle(self):
        self.folder: Optional[Folder] = None
        self.signed_in = False
        if self.mail.slow:
            import time
            time.sleep(self.mail.slow)
        self.say("* OK the mail stub is ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            literals = []
            # A literal: the count is announced, the server says go on, the bytes follow, then the rest of the line.
            while re.search(rb"\{(\d+)\}\r\n$", line):
                count = int(re.search(rb"\{(\d+)\}\r\n$", line).group(1))
                self.say("+ go on")
                literals.append(self.rfile.read(count))
                line = re.sub(rb"\{\d+\}\r\n$", b"\x00", line) + self.rfile.readline()
            tag, _, rest = line.strip().partition(b" ")
            try:
                if not rest.isascii() and b"\x00" not in rest:
                    raise Bad("8-bit bytes in a command: a mailbox name is modified UTF-7, a search string a literal "
                              "with CHARSET")
                if not self.command(tag.decode(), rest, literals):
                    return
            except Bad as wrong:
                self.mail.bad.append(str(wrong))
                self.say(f"{tag.decode('ascii', 'replace')} BAD {wrong}")
            except (BrokenPipeError, ConnectionError):
                return

    def tokens(self, rest: bytes, literals) -> List[str]:
        found, waiting = [], list(literals)
        for quoted, listed, atom in TOKEN_RE.findall(rest):
            if atom == b"\x00":
                found.append(waiting.pop(0).decode("utf-8", "replace"))
            elif listed:
                found.append(listed.decode("utf-8"))
            elif atom:
                found.append(atom.decode("utf-8"))
            else:
                found.append(quoted.decode("utf-8").replace('\\"', '"').replace("\\\\", "\\"))
        return found

    def folder_named(self, name: str) -> Optional[Folder]:
        if not name.isascii():
            raise Bad("a mailbox name is sent in modified UTF-7")
        shown = "INBOX" if name.upper() == "INBOX" else utf7.decode(name)
        return self.mail.folders.get(shown)

    def command(self, tag: str, rest: bytes, literals) -> bool:
        words = self.tokens(rest, literals)
        name = words[0].upper() if words else ""
        if name == "UID" and len(words) > 1:
            name = f"UID {words[1].upper()}"
            words = words[1:]
        self.mail.commands.append(name)
        arguments = words[1:]

        if name == "CAPABILITY":
            self.say("* CAPABILITY IMAP4rev1 SPECIAL-USE")
        elif name == "LOGIN":
            self.mail.logins += 1
            if arguments[:2] != [self.mail.ACCOUNT, self.mail.PASSWORD]:
                self.say(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
                return True
            self.signed_in = True
        elif name == "LOGOUT":
            self.say("* BYE see you")
            self.say(f"{tag} OK done")
            return False
        elif not self.signed_in:
            self.say(f"{tag} NO sign in first")
            return True
        elif name == "LIST":
            for folder in self.mail.folders.values():
                flags = " ".join(["\\HasNoChildren", *folder.flags])
                self.say(f'* LIST ({flags}) "/" {imap_string(utf7.encode(folder.name))}')
        elif name in ("SELECT", "EXAMINE"):
            found = self.folder_named(arguments[0])
            if found is None:
                self.say(f"{tag} NO [NONEXISTENT] no such folder")
                return True
            self.folder = found
            self.say(f"* {len(found.messages)} EXISTS")
            self.say(f"* OK [UIDVALIDITY {found.validity}] the folder's validity")
            self.say(f"* OK [UIDNEXT {found.next}] the next number")
            self.say(f"{tag} OK [READ-ONLY] opened")
            return True
        elif name == "STATUS":
            found = self.folder_named(arguments[0])
            if found is None:
                self.say(f"{tag} NO [NONEXISTENT] no such folder")
                return True
            unseen = sum(1 for m in found.messages if "\\Seen" not in m.flags)
            self.say(f"* STATUS {imap_string(arguments[0])} (MESSAGES {len(found.messages)} UNSEEN {unseen})")
        elif name == "UID SEARCH":
            if self.folder is None:
                raise Bad("no folder is open")
            found = [str(stored.uid) for stored in self.folder.messages
                     if Search(arguments, self.folder).matches(stored)]
            self.say("* SEARCH " + " ".join(found) if found else "* SEARCH")
        elif name == "UID FETCH":
            if self.folder is None:
                raise Bad("no folder is open")
            self.fetch(arguments)
        elif name == "APPEND":
            folder = self.folder_named(arguments[0])
            if folder is None:
                self.say(f"{tag} NO [TRYCREATE] no such folder")
                return True
            flags = arguments[1].strip("()").split() if len(arguments) > 2 else []
            stored = folder.put(literals[-1] if literals else arguments[-1].encode("utf-8"), flags)
            self.say(f"{tag} OK [APPENDUID {folder.validity} {stored.uid}] kept")
            return True
        elif name in ("NOOP", "CLOSE"):
            pass
        else:
            raise Bad(f"the stub does not speak {name}")
        self.say(f"{tag} OK done")
        return True

    def fetch(self, arguments) -> None:
        wanted = int(arguments[0])
        items = arguments[1].strip("()").split()
        for item in items:
            if not ITEM_RE.match(item.upper()):
                raise Bad(f"unknown FETCH item {item}")
        for position, stored in enumerate(self.folder.messages, 1):
            if stored.uid != wanted:
                continue
            said, pieces = [f"UID {stored.uid}"], []
            for item in (i.upper() for i in items):
                found = ITEM_RE.match(item)
                if item == "FLAGS":
                    said.append("FLAGS (" + " ".join(sorted(stored.flags)) + ")")
                elif item == "INTERNALDATE":
                    said.append('INTERNALDATE "' + stored.received.strftime("%d-%b-%Y %H:%M:%S %z") + '"')
                elif item == "RFC822.SIZE":
                    said.append(f"RFC822.SIZE {len(stored.raw)}")
                elif item == "BODYSTRUCTURE":
                    said.append("BODYSTRUCTURE " + structure(stored.message))
                elif item.startswith("BODY.PEEK["):
                    section, start = found.group(2), found.group(4)
                    if section == "HEADER":
                        content, label = stored.head, "BODY[HEADER]"
                    elif section == "TEXT":
                        content, label = stored.text, "BODY[TEXT]"
                    elif section == "":
                        content, label = stored.raw, "BODY[]"
                    else:
                        content, label = section_bytes(section_of(stored.message, section)), f"BODY[{section}]"
                    if start:
                        content, label = content[:int(start)], label + "<0>"
                    pieces.append((label, content))
            self.wfile.write(f"* {position} FETCH ({' '.join(said)}".encode())
            for label, content in pieces:
                self.wfile.write(f" {label} {{{len(content)}}}\r\n".encode() + content)
            self.wfile.write(b")\r\n")
            self.wfile.flush()


class Search:
    """The search keys the agent sends, read against one message."""

    def __init__(self, words: List[str], folder: Folder):
        self.words = list(words)
        self.charset = ""
        if self.words[:1] and self.words[0].upper() == "CHARSET":
            self.charset, self.words = self.words[1].upper(), self.words[2:]
        if not self.charset and any(not w.isascii() for w in self.words):
            raise Bad("a search string other than ASCII needs CHARSET")
        self.folder = folder

    def matches(self, stored: Stored) -> bool:
        words = list(self.words)
        while words:
            if not self.key(words, stored):
                return False
        return True

    def key(self, words: List[str], stored: Stored) -> bool:
        name = words.pop(0).upper()
        message = stored.message
        if name == "ALL":
            return True
        if name == "UNSEEN":
            return "\\Seen" not in stored.flags
        if name == "SEEN":
            return "\\Seen" in stored.flags
        if name in ("FROM", "TO", "CC", "SUBJECT"):
            return words.pop(0).lower() in self.header(message, name).lower()
        if name in ("TEXT", "BODY"):
            return words.pop(0).lower() in stored.words()
        if name == "HEADER":
            header, value = words.pop(0), words.pop(0)
            return value.lower() in str(message.get(header) or "").lower()
        if name in ("SINCE", "BEFORE", "ON"):
            day, month, year = words.pop(0).split("-")
            asked = (int(year), MONTHS[month.lower()], int(day))
            received = stored.received
            have = (received.year, received.month, received.day)
            return {"SINCE": have >= asked, "BEFORE": have < asked, "ON": have == asked}[name]
        if name == "OR":
            first = self.key(words, stored)
            second = self.key(words, stored)
            return first or second
        if name == "NOT":
            return not self.key(words, stored)
        if name == "UID":
            return self.among(words.pop(0), stored.uid)
        raise Bad(f"the stub does not search by {name}")

    @staticmethod
    def header(message, name: str) -> str:
        from email.header import decode_header, make_header

        return str(make_header(decode_header(str(message.get(name) or ""))))

    def among(self, numbers: str, uid: int) -> bool:
        largest = max((stored.uid for stored in self.folder.messages), default=0)
        for part in numbers.split(","):
            first, colon, last = part.partition(":")
            low = largest if first == "*" else int(first)
            high = low if not colon else (largest if last == "*" else int(last))
            # As a server does: n:* names the last message even when that is older than n.
            if min(low, high) <= uid <= max(low, high):
                return True
        return False


# ----------------------------------------------------------------------
# SMTP
# ----------------------------------------------------------------------

class SmtpSession(socketserver.StreamRequestHandler):
    mail: MailStub

    def say(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8") + b"\r\n")
        self.wfile.flush()

    def handle(self):
        self.say("220 the mail stub is ready")
        sender, recipients, signed_in = "", [], False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            word, _, rest = line.decode("utf-8", "replace").strip().partition(" ")
            word = word.upper()
            if word in ("EHLO", "HELO"):
                self.say("250-the mail stub")
                self.say(f"250-SIZE {SIZE_MOST}")
                self.say("250-AUTH PLAIN LOGIN")
                self.say("250 OK")
            elif word == "AUTH":
                kind, _, given = rest.partition(" ")
                if kind.upper() == "PLAIN":
                    if not given:
                        self.say("334 ")
                        given = self.rfile.readline().decode().strip()
                    _, user, password = base64.b64decode(given).decode().split("\0")
                else:
                    self.say("334 " + base64.b64encode(b"Username:").decode())
                    user = base64.b64decode(self.rfile.readline().strip()).decode()
                    self.say("334 " + base64.b64encode(b"Password:").decode())
                    password = base64.b64decode(self.rfile.readline().strip()).decode()
                self.mail.logins += 1
                signed_in = (user, password) == (self.mail.ACCOUNT, self.mail.PASSWORD)
                self.say("235 signed in" if signed_in else "535 5.7.8 Username and Password not accepted")
            elif word == "MAIL":
                if not signed_in:
                    self.say("530 5.7.0 sign in first")
                    continue
                found = re.search(r"FROM:\s*<([^>]*)>(.*)", rest, re.IGNORECASE)
                if not found or found.group(1).lower() != self.mail.ACCOUNT:
                    self.say("553 5.7.1 the sender must be the account signed in")
                    continue
                size = re.search(r"SIZE=(\d+)", found.group(2), re.IGNORECASE)
                if size and int(size.group(1)) > SIZE_MOST:
                    self.say("552 5.3.4 message too big")
                    continue
                sender, recipients = found.group(1), []
                self.say("250 OK")
            elif word == "RCPT":
                found = re.search(r"TO:\s*<([^>]+)>", rest, re.IGNORECASE)
                if not sender or not found:
                    self.say("503 5.5.1 MAIL first, and RCPT TO:<address>")
                    continue
                address = found.group(1).lower()
                if address in self.mail.refused_recipients:
                    self.say("550 5.1.1 no such person here")
                else:
                    recipients.append(address)
                    self.say("250 OK")
            elif word == "DATA":
                if not recipients:
                    self.say("503 5.5.1 no valid recipients")
                    continue
                if self.mail.refuse_data:
                    self.say("554 5.7.1 message refused by policy")
                    continue
                self.say("354 go on")
                raw = b""
                while not raw.endswith(b"\r\n.\r\n"):
                    chunk = self.rfile.readline()
                    if not chunk:
                        return
                    raw += chunk
                raw = raw[:-3].replace(b"\r\n..", b"\r\n.")
                self.mail.received_data.append(raw)
                if self.mail.never_answers:
                    self.mail.sent.append((sender, list(recipients), raw))
                    return                  # hangs up without a word
                wrong = self.wrong(raw)
                if wrong or self.mail.reject_after_data:
                    self.say(f"554 5.6.0 {wrong or 'message rejected'}")
                    sender, recipients = "", []
                    continue
                self.mail.sent.append((sender, list(recipients), raw))
                if self.mail.keeps_sent:
                    self.mail.folders["Sent Items"].put(raw, ["\\Seen"])
                self.say("250 OK taken")
                sender, recipients = "", []
            elif word == "RSET":
                sender, recipients = "", []
                self.say("250 OK")
            elif word == "NOOP":
                self.say("250 OK")
            elif word == "QUIT":
                self.say("221 see you")
                return
            else:
                self.say(f"502 the stub does not speak {word}")

    @staticmethod
    def wrong(raw: bytes) -> str:
        if not raw.isascii():
            return "8-bit data, and 8BITMIME was not offered"
        if any(len(line) > 998 for line in raw.split(b"\r\n")):
            return "a line longer than 998 octets"
        if len(raw) > SIZE_MOST:
            return "message too big"
        return ""
