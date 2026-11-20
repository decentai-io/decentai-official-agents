"""Drafts: kept on the platform, shown to the person, and sent only on a separate level-3 call whose success is the
mail server's word alone.

A draft is a record here and not a message in the account's Drafts folder: what is sent is what the person was
shown, letter for letter. Its words and recipients are kept encrypted (``values``); files attached from the chat are
kept as the agent's own files until it is sent.

**Sent once.** A draft is claimed before it is handed over: in this process (two calls of one chat at once) and in
its record (status ``sending``, written before the first byte goes). Only a ``prepared`` draft is sent; one that
was refused with nothing sent goes back to ``prepared``; one whose outcome is unknown is never sent again here. Its
Message-ID is chosen when it is drafted, so even a copy sent by some other way is the same message to every server.
"""

from __future__ import annotations

from pathlib import Path

from standard import ResourceDenied, ToolBase
from standard.http import FILE_MOST, human_size

from . import messages
from .account_tool import account_for, blocking, failure
from .mailbox import Mailbox
from .search_tool import conversation, root_of
from .sender import Outgoing, formatted, forwarded_text, new_message_id, reply_all, written
from .servers import MailError

#: The most the files of one message may come to, before encoding: base64 makes 18 MB about 24.7 MB, under the 25 MB
#: most providers take.
ATTACH_MOST = 18 * 1024 * 1024
#: Drafts being sent by this process right now: a second call for one of them is refused at once.
SENDING: set = set()


def reply_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def forward_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if subject.lower().startswith(("fwd:", "fw:")) else f"Fwd: {subject}"


def joined(*texts: str) -> str:
    return ", ".join(t.strip() for t in texts if t and t.strip())


