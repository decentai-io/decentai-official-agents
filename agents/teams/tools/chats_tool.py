"""Chats: listed most recent first, read a page at a time, sent to on the person's go-ahead, what was missed, and
whether anyone answered."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from standard import ResourceDenied, ToolBase

from .client import (GRAPH, PAGE, HttpError, TeamsGraph, body_for, chat_row, client_for, exact, is_message,
                     members_of, message_row, pick_people, segment, sender_id, unread)
from .sending import deliver

ORDER = "lastMessagePreview/createdDateTime desc"
#: How many pages of chats a search by person reads at most.
WITH_PAGES = 10
#: How far back chats_replies reads a chat for answers: 10 pages of 50.
REPLY_PAGES = 10


async def chats_page(graph: TeamsGraph, top: int = PAGE, absolute: str = "") -> dict:
    """One page of chats, most recent message first, with members and the last message's preview. Should Graph
    refuse the two expansions together, the page is asked again with the preview alone."""
    if absolute:
        return await graph.get("", absolute=absolute)
    params = {"$expand": "members,lastMessagePreview", "$orderby": ORDER, "$top": top}
    try:
        return await graph.get("me/chats", params=params)
    except HttpError as failed:
        if failed.kind != "invalid":
            raise
        return await graph.get("me/chats", params={**params, "$expand": "lastMessagePreview"})


def preview_at(chat: dict) -> str:
    return exact((chat.get("lastMessagePreview") or {}).get("createdDateTime"))


async def new_messages(graph: TeamsGraph, chat_id: str, after: str, pages: int = 4) -> tuple[list[dict], bool]:
    """A chat's messages made after ``after`` (exact UTC), oldest first, and whether there were more than were read.
    Graph filters chat messages by lastModifiedDateTime with gt, ordered by the same property (it ignores a filter on
    another); edits of older messages come too, and are left out here by their creation time."""
    params = {"$top": PAGE, "$orderby": "lastModifiedDateTime desc",
              "$filter": f"lastModifiedDateTime gt {after}"}
    answer = await graph.get(f"chats/{segment(chat_id)}/messages", params=params)
    found, read, cut = {}, 1, False
    while True:
        found.update({str(m.get("id")): m for m in answer.get("value") or []
                      if exact(m.get("createdDateTime")) > after})
        following = str(answer.get("@odata.nextLink") or "")
        if not following:
            break
        if read >= pages:
            cut = True
            break
        answer = await graph.get("", absolute=following)
        read += 1
    return sorted(found.values(), key=lambda m: exact(m.get("createdDateTime"))), cut


def even(most: int) -> int:
    """A page size that reads ``most`` in as few pages as Graph allows, the pages even: 120 is 3 pages of 40."""
    pages = -(-most // PAGE)
    return -(-most // pages)


def since_hours(hours: int) -> str:
    return exact((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat())


class ChatsTool(ToolBase):
    id = "chats"

    async def list(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        person = str(call.inputs.get("with") or "").strip().lower()
        most = int(call.inputs.get("max_results") or 20)
        token = str(call.inputs.get("page_token") or "")
        try:
            me = await graph.my_id()
            answer = await chats_page(graph, PAGE if person else most, token)
            rows, read = [], 1
            while True:
                rows += [chat_row(c, me) for c in answer.get("value") or []]
                following = str(answer.get("@odata.nextLink") or "")
                if person:
                    rows = [r for r in rows if person in r["members"].lower() or person in r["topic"].lower()]
                if not person or not following or len(rows) >= most or read >= WITH_PAGES:
                    break
                answer = await chats_page(graph, absolute=following)
                read += 1
        except HttpError as failed:
            return failed.answer()
        result = {"chats": rows[:most]}
        if following:
            result["next_page_token"] = following
        return result, "success"

    async def read(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        chat_id = str(call.inputs["chat_id"])
        most = int(call.inputs.get("max_results") or 30)
        token = str(call.inputs.get("page_token") or "")
        try:
            me = await graph.my_id()
            chat = await graph.get(f"chats/{segment(chat_id)}", params={"$expand": "members"})
            if token:
                answer = await graph.get("", absolute=token)
            else:
                answer = await graph.get(f"chats/{segment(chat_id)}/messages",
                                         params={"$top": even(most), "$orderby": "createdDateTime desc"})
            found = []
            while True:
                found += answer.get("value") or []
                following = str(answer.get("@odata.nextLink") or "")
                if not following or len(found) >= most:
                    break
                answer = await graph.get("", absolute=following)
        except HttpError as failed:
            return failed.answer()
        members = members_of(chat)
        found.sort(key=lambda m: exact(m.get("createdDateTime")))
        rows = [message_row(m, members, me) for m in found if is_message(m)]
        result = {"chat_id": chat_id, "topic": chat_row(chat, me)["topic"], "messages": rows}
        if following:
            result["next_page_token"] = following
        return result, "success"

    async def send(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        text = str(call.inputs["text"]).strip()
        chat_id = str(call.inputs.get("chat_id") or "").strip()
        to = str(call.inputs.get("to") or "").strip()
        asked = [str(m) for m in call.inputs.get("mention") or []]
        if not text:
            return {"error": "There is nothing to send.", "kind": "invalid"}, "error"
        if bool(chat_id) == bool(to):
            return {"error": "Give chat_id (from chats_list), or to: one person's email address for their one-to-one "
                             "chat; not both.", "kind": "invalid"}, "error"
        if to and ("@" not in to or " " in to):
            return {"error": f"to is an email address; {to!r} is not one. Find the person's address first "
                             f"(chats_list with 'with' finds chats by name).", "kind": "invalid"}, "error"
        try:
            if to:
                me = await graph.my_id()
                # Graph answers with the existing one-to-one chat when there is one, and makes none twice.
                chat = await graph.post("chats", json={"chatType": "oneOnOne", "members": [
                    {"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": ["owner"],
                     "user@odata.bind": f"{GRAPH}/users('{who}')"} for who in (me, to)]})
                chat_id = str(chat.get("id") or "")
            mentioned = []
            if asked:
                members = (await graph.get(f"chats/{segment(chat_id)}/members")).get("value") or []
                mentioned, problem = pick_people(asked, members)
                if problem:
                    return {"error": f"A mention must be someone in the chat: {problem}. Nothing was sent.",
                            "kind": "invalid"}, "error"
        except HttpError as failed:
            if failed.kind == "unknown":
                return {"error": f"The one-to-one chat with {to} could not be confirmed ({failed.message}). The "
                                 f"message itself was not sent.", "kind": "unknown"}, "error"
            if failed.kind == "not_found" and to:
                return {"error": f"Teams has no person {to} that this account can chat with. Nothing was sent.",
                        "kind": "not_found"}, "error"
            return failed.answer()
        where = {"kind": "chat", "chat_id": chat_id, "team_id": "", "parent_id": ""}
        return await deliver(call, graph, where=where, to=to or chat_id, text=text,
                             path=f"chats/{segment(chat_id)}/messages", payload=body_for(text, mentioned),
                             again=bool(call.inputs.get("again")))

    async def replies(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        ref = str(call.inputs["sent_ref"])
        try:
            sent = (await call.records.read("sent", ref))["fields"]
        except ResourceDenied:
            return {"error": f"No sent message {ref}.", "kind": "not_found"}, "error"
        if sent.get("status") != "sent":
            return {"error": f"That message is {sent.get('status')}, not known to be sent: there is nothing to look "
                             f"for answers to.", "kind": "invalid"}, "error"
        after = exact(sent.get("sent_at") or sent.get("attempted_at"))
        try:
            me = await graph.my_id()
            if sent.get("kind") == "chat":
                rows = await self._chat_answers(graph, str(sent.get("chat_id")), str(sent.get("message_id")),
                                                after, me)
            else:
                from .channels_tool import read_replies
                root = str(sent.get("parent_id") or sent.get("message_id") or "")
                found = await read_replies(graph, str(sent.get("team_id")), str(sent.get("chat_id")), root)
                # Every reply to a post answers it; in a thread this agent replied in, those after its reply do.
                rows = [message_row(m, {}, me) for m in found
                        if is_message(m) and sender_id(m) != me
                        and (sent.get("kind") == "channel" or exact(m.get("createdDateTime")) > after)]
        except HttpError as failed:
            return failed.answer()
        return {"replied": bool(rows), "chat_id": str(sent.get("chat_id") or ""), "replies": rows}, "success"

    @staticmethod
    async def _chat_answers(graph, chat_id, message_id, after, me):
        """Messages from others after the one sent, read back page by page (50 a page), newest first, until the
        message itself (or its time, should it have been deleted)."""
        chat = await graph.get(f"chats/{segment(chat_id)}", params={"$expand": "members"})
        answer = await graph.get(f"chats/{segment(chat_id)}/messages",
                                 params={"$top": PAGE, "$orderby": "createdDateTime desc"})
        later, read, reached = [], 1, False
        while not reached:
            for message in answer.get("value") or []:
                if str(message.get("id")) == message_id or exact(message.get("createdDateTime")) <= after:
                    reached = True
                    break
                later.append(message)
            following = str(answer.get("@odata.nextLink") or "")
            if reached or not following or read >= REPLY_PAGES:
                break
            answer = await graph.get("", absolute=following)
            read += 1
        members = members_of(chat)
        later.sort(key=lambda m: exact(m.get("createdDateTime")))
        return [message_row(m, members, me) for m in later if is_message(m) and sender_id(m) != me]

    async def missed(self, call):
        """Chats with messages from others since a time, read here: unread ones by default."""
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        hours = int(call.inputs.get("hours") or 24)
        only_unread = call.inputs.get("unread_only", True) is not False
        most = int(call.inputs.get("max_chats") or 10)
        since = since_hours(hours)
        try:
            me = await graph.my_id()
            changed, answer, read = [], await chats_page(graph), 1
            while True:
                page = answer.get("value") or []
                for chat in page:
                    if preview_at(chat) > since and (unread(chat, me) if only_unread else
                                                     sender_id(chat.get("lastMessagePreview") or {}) != me):
                        changed.append(chat)
                oldest = min((preview_at(c) for c in page), default="")
                following = str(answer.get("@odata.nextLink") or "")
                if not following or (oldest and oldest <= since) or read >= 5:
                    break
                answer = await chats_page(graph, absolute=following)
                read += 1
            chats = []
            for chat in changed[:most]:
                read_at = exact((chat.get("viewpoint") or {}).get("lastMessageReadDateTime"))
                start = max(since, read_at) if only_unread and read_at else since
                found, more = await new_messages(graph, str(chat["id"]), start, pages=2)
                members = members_of(chat)
                rows = [message_row(m, members, me, most=500) for m in found
                        if is_message(m) and sender_id(m) != me]
                if not rows:
                    continue
                chats.append({**{k: v for k, v in chat_row(chat, me).items()
                                 if k in ("chat_id", "kind", "topic", "link")},
                              "new_count": len(rows), "more_than_counted": more, "messages": rows[-20:]})
        except HttpError as failed:
            return failed.answer()
        return {"since": since, "unread_only": only_unread, "chats": chats,
                "chats_not_read": max(0, len(changed) - most)}, "success"
