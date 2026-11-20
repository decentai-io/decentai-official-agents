"""Sending, over SMTP. Everything here blocks on a socket: a tool runs it with ``asyncio.to_thread``.

A message is handed to the account's own mail server, which sends it as the account. Three outcomes are told apart,
because the assistant must word them differently and because only one of them may ever be sent again:

- **sent:** the server answered 250 to the message.
- **refused, nothing sent** (a MailError of kind ``server``, ``auth`` or ``network``): the sign-in, the sender, a
  recipient, or the DATA command itself was refused, or the connection failed before the message's bytes went. The
  draft stays prepared and may be sent again.
- **unknown:** the message's bytes went and no answer came. Nothing here tries again.

The message is written 7-bit clean (base64 or quoted-printable bodies, RFC 2047 headers), so it passes any server,
with or without 8BITMIME; an attachment's name other than ASCII is written per RFC 2231 (``filename*=utf-8''…``).
"""

from __future__ import annotations

import mimetypes
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from typing import Iterable, List, Tuple

from . import messages
from .servers import Account, MailError, SECONDS

#: Written 7-bit, lines folded at 78, CRLF: what any SMTP server takes.
POLICY = SMTP.clone(cte_type="7bit")


class _Connection(smtplib.SMTP):
    """smtplib, over a socket that is already open. ``host`` is the server's name: what its certificate is checked
    against when the connection is encrypted after the greeting (STARTTLS)."""

    def __init__(self, link, host: str):
        self._link = link
        super().__init__(timeout=SECONDS)
        self._host = host

    def _get_socket(self, host, port, timeout):
        return self._link


def new_message_id(account_email: str) -> str:
    domain = account_email.rsplit("@", 1)[-1] or "localhost"
    return messages.bare(make_msgid(domain=domain))


