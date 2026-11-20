"""Microsoft Graph, for the Microsoft agents: the HTTP of ``http.py`` with Graph's own ways.

- Every request asks for **immutable ids**, so a message keeps its id when Outlook moves it (Drafts to Sent Items).
- **Who the account is:** its mail address and its sign-in name (UPN) can differ in a work tenant, and either may
  appear on its own messages; ``Me.mine`` holds every address that is the account's.
- **Pages** follow ``@odata.nextLink``, which is checked to be Graph's own.
- Graph's **InefficientFilter** rule: a ``$filter`` with an ``$orderby`` must lead with the ordered property, and
  most collections refuse a filter on another property with an order at all. Filter, then sort in code
  (``sorted_by``), unless the filter is on the ordered property.
- **Work-only features** (Teams, getSchedule, organization links, SharePoint) fail on a personal account; ``personal``
  tells them apart so the refusal can say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import Any

import requests

from .accounts import account
from .http import Http, HttpError, segment

GRAPH = "https://graph.microsoft.com/v1.0"
#: The tenant id Microsoft gives personal accounts (outlook.com, hotmail.com, live.com).
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"


class Graph(Http):
    SERVICE = "Microsoft"
    BASE = GRAPH

    def __init__(self, secret: dict[str, Any]):
        super().__init__(str(secret.get("access_token") or ""), api_base_url=secret.get("api_base_url"))
        self.account = str(secret.get("account") or "")
        self._me: Me | None = None

    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"Prefer": 'IdType="ImmutableId"'}

    def detail(self, response: requests.Response) -> str:
        words = super().detail(response)
        try:
            code = str(((response.json() or {}).get("error") or {}).get("code") or "")
        except (ValueError, AttributeError):
            code = ""
        return f"{words} ({code})" if code and code not in words else words

    def scope_refused(self, response: requests.Response, words: str) -> bool:
        lowered = words.lower()
        return response.status_code == 403 and any(w in lowered for w in (
            "scope", "insufficient privileges", "accessdenied", "access is denied", "erroraccessdenied",
            "authorization_requestdenied"))

    async def me(self) -> "Me":
        """Who the connected account is: asked once per client."""
        if self._me is None:
            profile = await self.get("me", params={"$select": "id,mail,userPrincipalName,displayName,"
                                                              "otherMails,proxyAddresses"})
            self._me = Me.of(profile, self.account)
        return self._me

    async def personal(self) -> bool:
        """Whether this is a personal Microsoft account, where work-only features don't exist. A work or school
        account's access token is a JWT naming its tenant; a personal account's is not a JWT, or names Microsoft's
        consumer tenant."""
        tenant = self.tenant()
        if tenant:
            return tenant == PERSONAL_TENANT
        return (await self.me()).personal or not self.token.count(".") == 2

    def tenant(self) -> str:
        import base64
        import json

        parts = self.token.split(".")
        if len(parts) != 3:
            return ""
        try:
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        except (ValueError, TypeError):
            return ""
        return str(claims.get("tid") or "") if isinstance(claims, dict) else ""


@dataclass
class Me:
    id: str = ""
    mail: str = ""
    upn: str = ""
    name: str = ""
    mine: set[str] = field(default_factory=set)
    personal: bool = False

    @classmethod
    def of(cls, profile: dict[str, Any], connected: str = "") -> "Me":
        mail = str(profile.get("mail") or "").strip()
        upn = str(profile.get("userPrincipalName") or "").strip()
        mine = {a.lower() for a in [mail, upn, connected, *(profile.get("otherMails") or [])] if a}
        mine |= {str(p).split(":", 1)[-1].lower() for p in profile.get("proxyAddresses") or [] if ":" in str(p)}
        domain = (mail or upn or connected).rsplit("@", 1)[-1].lower()
        personal = domain in PERSONAL_DOMAINS or "#ext#" in upn.lower() and not mail
        return cls(str(profile.get("id") or ""), mail or upn or connected, upn, str(profile.get("displayName") or ""),
                   mine, personal)

    @property
    def address(self) -> str:
        return self.mail or self.upn


PERSONAL_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com", "passport.com", "outlook.sa", "hotmail.co.uk",
                    "live.co.uk", "outlook.ae", "hotmail.fr", "live.fr", "outlook.fr"}


async def graph_for(call: Any, wanted: Any = None, ask: bool = True) -> tuple[Graph | None, str]:
    """The Graph client over the connected Microsoft account wanted, or None and why."""
    secret, why = await account(call, "microsoft", wanted, ask, "Microsoft")
    return (Graph(secret), "") if secret is not None else (None, why)


# -- what Graph writes, read --------------------------------------------------------------------------------------

def address(entry: Any) -> str:
    """One recipient as 'Name <address>' or the bare address."""
    mailbox = (entry or {}).get("emailAddress") or {}
    name, addr = str(mailbox.get("name") or "").strip(), str(mailbox.get("address") or "").strip()
    if name and addr and name.lower() != addr.lower():
        return f"{name} <{addr}>"
    return addr or name


def addresses(entries: Any) -> str:
    return ", ".join(a for a in (address(e) for e in entries or []) if a)


def bare(entry: Any) -> str:
    return str(((entry or {}).get("emailAddress") or {}).get("address") or "").strip().lower()


def recipients(text: Any) -> list[dict[str, Any]]:
    """'a@x, Name <b@y>' (or a list of them) → Graph recipient objects, each address once."""
    texts = text if isinstance(text, (list, tuple)) else [str(text or "")]
    seen, found = set(), []
    # Outlook's own habit is to separate addresses with ";".
    for name, addr in getaddresses([str(t).replace(";", ",") for t in texts]):
        if addr and "@" in addr and addr.lower() not in seen:
            seen.add(addr.lower())
            found.append({"emailAddress": {"address": addr, **({"name": name} if name else {})}})
    return found


def utc(text: Any) -> str:
    """An ISO 8601 date-time as Graph compares one: UTC, to the second, Z. "" when it is not one. A naive time is UTC.
    Graph writes seven fraction digits, which Python reads only up to six."""
    import re

    text = str(text or "").strip()
    if not text:
        return ""
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sorted_by(items: list[dict[str, Any]], key: str, newest_first: bool = False) -> list[dict[str, Any]]:
    """Items ordered by a date-time property in code, where Graph may not order a filtered collection."""
    return sorted(items, key=lambda i: utc(i.get(key)) or "", reverse=newest_first)


__all__ = ["GRAPH", "Graph", "HttpError", "Me", "address", "addresses", "bare", "graph_for", "recipients", "segment",
           "sorted_by", "utc"]
