"""Where a mail account's servers are, and how this agent reaches them.

An account is an address and a password the person typed once — for
most providers an APP PASSWORD, made in the provider's own settings
for one program to use. The common providers are picked by name and
their servers are known here; any other is reached at the server names
the person gives.

Mail is read over IMAP on port 993 and sent over SMTP on 465, or on
587 where a provider offers only that. Both are encrypted from the
first byte or before the password is said (STARTTLS); this agent never
speaks to a mail server in the clear, except to the loopback servers
of its own tests.

Every connection is opened with the standard's Tunnel: through the
sandbox's way out, which opens the hosts and ports below, the ones the
manifest declares, and a server an account names. A server named in
the account is opened on the web's ports only, unless the port is
written with it (imap.example.com:993): so it is read with its port,
and connected to on that port, or on mail's own ports when none is
written.
"""

from __future__ import annotations

import os
import ssl
from typing import Any, Dict, List, Optional, Tuple

from standard import Tunnel, TunnelRefused

IMAP_PORT = 993
SMTP_PORTS = (465, 587)
SECONDS = 30

#: provider -> (imap host, smtp host, the ports its smtp answers on).
PROVIDERS: Dict[str, Tuple[str, str, Tuple[int, ...]]] = {
    "gmail":    ("imap.gmail.com",      "smtp.gmail.com",      (465, 587)),
    "yahoo":    ("imap.mail.yahoo.com", "smtp.mail.yahoo.com", (465, 587)),
    "icloud":   ("imap.mail.me.com",    "smtp.mail.me.com",    (587,)),
    "fastmail": ("imap.fastmail.com",   "smtp.fastmail.com",   (465, 587)),
}

#: Where a person makes an app password, said when a sign-in is refused.
APP_PASSWORDS = {
    "gmail": "Google needs an app password here, not the account's own: turn on "
             "2-Step Verification, then make one at "
             "https://myaccount.google.com/apppasswords",
    "yahoo": "Yahoo needs an app password here: Account security, Generate app password.",
    "icloud": "iCloud needs an app-specific password here: make one at "
              "https://account.apple.com under Sign-In and Security.",
    "fastmail": "Fastmail needs an app password here: Settings, Privacy & Security, "
                "App passwords, with access to mail.",
}


class MailError(Exception):
    """A failure the assistant can word: ``auth`` (the sign-in was
    refused), ``refused`` (the platform would not open the connection),
    ``network`` (the server did not answer), ``server`` (the server
    answered with an error), ``not_found``, ``unknown`` (a message was
    handed over and no answer came back: nobody knows whether it was
    sent)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Account:
    """One connected account: whose it is, its password, and where its
    servers are."""

    def __init__(self, secret: Dict[str, Any]):
        self.email = str(secret.get("account") or "").strip()
        self.password = str(secret.get("password") or "")
        self.provider = str(secret.get("provider") or "other").strip().lower()
        known = PROVIDERS.get(self.provider)
        imap_host, imap_port = self._server(secret.get("imap_host"))
        smtp_host, smtp_port = self._server(secret.get("smtp_host"))
        self.imap_host = imap_host or (known[0] if known else "")
        self.smtp_host = smtp_host or (known[1] if known else "")
        self.imap_port = imap_port or IMAP_PORT
        self.smtp_ports = ((smtp_port,) if smtp_port else
                           known[2] if known and not smtp_host else SMTP_PORTS)
        #: The servers the account names without a port: the way out opens those on the web's ports only.
        self.portless = {host for host, port in ((imap_host, imap_port), (smtp_host, smtp_port))
                         if host and not port}
        #: The tests' loopback servers are spoken to in the clear: said in the environment, which the
        #: runtime gives no program, so no account can turn encryption off.
        self.plain = os.environ.get("MAIL_TEST_PLAIN") == "1"

    @staticmethod
    def _server(value: Any) -> Tuple[str, Optional[int]]:
        """The name, and the port when one is written, in what a person
        typed: a name, a name and a port, or a whole address."""
        value = str(value or "").strip().lower()
        if "://" in value:
            value = value.split("://", 1)[1]
        host, _, port = value.split("/", 1)[0].partition(":")
        return host, int(port) if port.isdigit() else None

    def problems(self) -> List[str]:
        """Why this account cannot be used, empty when it can."""
        found = []
        if not self.email or "@" not in self.email:
            found.append("The account has no email address.")
        if not self.password:
            found.append("The account has no password.")
        if not self.imap_host:
            found.append("The account names no mail server: choose a provider, "
                         "or give the IMAP and SMTP servers with their ports.")
        return found

    def hint(self) -> str:
        return APP_PASSWORDS.get(
            self.provider,
            "Most providers need an app password here, made in the account's "
            "security settings, and not the account's own password.")

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def to_imap(self):
        """A connected, encrypted socket to the account's IMAP server."""
        link = self._open(self.imap_host, self.imap_port)
        return link if self.plain else self._encrypted(link, self.imap_host)

    def to_smtp(self):
        """(socket, port, encrypted): the first of the server's ports
        that could be opened. On 465 the socket is encrypted already;
        on 587 it is not yet, and STARTTLS follows the greeting."""
        last: Optional[MailError] = None
        for port in self.smtp_ports:
            try:
                link = self._open(self.smtp_host, port)
            except MailError as exc:
                last = exc
                continue
            if self.plain:
                return link, port, None
            if port == 465:
                return self._encrypted(link, self.smtp_host), port, True
            return link, port, False
        raise last or MailError("network", f"{self.smtp_host} could not be reached.")

    def _open(self, host: str, port: int):
        try:
            return Tunnel.open(host, port, timeout=SECONDS)
        except TunnelRefused as exc:
            if exc.status == 403:
                written = (f" A server named in the account is reached on the port written with it: "
                           f"give it as {host}:{port}." if host in self.portless else "")
                raise MailError("refused", f"The platform refused the connection "
                                           f"to {host}:{port}: {exc.reason.rstrip('.')}.{written}")
            raise MailError("network", f"{host} could not be reached on port "
                                       f"{port}: {exc.reason}")
        except OSError as exc:
            raise MailError("network", f"{host} could not be reached on port "
                                       f"{port}: {exc}")

    @staticmethod
    def context() -> ssl.SSLContext:
        return ssl.create_default_context()

    @classmethod
    def _encrypted(cls, link, host: str):
        try:
            return cls.context().wrap_socket(link, server_hostname=host)
        except (ssl.SSLError, OSError) as exc:
            link.close()
            raise MailError("network", f"{host} could not be spoken to safely: {exc}")
