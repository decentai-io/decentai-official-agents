"""Microsoft Teams over Graph, for this agent: the shared library's ``Graph``, held to a time that fits a call.

- **Work or school accounts only.** Teams has no Graph API for a personal Microsoft account; that is said before
  any request is made (the access token names its tenant), never as a refusal Graph words obscurely.
- **A send is one POST.** Nothing is read back after it and nothing is sent again: an answer that does not come
  is ``unknown``, and recorded so.
- **Times fit the call.** Every request has its own timeout, and the waits for a busy Graph (429, Retry-After) are
  capped, so a function answers before the runtime's clock does.
- **Pages** follow Graph's ``@odata.nextLink``, which is checked to be Graph's own before it is followed.
- Message bodies come back as HTML; they are read as text here.
"""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime, timezone
from typing import Any

from standard.accounts import account
from standard.graph import GRAPH, Graph, utc
from standard.http import HttpError, segment

WORK_ONLY = ("Teams needs a work or school account (Microsoft 365). The connected account {who} is a personal "
             "Microsoft account, which has no Teams chats or channels that an app may read or send to. Connect a "
             "work or school account to use this agent.")

#: Graph's largest page for chats and chat messages.
PAGE = 50


class TeamsGraph(Graph):
    """Graph with times that fit this agent's calls: a read waits at most 15 s for an answer and is tried 3 times;
    a write waits 30 s, once (twice only when Graph said 429 or 503, which means it did nothing); the waits for a
    busy Graph come to at most 10 s in a call."""

    READ_TIMEOUT = 15
    WRITE_TIMEOUT = 30
    ATTEMPTS = 3
    WAIT_MOST = 10.0

    def claims(self) -> dict[str, Any]:
        parts = self.token.split(".")
        if len(parts) != 3:
            return {}
        try:
            found = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        except (ValueError, TypeError):
            return {}
        return found if isinstance(found, dict) else {}

    async def my_id(self) -> str:
        """The account's own user id: from the token where it says so, else asked of Graph once."""
        oid = str(self.claims().get("oid") or "")
        return oid or (await self.me()).id


async def client_for(call: Any, ask: bool = True, account_name: Any = None) -> tuple[TeamsGraph | None, dict | None]:
    """The client over the account wanted, or None and the error result. A personal account is refused here,
    before any request to Graph."""
    secret, why = await account(call, "microsoft", account_name, ask, "Microsoft")
    if secret is None:
        return None, {"error": why, "kind": "auth"}
    graph = TeamsGraph(secret)
    try:
        if await graph.personal():
            return None, {"error": WORK_ONLY.format(who=graph.account or "here"), "kind": "personal"}
    except HttpError as failed:
        return None, failed.answer()[0]
    return graph, None


def exact(text: Any) -> str:
    """A Graph date-time to the microsecond, UTC, Z: what is compared and kept as a cursor, so two messages in one
    second are told apart. "" when it is not one."""
    text = re.sub(r"(\.\d{6})\d+", r"", str(text or "").strip())
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return ""
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# -- what Teams writes, read ---------------------------------------------------------------------------------------