def written(account_email: str, *, to: str, subject: str, body: str, cc: str = "",
            in_reply_to: str = "", references: Iterable[str] = (), message_id: str = "",
            attachments: Iterable[Tuple[str, str, bytes]] = ()) -> Tuple[bytes, str]:
    """(the message as it travels, its Message-ID without brackets). ``attachments``: (filename, mime type, bytes).
    Bcc recipients are never written into the message."""
    message = EmailMessage(policy=POLICY)
    message["From"] = account_email
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = messages.bracketed(message_id or new_message_id(account_email))
    if in_reply_to:
        message["In-Reply-To"] = messages.bracketed(in_reply_to)
        chain = [messages.bracketed(found) for found in references]
        if messages.bracketed(in_reply_to) not in chain:
            chain.append(messages.bracketed(in_reply_to))
        message["References"] = " ".join(chain)
    message.set_content(body, charset="utf-8")
    for name, mime_type, content in attachments:
        kind = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, _, subtype = kind.partition("/")
        if maintype == "text":
            maintype, subtype = "application", "octet-stream"  # bytes as they are: no charset guessed
        message.add_attachment(content, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
        part = message.get_payload()[-1]
        part.set_param("name", name)  # the older place some programs read a name from
    return message.as_bytes(policy=POLICY), messages.bare(message["Message-ID"])


def dot_stuffed(raw: bytes) -> bytes:
    """The message as DATA carries it: CRLF lines, a leading dot doubled, ended by a lone dot."""
    raw = re.sub(rb"\r?\n", b"\r\n", raw)
    raw = re.sub(rb"(?m)^\.", b"..", raw)
    if not raw.endswith(b"\r\n"):
        raw += b"\r\n"
    return raw + b".\r\n"


class Outgoing:
    def __init__(self, account: Account):
        self.account = account

    def send(self, raw: bytes, recipients: List[str]) -> None:
        """Hand the message to the server. Returns when the server took it; raises MailError otherwise,
        ``unknown`` when nobody knows."""
        if not recipients:
            raise MailError("server", "The message names nobody to send it to.")
        link, port, encrypted = self.account.to_smtp()
        host = self.account.smtp_host or "localhost"
        connection = _Connection(link, host)
        try:
            try:
                connection.connect(host, port)
                connection.ehlo()
                if encrypted is False:
                    connection.starttls(context=Account.context())
                    connection.ehlo()
                connection.login(self.account.email, self.account.password)
            except smtplib.SMTPAuthenticationError as exc:
                raise MailError("auth", f"The mail server refused the sign-in for {self.account.email} "
                                        f"({self._said(exc)}). {self.account.hint()}")
            except smtplib.SMTPNotSupportedError as exc:
                raise MailError("server", f"{host} offers no way to sign in that this agent speaks: {exc}")
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                raise MailError("network", f"{host} could not be spoken to: {self._said(exc)}")
            most = self._size_limit(connection)
            if most and len(raw) > most:
                raise MailError("too_large", f"The message is {len(raw) // 1024:,} KB and {host} takes at most "
                                             f"{most // 1024:,} KB. Nothing was sent: send fewer or smaller "
                                             f"attachments, or a link.")
            self._hand_over(connection, raw, recipients)
        finally:
            try:
                connection.quit()
            except Exception:
                connection.close()

    @staticmethod
    def _size_limit(connection) -> int:
        size = str((connection.esmtp_features or {}).get("size") or "").strip()
        return int(size) if size.isdigit() else 0

    def _hand_over(self, connection, raw: bytes, recipients: List[str]) -> None:
        try:
            code, said = connection.mail(self.account.email, [f"SIZE={len(raw)}"]
                                         if self._size_limit(connection) else [])
            if code != 250:
                raise MailError("server", f"The mail server refused the sender: {self._said(said)}. Nothing was "
                                          f"sent.")
            refused = []
            for recipient in recipients:
                code, said = connection.rcpt(recipient)
                if code not in (250, 251):
                    refused.append(f"{recipient} ({self._said(said)})")
            if refused:
                connection.rset()
                raise MailError("server", "The mail server refused to send to " + ", ".join(refused)
                                + ". Nothing was sent.")
            # DATA itself may be refused (a 4xx or 5xx in place of 354): the message has not gone, and may go later.
            code, said = connection.docmd("DATA")
            if code != 354:
                raise MailError("server", f"The mail server refused to take the message ({code} "
                                          f"{self._said(said)}). Nothing was sent.")
        except (smtplib.SMTPServerDisconnected, OSError, socket.timeout) as exc:
            raise MailError("network", f"The mail server hung up before the message was handed over: "
                                       f"{self._said(exc)}. Nothing was sent.")
        # From here the message's bytes are on their way: an answer that does not come is an outcome nobody knows.
        try:
            connection.send(dot_stuffed(raw))
            code, said = connection.getreply()
        except (smtplib.SMTPServerDisconnected, OSError, socket.timeout) as exc:
            raise MailError("unknown", f"The message was handed to the mail server and no answer came back: "
                                       f"whether it was sent is not known ({self._said(exc)}).")
        if code != 250:
            raise MailError("server", f"The mail server refused the message: {code} {self._said(said)}. Nothing "
                                      f"was sent.")

    @staticmethod
    def _said(found) -> str:
        if isinstance(found, smtplib.SMTPResponseException):
            found = found.smtp_error
        if isinstance(found, bytes):
            found = found.decode("utf-8", "replace")
        return " ".join(str(found).split())[:200]


def reply_all(last, me: set, to: str) -> List[Tuple[str, str]]:
    """Who a reply-all copies: everyone the last message went to and came from, but the account and those it is
    addressed to."""
    skip = set(me) | {a for _, a in messages.people(to)}
    skip = {a.lower() for a in skip}
    return [(n, a) for n, a in messages.people(last.get("From"), last.get("To"), last.get("Cc"))
            if a.lower() not in skip]


def formatted(people: List[Tuple[str, str]]) -> str:
    from email.utils import formataddr
    return ", ".join(formataddr((name, addr)) if name else addr for name, addr in people)


def forwarded_text(message, received: str, note: str) -> str:
    """A forward's body: the person's words, then the message forwarded, as text."""
    text, _ = messages.body_and_attachments(message)
    head = ["---------- Forwarded message ----------",
            f"From: {messages.header(message, 'From')}",
            f"Date: {messages.sent_at(message, received)}",
            f"Subject: {messages.header(message, 'Subject')}",
            f"To: {messages.header(message, 'To')}"]
    if messages.header(message, "Cc"):
        head.append(f"Cc: {messages.header(message, 'Cc')}")
    return (note.rstrip() + "\n\n" if note.strip() else "") + "\n".join(head) + "\n\n" + text

