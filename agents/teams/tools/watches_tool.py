"""New-message watches: one chat, or every chat; checked on a schedule, each new message from someone else handed
on once (standard.watch keeps the cursors; a chat that is gone closes its watch)."""

from __future__ import annotations

from standard import ResourceDenied, ToolBase
from standard.watch import Seen, check, close, listing

from .chats_tool import chats_page, new_messages, preview_at
from .client import HttpError, client_for, exact, is_message, members_of, message_row, segment, sender_id

#: The time a run may spend before leaving watches to the next one (the function's timeout is 120 s).
BUDGET = 75.0


class WatchesTool(ToolBase):
    id = "watches"

    async def start(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        chat_id = str(call.inputs.get("chat_id") or "").strip()
        try:
            if chat_id:
                # Graph's own time of the chat's newest message is where the watch starts: no clock but Graph's.
                newest = await graph.get(f"chats/{segment(chat_id)}/messages",
                                         params={"$top": 1, "$orderby": "createdDateTime desc"})
                times = [exact(m.get("createdDateTime")) for m in newest.get("value") or []]
            else:
                page = await chats_page(graph, top=1)
                times = [preview_at(c)
                         for c in page.get("value") or []]
        except HttpError as failed:
            return failed.answer()
        from .sending import now
        cursor = max([t for t in times if t] or [exact(now())])
        ref = await call.records.create("watch", {
            "kind": "chat" if chat_id else "chats", **({"chat_id": chat_id} if chat_id else {}),
            "status": "active", "cursor": cursor, "account": graph.account.lower(),
            "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": ref, "kind": "chat" if chat_id else "chats", "since": cursor}, "success"

    async def check(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            watches = [await call.records.read("watch", wanted)]
        else:
            watches = await call.records.list("watch", {"status": "active"})
        watches = [w for w in watches if str(w["fields"].get("status") or "active") == "active"]
        clients: dict[str, tuple] = {}

        async def read(watch):
            fields = watch["fields"]
            named = str(fields.get("account") or "")
            if named not in clients:
                clients[named] = await client_for(call, ask=False, account_name=named)
            graph, refused = clients[named]
            if graph is None:
                raise HttpError(refused.get("kind") or "auth", refused["error"])
            me, cursor = await graph.my_id(), str(fields.get("cursor") or "")
            if fields.get("kind") == "chat":
                chats = [await graph.get(f"chats/{segment(fields['chat_id'])}", params={"$expand": "members"})]
            else:
                chats, answer, pages = [], await chats_page(graph), 1
                while True:
                    page = answer.get("value") or []
                    chats += [c for c in page
                              if preview_at(c) > cursor]
                    following = str(answer.get("@odata.nextLink") or "")
                    oldest = min((preview_at(c) for c in page),
                                 default="")
                    if not following or (oldest and oldest <= cursor) or pages >= 10:
                        break
                    answer = await chats_page(graph, absolute=following)
                    pages += 1
            keyed, newest, more = [], cursor, False
            for chat in chats:
                try:
                    found, cut = await new_messages(graph, str(chat["id"]), cursor)
                except HttpError as failed:
                    if failed.kind == "not_found" and fields.get("kind") == "chats":
                        continue  # a chat left since: the other chats are still watched
                    raise
                more = more or cut
                members = members_of(chat)
                topic = str(chat.get("topic") or "") or ", ".join(
                    name for user, (_, name) in members.items() if user != me and name)
                for message in found:
                    made = exact(message.get("createdDateTime"))
                    newest = max(newest, made)
                    if is_message(message) and sender_id(message) != me:
                        keyed.append((made, {"watch_ref": watch["ref"], "chat_id": str(chat["id"]), "topic": topic,
                                             **message_row(message, members, me, most=1000)}))
            rows = [row for _, row in sorted(keyed, key=lambda pair: pair[0])]
            return Seen(rows, {"cursor": newest} if newest != cursor else None, more=more)

        found = await check(call, "watch", watches, read, budget=BUDGET)
        return {"checked": found["checked"], "messages": found["rows"], "problems": found["problems"],
                "closed": found["closed"], "more": found["more"]}, "success"

    async def list(self, call):
        found = await listing(call, "watch", lambda r: {
            "kind": str(r["fields"].get("kind") or ""), "chat_id": str(r["fields"].get("chat_id") or ""),
            "account": str(r["fields"].get("account") or ""), "since": str(r["fields"].get("cursor") or "")},
            include_closed=bool(call.inputs.get("include_closed")))
        return {"watches": found}, "success"

    async def close(self, call):
        try:
            return await close(call, "watch", str(call.inputs["watch_ref"])), "success"
        except ResourceDenied:
            return {"error": f"No watch {call.inputs['watch_ref']}.", "kind": "not_found"}, "error"
