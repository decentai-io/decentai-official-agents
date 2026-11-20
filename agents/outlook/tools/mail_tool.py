"""Reading mail whole, and keeping it in order: search, a folder's newest, one message, a conversation, attachments
saved into the chat, folders, and marking, moving and archiving."""

from __future__ import annotations

from typing import Any

from standard import ToolBase
from standard.graph import addresses
from standard.http import FILE_MOST, HttpError, human_size, segment

from .common import IMMUTABLE, TEXT, Refused, body_text, client, guarded, moment, told, utc_z, zone_for
from .mail import (FIELDS, attachment_kind, attachments, conversation, folder_id, folder_path, folders, mine, row,
                   thread_of)

#: A search can take Microsoft a while over a large mailbox: its own read timeout, longer than a lookup's.
SEARCH_TIMEOUT = 50
FLAGS = {"flagged": "flagged", "complete": "complete", "none": "notFlagged"}


class MailTool(ToolBase):
    id = "mail"

    @guarded
    async def find(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs, soft=True)
        most = int(call.inputs.get("max_results") or 10)
        token = str(call.inputs.get("page_token") or "")
        if token:
            page = await graph.get("", absolute=token, headers=TEXT, timeout=SEARCH_TIMEOUT)
        else:
            query = str(call.inputs["query"]).strip().replace('"', '\\"')
            folder, _ = await folder_id(graph, call.inputs.get("folder")) if call.inputs.get("folder") else ("", "")
            # $search stands alone: Graph takes no $filter, $orderby or $skip with it on messages.
            page = await graph.get(folder_path(folder) if folder else "me/messages", headers=TEXT,
                                   timeout=SEARCH_TIMEOUT,
                                   params={"$search": f'"{query}"', "$top": most, "$select": FIELDS})
        result = {"messages": [row(m, zone) for m in page.get("value") or []], **told(zone)}
        result["more"] = bool(page.get("@odata.nextLink"))
        if page.get("@odata.nextLink"):
            result["next_page_token"] = str(page["@odata.nextLink"])
        return result, "success"

    @guarded
    async def list(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs, soft=True)
        most = int(call.inputs.get("max_results") or 20)
        folder, name = await folder_id(graph, call.inputs.get("folder"))
        token = str(call.inputs.get("page_token") or "")
        if token:
            page = await graph.get("", absolute=token, headers=TEXT)
        else:
            # Ordered by receivedDateTime, so the filter must lead with it (Graph's InefficientFilter rule); with no
            # date asked for, it is one every message meets.
            since = call.inputs.get("since")
            parts = []
            if since or call.inputs.get("unread_only") or call.inputs.get("from"):
                start = utc_z(moment(since, zone, "since")) if since else "1900-01-01T00:00:00Z"
                parts.append(f"receivedDateTime ge {start}")
            if call.inputs.get("unread_only"):
                parts.append("isRead eq false")
            if call.inputs.get("from"):
                sender = str(call.inputs["from"]).strip().lower().replace("'", "''")
                parts.append(f"from/emailAddress/address eq '{sender}'")
            params = {"$orderby": "receivedDateTime desc", "$top": most, "$select": FIELDS}
            if parts:
                params["$filter"] = " and ".join(parts)
            page = await graph.get(folder_path(folder), params=params, headers=TEXT)
        result = {"folder": name, "messages": [row(m, zone) for m in page.get("value") or []],
                  "more": bool(page.get("@odata.nextLink")), **told(zone)}
        if page.get("@odata.nextLink"):
            result["next_page_token"] = str(page["@odata.nextLink"])
        return result, "success"

    @guarded
    async def read(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs, soft=True)
        message_id = str(call.inputs["message_id"])
        message = await graph.get(f"me/messages/{segment(message_id)}", headers=TEXT,
                                  params={"$select": FIELDS + ",body,bccRecipients,replyTo"})
        text = await body_text(message.get("body"))
        offset, most = int(call.inputs.get("offset") or 0), int(call.inputs.get("max_chars") or 20000)
        result = {**row(message, zone), "body": text[offset:offset + most], "body_chars": len(text),
                  "offset": offset, "internet_id": str(message.get("internetMessageId") or ""), **told(zone)}
        result.pop("snippet", None)
        if message.get("bccRecipients"):
            result["bcc"] = addresses(message["bccRecipients"])
        if offset + most < len(text):
            result["next_offset"] = offset + most
        result["attachments"] = await attachments(graph, message_id) if message.get("hasAttachments") else []
        return result, "success"

    @guarded
    async def thread(self, call):
        graph = await client(call)
        zone = await zone_for(graph, call.inputs, soft=True)
        me = await graph.me()
        thread_id = await thread_of(graph, call.inputs)
        quoted = bool(call.inputs.get("quoted"))
        found = await conversation(graph, thread_id, FIELDS + (",body" if quoted else ",uniqueBody"))
        if not found:
            raise Refused(f"Outlook has no conversation {thread_id} in this mailbox.", "not_found")
        start, offset = int(call.inputs.get("start") or 0), int(call.inputs.get("offset") or 0)
        budget = int(call.inputs.get("max_chars") or 30000)
        if start >= len(found):
            raise Refused(f"The conversation has {len(found)} messages; start {start} is past its end.")
        messages, attached, following = [], [], None
        for index in range(start, len(found)):
            message = found[index]
            # Each message's own words (uniqueBody), the history it quotes being the messages before it; its whole
            # body where Outlook gave no unique part, and with quoted.
            text = await body_text(message.get("body") if quoted else message.get("uniqueBody"))
            if not text and not quoted:
                whole = await graph.get(f"me/messages/{segment(message['id'])}", headers=TEXT,
                                        params={"$select": "id,body"})
                text = await body_text(whole.get("body"))
            begin = offset if index == start else 0
            if budget <= 0:
                following = {"start": index, "offset": 0}
                break
            piece = text[begin:begin + budget]
            budget -= len(piece)
            entry = {**row(message, zone), "index": index, "mine": mine(message, me), "body": piece,
                     "body_chars": len(text)}
            entry.pop("snippet", None)
            if begin:
                entry["offset"] = begin
            messages.append(entry)
            if message.get("hasAttachments"):
                attached.extend(await attachments(graph, str(message["id"])))
            if begin + len(piece) < len(text):
                following = {"start": index, "offset": begin + len(piece)}
                break
        result = {"thread_id": thread_id, "subject": str(found[0].get("subject") or ""),
                  "message_count": len(found), "messages": messages, "attachments": attached,
                  "complete": following is None, **told(zone)}
        if following:
            result["next"] = following
        return result, "success"

    @guarded
    async def save_attachment(self, call):
        graph = await client(call)
        message_id, attachment_id = str(call.inputs["message_id"]), str(call.inputs["attachment_id"])
        base = f"me/messages/{segment(message_id)}/attachments/{segment(attachment_id)}"
        info = await graph.get(base, params={"$select": "id,name,contentType,size,isInline"})
        kind, name = attachment_kind(info), str(info.get("name") or "attachment")
        size = int(info.get("size") or 0)
        if kind == "reference":
            link = (await graph.get(f"me/messages/{segment(message_id)}", params={"$select": "id,webLink"}))
            raise Refused(f"“{name}” is a link to a file in cloud storage, not a file held in the message: open it "
                          f"from the message ({link.get('webLink') or 'in Outlook'}).", "unsupported")
        if size > FILE_MOST:
            link = (await graph.get(f"me/messages/{segment(message_id)}", params={"$select": "id,webLink"}))
            raise Refused(f"“{name}” is {human_size(size)}; at most {human_size(FILE_MOST)} can be brought into the "
                          f"chat. Open it from the message: {link.get('webLink') or 'in Outlook'}.", "too_large",
                          size=size, limit=FILE_MOST)
        # The raw bytes ($value): a file as it is, an attached message as a .eml file.
        raw = await graph.download(f"{base}/$value", most=FILE_MOST, what=f"“{name}”",
                                   headers={"Prefer": IMMUTABLE})
        if kind == "item" and "." not in name[-6:]:
            name += ".eml"
        if not raw:
            raise Refused(f"Outlook returned no bytes for “{name}”.", "empty")
        await call.progress(f"Saving {name} ({human_size(len(raw))})")
        return {"file_ref": call.give(name, raw), "filename": name, "size": len(raw), "kind": kind}, "success"

    @guarded
    async def folders(self, call):
        graph = await client(call)
        found = await folders(graph)
        return {"folders": [{"folder_id": str(f.get("id") or ""), "name": str(f.get("displayName") or ""),
                             "path": str(f.get("path") or ""), "unread": int(f.get("unreadItemCount") or 0),
                             "total": int(f.get("totalItemCount") or 0)} for f in found]}, "success"

    @guarded
    async def mark(self, call):
        graph = await client(call)
        change: dict[str, Any] = {}
        if call.inputs.get("read") is not None:
            change["isRead"] = bool(call.inputs["read"])
        if call.inputs.get("flag"):
            change["flag"] = {"flagStatus": FLAGS[str(call.inputs["flag"])]}
        if not change:
            raise Refused("Nothing to change: give read (true or false) or flag (flagged, complete, none).")
        return await self._each(call, graph, lambda m: graph.patch(f"me/messages/{segment(m)}", json=change),
                                "marked")

    @guarded
    async def move(self, call):
        graph = await client(call)
        return await self._moved(call, graph, call.inputs["folder"])

    @guarded
    async def archive(self, call):
        graph = await client(call)
        return await self._moved(call, graph, "archive")

    async def _moved(self, call, graph, folder):
        destination, name = await folder_id(graph, folder)
        result, status = await self._each(call, graph, lambda m: graph.post(
            f"me/messages/{segment(m)}/move", json={"destinationId": destination}), "moved")
        if status == "error" and folder == "archive" and result.get("kind") == "not_found":
            raise Refused("This mailbox has no Archive folder: move the mail to a folder by name with mail_move.",
                          "not_found")
        if status == "success":
            result["folder"] = name
        return result, status

    @staticmethod
    async def _each(call, graph, act, done_key):
        ids = [str(m) for m in call.inputs["message_ids"]]
        done, failed = [], []
        for message_id in ids:
            try:
                answer = await act(message_id)
            except HttpError as refused:
                if refused.kind in ("auth", "scope"):
                    raise
                failed.append({"message_id": message_id, "error": refused.message, "kind": refused.kind})
                continue
            done.append(str((answer or {}).get("id") or message_id))
        if not done:
            return {"error": f"None of the {len(ids)} messages could be changed: {failed[0]['error']}",
                    "kind": failed[0]["kind"], "failed": failed}, "error"
        return {done_key: done, "failed": failed}, "success"
