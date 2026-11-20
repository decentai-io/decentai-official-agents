"""Sending, for chats and channels alike: one POST, recorded before it goes and after it answers, never sent twice.

1. An earlier send of the same words to the same place whose outcome is unknown (no answer came, or the call was
   ended while it waited) is looked for first: found in the chat, it is recorded as sent and not sent again; not
   found, the send is refused unless the person says to send it again.
2. A ``sent`` record is written with status ``sending``. Should the call be ended while Graph is asked, the record
   says so, and the next attempt treats it as unknown.
3. One POST. Graph's answer makes the record ``sent``; no answer makes it ``unknown`` (said, never retried); a
   refusal makes it ``failed`` (nothing was sent).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from standard.agent import ResourceDenied

from .client import HttpError, TeamsGraph, exact, is_message, segment, sender_id, text_of, utc

#: How long an earlier attempt of unknown outcome holds back the same words to the same place.
HELD_HOURS = 24


def digest(kind: str, place: str, parent: str, text: str) -> str:
    return hashlib.sha256(f"{kind}\n{place}\n{parent}\n{text.strip()}".encode("utf-8")).hexdigest()[:32]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def earlier_attempts(call: Any, mark: str) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=HELD_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [r for r in await call.records.list("sent", {"digest": mark})
            if r["fields"].get("status") in ("sending", "unknown")
            and str(r["fields"].get("attempted_at") or "") >= since]


async def landed(graph: TeamsGraph, where: dict[str, str], text: str, after: str) -> dict[str, Any] | None:
    """The account's own message with these words, made at or after ``after``, where it was sent; None when it is
    not there or can't be looked at."""
    if where["kind"] == "chat":
        path = f"chats/{segment(where['chat_id'])}/messages"
        params = {"$top": 50, "$orderby": "createdDateTime desc"}
    else:
        root = where["parent_id"] or ""
        if not root:
            return None  # a channel post is found only by reading the channel, which needs an admin's consent
        path = (f"teams/{segment(where['team_id'])}/channels/{segment(where['chat_id'])}/messages/"
                f"{segment(root)}/replies")
        params = {"$top": 50}
    try:
        found = (await graph.get(path, params=params)).get("value") or []
    except HttpError:
        return None
    me, wanted = await graph.my_id(), " ".join(text.split())
    earliest = (datetime.fromisoformat(after.replace("Z", "+00:00")) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ") if after else ""
    for message in found:
        if is_message(message) and sender_id(message) == me and utc(message.get("createdDateTime")) >= earliest \
                and " ".join(text_of(message.get("body")).split()) == wanted:
            return message
    return None


async def deliver(call: Any, graph: TeamsGraph, *, where: dict[str, str], to: str, text: str, path: str,
                  payload: dict[str, Any], again: bool) -> tuple[dict[str, Any], str]:
    """``where``: kind (chat, channel, reply), chat_id (a chat's or a channel's), team_id, parent_id."""
    mark = digest(where["kind"], where["chat_id"], where.get("parent_id", ""), text)
    place = {"chat_id": where["chat_id"], **({"team_id": where["team_id"]} if where.get("team_id") else {}),
             **({"parent_id": where["parent_id"]} if where.get("parent_id") else {})}
    earlier = await earlier_attempts(call, mark)
    if earlier and not again:
        first = earlier[-1]
        message = await landed(graph, where, text, str(first["fields"].get("attempted_at") or ""))
        if message is not None:
            made = await settle(call, first["ref"], "sent", message)
            return {"status": "sent", "already": True, "sent_ref": first["ref"], **place, **made,
                    "problem": "An earlier send of these words got no answer; it did arrive, so it was not sent "
                               "again."}, "success"
        return {"error": f"These exact words were sent here at {first['fields'].get('attempted_at')} and no answer "
                         f"came back, so whether they arrived is unknown, and they are not in the latest messages "
                         f"this agent can see. Check the chat in Teams first. To send them anyway, the person must "
                         f"say so: then call again with again: true.",
                "kind": "unknown", "sent_ref": first["ref"]}, "error"
    ref = await call.records.create("sent", {
        **{k: v for k, v in where.items() if v}, "status": "sending", "digest": mark, "attempted_at": now(),
        "account": graph.account.lower(), "to": to, "text": text})
    await call.progress(f"Sending to {to}")
    try:
        made = await graph.post(path, json=payload)
    except HttpError as failed:
        if failed.kind == "unknown":
            await quietly(call, ref, {"status": "unknown"})
            return {"status": "unknown", "sent_ref": ref, **place,
                    "problem": f"{failed.message} It is recorded as unknown: read the chat before sending it "
                               f"again."}, "success"
        await quietly(call, ref, {"status": "failed"})
        return failed.answer()
    kept = await settle(call, ref, "sent", made)
    return {"status": "sent", "sent_ref": ref, **place, **kept}, "success"


async def settle(call: Any, ref: str, status: str, message: dict[str, Any]) -> dict[str, Any]:
    made = {"message_id": str(message.get("id") or ""), "sent_at": exact(message.get("createdDateTime"))}
    made = {k: v for k, v in made.items() if v}
    link = str(message.get("webUrl") or "")
    problem = await quietly(call, ref, {"status": status, **made, **({"link": link} if link else {})})
    return {**made, **({"link": link} if link else {}), **({"problem": problem} if problem else {})}


async def quietly(call: Any, ref: str, fields: dict[str, Any]) -> str:
    """A record update that must not undo what Graph already did: its failure is said, not raised."""
    try:
        await call.records.update("sent", ref, fields)
    except ResourceDenied as failed:
        return f"It went to Teams, but its record could not be updated: {failed}"
    return ""
