"""Drafts: prepared in Outlook (a new message, a reply or reply-all that keeps the quoted history, a forward), chat
files attached, recorded on the platform, and sent only on a separate level-3 call that Outlook confirms.

Graph's send answers 202 and nothing more, so a send is confirmed by reading the same message back: with immutable
ids the draft keeps its id on its way to Sent Items, and ``isDraft`` turning false is Outlook saying it went."""

from __future__ import annotations

import asyncio
from typing import Any

from standard import ToolBase
from standard.graph import addresses, bare, recipients
from standard.http import HttpError, segment

from .common import Refused, client, guarded
from .mail import FIELDS, attach_files, conversation, mine, received


class DraftTool(ToolBase):
    id = "draft"

    #: How long a send is read back: a handful of reads, which covers the ordinary case and still answers a stuck
    #: one well inside the function's timeout.
    CONFIRM_ATTEMPTS = 10
    CONFIRM_PAUSE = 0.75

    @guarded
    async def compose(self, call):
        graph = await client(call)
        to, cc, bcc = (recipients(call.inputs.get(k) or "") for k in ("to", "cc", "bcc"))
        if not to:
            raise Refused(f"to names no address: {call.inputs.get('to')!r}.")
        html = str(call.inputs.get("format") or "text") == "html"
        draft = await graph.post("me/messages", json={
            "subject": str(call.inputs["subject"]), "toRecipients": to, "ccRecipients": cc, "bccRecipients": bcc,
            "body": {"contentType": "html" if html else "text", "content": str(call.inputs["body"])}})
        return await self._finish(call, graph, draft, "new", "")

    @guarded
    async def reply(self, call):
        graph = await client(call)
        me = await graph.me()
        everyone = bool(call.inputs.get("all"))
        source = await self._source(graph, call.inputs, me)
        verb = "createReplyAll" if everyone else "createReply"
        # Outlook writes the reply: the Re: subject, the conversation kept, and the person's words above the quoted
        # history (``comment``), never in place of it.
        draft = await graph.post(f"me/messages/{segment(source['id'])}/{verb}",
                                 json={"comment": str(call.inputs["body"])})
        change: dict[str, Any] = {}
        if call.inputs.get("to"):
            change["toRecipients"] = recipients(call.inputs["to"])
        elif mine(source, me):
            # Answering our own last message: Outlook would address it to us; it goes to those we wrote to.
            change["toRecipients"] = [r for r in source.get("toRecipients") or [] if bare(r) not in me.mine]
            if everyone:
                change["ccRecipients"] = [r for r in source.get("ccRecipients") or [] if bare(r) not in me.mine]
        if call.inputs.get("cc"):
            change["ccRecipients"] = (change.get("ccRecipients") or draft.get("ccRecipients") or []) + \
                recipients(call.inputs["cc"])
        if change:
            draft = await self._changed(graph, draft, change)
        return await self._finish(call, graph, draft, "reply_all" if everyone else "reply",
                                  str(source.get("conversationId") or ""))

    @guarded
    async def forward(self, call):
        graph = await client(call)
        me = await graph.me()
        to = recipients(call.inputs["to"])
        if not to:
            raise Refused(f"to names no address: {call.inputs.get('to')!r}.")
        source = await self._source(graph, call.inputs, me)
        draft = await graph.post(f"me/messages/{segment(source['id'])}/createForward",
                                 json={"comment": str(call.inputs.get("body") or ""), "toRecipients": to})
        return await self._finish(call, graph, draft, "forward", str(source.get("conversationId") or ""))

    @guarded
    async def attach(self, call):
        record = await call.records.read("draft", str(call.inputs["draft_ref"]))
        keys = record["fields"]
        if keys.get("status") != "prepared":
            raise Refused(f"This draft is {keys.get('status')}: files are attached only to a prepared draft.")
        graph = await client(call, keys.get("account") or "")
        done = await attach_files(call, graph, str(keys["outlook_draft_id"]), call.inputs["files"])
        content = dict(keys.get("content") or {})
        content["files"] = list(content.get("files") or []) + [d["filename"] for d in done]
        await call.records.update("draft", record["ref"], {"content": content})
        return {"draft_ref": record["ref"], "attached": done}, "success"

    @guarded
    async def send(self, call):
        draft_ref = str(call.inputs["draft_ref"])
        keys = (await call.records.read("draft", draft_ref))["fields"]
        status = str(keys.get("status") or "")
        if status == "sent":
            raise Refused(f"This draft was already sent (message {keys.get('sent_message_id')}).", "conflict")
        if status == "unknown":
            raise Refused("This draft's last send had an unknown outcome. Check the Sent Items folder before sending "
                          "again; this function will not send it again.", "unknown")
        if status == "discarded":
            raise Refused("This draft was discarded.", "not_found")
        graph = await client(call, keys.get("account") or "")
        draft_id = str(keys.get("outlook_draft_id") or "")
        content = keys.get("content") or {}
        await call.progress(f"Sending to {content.get('to', '')}: {content.get('subject', '')}")
        try:
            await graph.post(f"me/messages/{segment(draft_id)}/send")
        except HttpError as failed:
            if failed.kind == "unknown":
                # Outlook may or may not have sent it: recorded so, and nothing here sends it twice by accident.
                await call.records.update("draft", draft_ref, {"status": "unknown"})
            raise
        sent = await self._confirm(graph, draft_id)
        if sent.get("isDraft") is not False:
            await call.records.update("draft", draft_ref, {"status": "unknown"})
            raise Refused("Outlook accepted the send but has not confirmed it; the outcome is unknown. Check Sent "
                          "Items before sending again.", "unknown")
        message_id = str(sent.get("id") or draft_id)
        await call.records.update("draft", draft_ref, {"status": "sent", "sent_message_id": message_id})
        return {"sent": True, "status": "sent", "message_id": message_id,
                "thread_id": str(sent.get("conversationId") or ""), "link": str(sent.get("webLink") or "")}, "success"

    @guarded
    async def discard(self, call):
        draft_ref = str(call.inputs["draft_ref"])
        keys = (await call.records.read("draft", draft_ref))["fields"]
        if keys.get("status") == "sent":
            raise Refused("A sent message cannot be discarded.", "conflict")
        graph = await client(call, keys.get("account") or "")
        try:
            await graph.delete(f"me/messages/{segment(str(keys.get('outlook_draft_id') or ''))}")
        except HttpError as failed:
            if failed.kind != "not_found":
                raise
        await call.records.update("draft", draft_ref, {"status": "discarded"})
        return {"discarded": True}, "success"

    # -- helpers ---------------------------------------------------------------------------------------------------
    @staticmethod
    async def _source(graph, inputs, me):
        """The message answered or forwarded: the one named, else the conversation's latest."""
        if inputs.get("message_id"):
            return await graph.get(f"me/messages/{segment(inputs['message_id'])}", params={"$select": FIELDS})
        if not inputs.get("thread_id"):
            raise Refused("Name the message with message_id, or its conversation with thread_id.")
        found = await conversation(graph, str(inputs["thread_id"]))
        if not found:
            raise Refused(f"Outlook has no conversation {inputs['thread_id']} in this mailbox.", "not_found")
        return max(found, key=received)

    @staticmethod
    async def _changed(graph, draft, change):
        """A draft's recipients changed; a draft that cannot be changed is removed, never left half made."""
        try:
            return await graph.patch(f"me/messages/{segment(draft['id'])}", json=change) or draft
        except HttpError:
            await DraftTool._removed(graph, draft)
            raise

    @staticmethod
    async def _removed(graph, draft):
        try:
            await graph.delete(f"me/messages/{segment(draft['id'])}")
        except HttpError:
            pass

    async def _finish(self, call, graph, draft, kind, thread_id):
        draft_id = str(draft.get("id") or "")
        if not draft_id:
            raise Refused("Outlook created no draft.", "unknown")
        try:
            attached = await attach_files(call, graph, draft_id, call.inputs.get("files") or [])
        except (HttpError, Refused):
            await self._removed(graph, draft)
            raise
        to = addresses(draft.get("toRecipients"))
        content = {"to": to, "cc": addresses(draft.get("ccRecipients")), "subject": str(draft.get("subject") or ""),
                   "body": str(call.inputs.get("body") or ""), "files": [a["filename"] for a in attached]}
        fields = {"outlook_draft_id": draft_id, "kind": kind, "status": "prepared",
                  # The account it was made on: sending and discarding follow it, however many are connected then.
                  "account": graph.account.lower(), "content": content}
        if thread_id or draft.get("conversationId"):
            fields["thread_id"] = thread_id or str(draft["conversationId"])
        ref = await call.records.create("draft", fields)
        result = {"draft_ref": ref, "outlook_draft_id": draft_id, "to": to, "subject": content["subject"],
                  "attached": attached}
        if content["cc"]:
            result["cc"] = content["cc"]
        if draft.get("webLink"):
            result["link"] = str(draft["webLink"])
        return result, "success"

    async def _confirm(self, graph, draft_id):
        last: dict[str, Any] = {}
        for attempt in range(self.CONFIRM_ATTEMPTS):
            try:
                last = await graph.get(f"me/messages/{segment(draft_id)}",
                                       params={"$select": "id,isDraft,conversationId,webLink"})
            except HttpError:
                last = {}
            if last.get("isDraft") is False:
                return last
            if attempt < self.CONFIRM_ATTEMPTS - 1:
                await asyncio.sleep(self.CONFIRM_PAUSE)
        return last