class DraftTool(ToolBase):
    id = "draft"

    # -- what the person attaches -----------------------------------------------------------------------------------
    async def _kept_files(self, call, already: int = 0):
        """The chat's files named in ``attachments``, kept as this agent's files: [{ref, name, size}], or the error
        result. Refused, with the numbers, when they come to more than one message carries."""
        given = [str(p) for p in call.inputs.get("attachments") or []]
        found, total = [], already
        for path in given:
            try:
                local = await call.file(path)
            except ResourceDenied:
                return None, ({"error": f"{path} is not a file of this chat: attach it to the chat first.",
                               "kind": "not_found"}, "error")
            data = local.read_bytes()
            if len(data) > FILE_MOST:
                return None, ({"error": f"{local.name} is {human_size(len(data))}; a chat file is at most "
                                        f"{human_size(FILE_MOST)}.", "kind": "too_large"}, "error")
            total += len(data)
            found.append((local.name, data))
        if total > ATTACH_MOST:
            return None, ({"error": f"The attachments come to {human_size(total)}; one message carries at most "
                                    f"{human_size(ATTACH_MOST)} of files (most providers refuse more than 25 MB once "
                                    f"encoded). Nothing was drafted: send fewer, or share a link instead.",
                           "kind": "too_large", "size": total, "limit": ATTACH_MOST}, "error")
        kept = []
        for name, data in found:
            ref = await call.records.create_file("attachment", name, data)
            kept.append({"ref": ref, "name": name, "size": len(data)})
        return kept, None

    async def _drafted(self, call, account, fields, kept, extra_names=()):
        ref = await call.records.create("draft", {
            **{k: v for k, v in fields.items() if v not in ("", None)}, "status": "prepared",
            "account": account.email.lower(), "message_id": new_message_id(account.email),
            **({"attachments": {"files": kept}} if kept else {})})
        result = {"draft_ref": ref, "to": fields["to"], "subject": fields["subject"]}
        if fields.get("cc"):
            result["cc"] = fields["cc"]
        names = [*extra_names, *(f["name"] for f in kept)]
        if names:
            result["attachments"] = names
        return result, "success"

    # -- drafting -------------------------------------------------------------------------------------------------
    async def reply(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"

        def read():
            with Mailbox(account) as mailbox:
                return conversation(mailbox, str(call.inputs["thread_id"]))

        try:
            found = await blocking(read)
        except MailError as exc:
            return failure(exc)
        mine = account.email.lower()
        last = found[-1][0]
        # The message being answered: the last one someone other than the account wrote.
        answered = next((m for m, _ in reversed(found) if messages.address(m.get("From")) != mine), last)
        to = str(call.inputs.get("to") or "").strip()
        if not to:
            sender = messages.address(answered.get("Reply-To") or answered.get("From"))
            to = sender if sender and sender != mine else ""
        if not to:
            return {"error": "Nobody but the account itself has written in this conversation: say who the reply "
                             "goes to.", "kind": "invalid"}, "error"
        cc = str(call.inputs.get("cc") or "")
        if call.inputs.get("reply_all"):
            cc = joined(formatted(reply_all(answered, {mine}, to)), cc)
        kept, refused = await self._kept_files(call)
        if refused:
            return refused
        return await self._drafted(call, account, {
            "kind": "reply", "thread_id": root_of(found), "to": to, "cc": cc,
            "bcc": str(call.inputs.get("bcc") or ""),
            "subject": reply_subject(messages.header(found[0][0], "Subject")), "body": str(call.inputs["body"]),
            "in_reply_to": messages.message_id(last),
            "references": " ".join(messages.bracketed(i) for i in messages.parents(last))}, kept)

    async def compose(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        kept, refused = await self._kept_files(call)
        if refused:
            return refused
        return await self._drafted(call, account, {
            "kind": "new", "to": str(call.inputs["to"]), "cc": str(call.inputs.get("cc") or ""),
            "bcc": str(call.inputs.get("bcc") or ""), "subject": str(call.inputs["subject"]),
            "body": str(call.inputs["body"])}, kept)

    async def forward(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        wanted = str(call.inputs["message_id"])

        def read():
            with Mailbox(account) as mailbox:
                where = mailbox.by_id(wanted)
                if where is None:
                    raise MailError("not_found", f"The mailbox has no message {wanted}.")
                mailbox.open(where[0])
                return mailbox.whole(where[1])

        try:
            message, received = await blocking(read)
        except MailError as exc:
            return failure(exc)
        theirs = messages.attachments_of(message)
        kept, refused = await self._kept_files(call, already=sum(len(c) for _, _, c in theirs))
        if refused:
            return refused
        return await self._drafted(call, account, {
            "kind": "forward", "forward_of": messages.message_id(message) or wanted,
            "thread_id": messages.thread_id(message), "to": str(call.inputs["to"]),
            "cc": str(call.inputs.get("cc") or ""), "bcc": str(call.inputs.get("bcc") or ""),
            "subject": forward_subject(messages.header(message, "Subject")),
            "body": forwarded_text(message, received, str(call.inputs.get("body") or ""))},
            kept, extra_names=[name for name, _, _ in theirs])

    # -- sending --------------------------------------------------------------------------------------------------
    async def send(self, call):
        ref = str(call.inputs["draft_ref"])
        if ref in SENDING:
            return {"error": "This draft is being sent by another call right now: it is not sent twice.",
                    "kind": "invalid"}, "error"
        SENDING.add(ref)
        try:
            return await self._send(call, ref)
        finally:
            SENDING.discard(ref)

    async def _send(self, call, ref):
        fields = (await call.records.read("draft", ref))["fields"]
        status = fields.get("status")
        if status == "sending":
            return {"error": "A send of this draft began and never finished, so whether it went is unknown: it is "
                             "not sent again. Check the Sent folder.", "kind": "unknown"}, "error"
        if status != "prepared":
            return {"error": f"This draft is {status}, not prepared: it is not sent again.", "kind": "invalid"}, "error"
        account, why = await account_for(call, str(fields.get("account") or ""))
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        files = []
        for kept in ((fields.get("attachments") or {}).get("files") or []):
            try:
                read = await call.records.read("attachment", kept["ref"])
            except ResourceDenied as exc:
                return {"error": f"The attached file {kept.get('name')} is no longer kept: {exc}. Nothing was sent.",
                        "kind": "not_found"}, "error"
            files.append((kept["name"], str(read.get("type") or ""), (Path.cwd() / read["path"]).read_bytes()))
        if fields.get("forward_of"):
            def original():
                with Mailbox(account) as mailbox:
                    where = mailbox.by_id(str(fields["forward_of"]))
                    if where is None:
                        raise MailError("not_found", f"The message being forwarded ({fields['forward_of']}) is no "
                                                     f"longer in the mailbox. Nothing was sent.")
                    mailbox.open(where[0])
                    return mailbox.whole(where[1])[0]
            try:
                files = messages.attachments_of(await blocking(original)) + files
            except MailError as exc:
                return failure(exc)
        message_id = str(fields.get("message_id") or "") or new_message_id(account.email)
        raw, message_id = written(
            account.email, to=str(fields.get("to") or ""), subject=str(fields.get("subject") or ""),
            body=str(fields.get("body") or ""), cc=str(fields.get("cc") or ""),
            in_reply_to=str(fields.get("in_reply_to") or ""),
            references=messages.ids_in(fields.get("references")), message_id=message_id, attachments=files)
        recipients = messages.addresses(fields.get("to"), fields.get("cc"), fields.get("bcc"))
        thread_id = str(fields.get("thread_id") or "") or message_id
        if fields.get("kind") == "forward":
            thread_id = message_id
        # Claimed before the first byte goes: a call that finds it so will not send it again.
        await call.records.update("draft", ref, {"status": "sending", "sent_message_id": message_id})
        try:
            await blocking(Outgoing(account).send, raw, recipients)
        except MailError as exc:
            if exc.kind != "unknown":
                await call.records.update("draft", ref, {"status": "prepared"})
                return failure(exc)
            await call.records.update("draft", ref, {"status": "unknown"})
            return {"sent": False, "status": "unknown", "message_id": message_id, "thread_id": thread_id,
                    "problem": exc.message}, "success"
        await call.records.update("draft", ref, {"status": "sent", "thread_id": thread_id})
        result = {"sent": True, "status": "sent", "message_id": message_id, "thread_id": thread_id}

        def kept_copy():
            with Mailbox(account) as mailbox:
                return mailbox.keep_sent(raw, message_id)

        # The message is sent whatever becomes of its copy.
        try:
            result["kept_in_sent"] = await blocking(kept_copy)
        except MailError as exc:
            result["kept_in_sent"] = False
            result["problem"] = f"It was sent. A copy could not be put in the Sent folder: {exc.message}"
        return result, "success"

    async def discard(self, call):
        ref = str(call.inputs["draft_ref"])
        if ref in SENDING:
            return {"error": "This draft is being sent right now.", "kind": "invalid"}, "error"
        status = (await call.records.read("draft", ref))["fields"].get("status")
        if status != "prepared":
            return {"error": f"This draft is {status}: only a prepared draft is discarded.", "kind": "invalid"}, "error"
        await call.records.update("draft", ref, {"status": "discarded"})
        return {"discarded": True}, "success"