BREAK_RE = re.compile(r"<br\s*/?>|</p>|</div>|</li>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def text_of(body: Any) -> str:
    content = str((body or {}).get("content") or "")
    if str((body or {}).get("contentType") or "").lower() == "html":
        content = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", content)
        content = BREAK_RE.sub("\n", content)
        content = html.unescape(TAG_RE.sub("", content))
    content = "\n".join(line.rstrip() for line in content.replace("\r\n", "\n").split("\n"))
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def is_message(message: dict[str, Any]) -> bool:
    """A person's message: not a system notice, not deleted."""
    return str(message.get("messageType") or "message") == "message" and not message.get("deletedDateTime")


def sender_id(message: dict[str, Any]) -> str:
    return str(((message.get("from") or {}).get("user") or {}).get("id") or "")


def members_of(chat: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Member user ids to (email, name)."""
    return {str(m.get("userId") or ""): (str(m.get("email") or ""), str(m.get("displayName") or ""))
            for m in chat.get("members") or []}


def chat_row(chat: dict[str, Any], me: str) -> dict[str, Any]:
    others = [m for m in chat.get("members") or [] if str(m.get("userId") or "") != me]
    names = [str(m.get("displayName") or m.get("email") or "") for m in others]
    preview = chat.get("lastMessagePreview") or {}
    row = {"chat_id": str(chat.get("id") or ""), "kind": str(chat.get("chatType") or ""),
           "topic": str(chat.get("topic") or "") or ", ".join(n for n in names if n),
           "members": ", ".join(str(m.get("email") or m.get("displayName") or "") for m in others
                                if m.get("email") or m.get("displayName")),
           "link": str(chat.get("webUrl") or "")}
    if preview.get("createdDateTime"):
        row["last_message_at"] = utc(preview.get("createdDateTime"))
        row["last_message"] = text_of(preview.get("body"))[:200]
        row["last_from_me"] = bool(sender_id(preview)) and sender_id(preview) == me
        row["unread"] = unread(chat, me)
    return row


def unread(chat: dict[str, Any], me: str) -> bool:
    """Whether the chat's last message came after the account last read it, and from someone else."""
    preview = chat.get("lastMessagePreview") or {}
    last = exact(preview.get("createdDateTime"))
    read = exact(((chat.get("viewpoint") or {}).get("lastMessageReadDateTime")))
    if not last or preview.get("isDeleted") or str(preview.get("messageType") or "message") != "message":
        return False
    return sender_id(preview) != me and (not read or last > read)


def message_row(message: dict[str, Any], members: dict[str, tuple[str, str]], me: str,
                most: int = 4000) -> dict[str, Any]:
    user = (message.get("from") or {}).get("user") or {}
    user_id = str(user.get("id") or "")
    email, name = members.get(user_id, ("", ""))
    text = text_of(message.get("body"))
    row = {"message_id": str(message.get("id") or ""),
           "from": email or str(user.get("displayName") or ""),
           "from_name": name or str(user.get("displayName") or ""),
           "sent": utc(message.get("createdDateTime")),
           "text": text[:most],
           "mine": bool(user_id) and user_id == me}
    if len(text) > most:
        row["truncated"] = True
    if message.get("subject"):
        row["subject"] = str(message["subject"])
    if message.get("attachments"):
        row["attachments"] = [str(a.get("name") or a.get("contentType") or "") for a in message["attachments"]]
    return row


# -- what is sent ------------------------------------------------------------------------------------------------

def body_for(text: str, mentioned: list[dict[str, str]]) -> dict[str, Any]:
    """A chatMessage's body and mentions. Plain text goes as text. With mentions it goes as HTML, each person's
    first "@Name" (or "@first name", or "@address") in the text made the mention, or the mention put first."""
    if not mentioned:
        return {"body": {"contentType": "text", "content": text}}
    pieces: list[tuple[str, int | None]] = [(text, None)]
    for number, person in enumerate(mentioned):
        name = person["name"]
        first = name.split()[0] if name.split() else name
        placed = False
        for written in [f"@{name}", f"@{person['email']}" if person["email"] else "", f"@{first}"]:
            if not written or placed:
                continue
            for index, (piece, owner) in enumerate(pieces):
                at = piece.lower().find(written.lower()) if owner is None else -1
                if at >= 0:
                    pieces[index:index + 1] = [(piece[:at], None), (name, number),
                                               (piece[at + len(written):], None)]
                    placed = True
                    break
        if not placed:
            pieces[0:0] = [(name, number), (" ", None)]
    content = "".join(f'<at id="{owner}">{html.escape(piece)}</at>' if owner is not None
                      else html.escape(piece).replace("\n", "<br>") for piece, owner in pieces)
    return {"body": {"contentType": "html", "content": content},
            "mentions": [{"id": number, "mentionText": person["name"],
                          "mentioned": {"user": {"id": person["id"], "displayName": person["name"],
                                                 "userIdentityType": "aadUser"}}}
                         for number, person in enumerate(mentioned)]}


def pick_people(asked: list[str], members: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str]:
    """The members named (by address, else whole name, else a part of a name that fits one person), or why not."""
    people = [{"id": str(m.get("userId") or ""), "email": str(m.get("email") or ""),
               "name": str(m.get("displayName") or m.get("email") or "")} for m in members if m.get("userId")]
    found, problems = [], []
    for wanted in asked:
        key = str(wanted).strip().lower()
        if not key:
            continue
        same = [p for p in people if p["email"].lower() == key] or [p for p in people if p["name"].lower() == key]
        partial = same or [p for p in people if key in p["name"].lower() or key in p["email"].lower()]
        unique = {p["id"]: p for p in partial}
        if len(unique) == 1:
            person = next(iter(unique.values()))
            if person["id"] not in {f["id"] for f in found}:
                found.append(person)
        elif not unique:
            problems.append(f"{wanted} is not among them")
        else:
            problems.append(f"{wanted} could be {', '.join(p['email'] or p['name'] for p in unique.values())}")
    return found, "; ".join(problems)


__all__ = ["GRAPH", "PAGE", "exact", "TeamsGraph", "HttpError", "body_for", "chat_row", "client_for", "is_message",
           "members_of", "message_row", "pick_people", "segment", "sender_id", "text_of", "unread", "utc"]
