"""Drafts: prepared in Gmail with any files of the chat's attached, recorded on the platform, and sent only by a
separate level-3 call whose success is Gmail's word alone. A send with no answer is ``unknown`` and never sent again.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any

from standard import ResourceDenied, ToolBase
from standard.http import FILE_MOST, HttpError, human_size

from .account_tool import no_account
from .gmail import ATTACHMENTS_MOST, RAW_MOST, Gmail, draft_link, gmail_for, link
from .mime import (
    addresses, b64url_decode, bare, build, forward_subject, formatted, header, html_to_text, mime_type,
    quoted_plain, references_for, reply_subject, text_to_html, unique, wrong_addresses,
)
from .search_tool import safe_name

#: How much of the message replied to or forwarded is carried in the new one.
QUOTE_MOST = 100_000


class Refused(Exception):
    def __init__(self, message: str, kind: str = "invalid", **extra: Any):
        super().__init__(message)
        self.answer = {"error": message, "kind": kind, **extra}, "error"


# -- what goes into a draft -----------------------------------------------------------------------------------------

async def sender_for(client: Gmail, wanted: str, addressed: list[str] | None = None) -> tuple[str, str, str]:
    """(name, address, signature HTML) of the send-as address wanted; else the one the replied message was sent to;
    else the account's default."""
    aliases = [a for a in await client.send_as() if a.get("sendAsEmail") and (
        a.get("isPrimary") or str(a.get("verificationStatus") or "accepted") == "accepted")]
    wanted = bare(wanted)
    chosen = None
    if wanted:
        chosen = next((a for a in aliases if str(a["sendAsEmail"]).lower() == wanted), None)
        if chosen is None and wanted != client.account.lower():
            usable = ", ".join(str(a["sendAsEmail"]) for a in aliases) or client.account
            raise Refused(f"{wanted} is not an address this account sends as. It sends as: {usable}.")
    by_address = {str(a["sendAsEmail"]).lower(): a for a in aliases}
    if chosen is None and addressed:
        chosen = next((by_address[a] for a in addressed if a in by_address), None)
    if chosen is None:
        chosen = next((a for a in aliases if a.get("isDefault")), None) or next(
            (a for a in aliases if a.get("isPrimary")), None)
    if chosen is None:
        return "", wanted or await client.address(), ""
    return str(chosen.get("displayName") or ""), str(chosen["sendAsEmail"]), str(chosen.get("signature") or "")


def people(value: Any, what: str) -> list[tuple[str, str]]:
    found = addresses(value)
    wrong = wrong_addresses(found)
    if wrong:
        raise Refused(f"{what} has addresses that are not email addresses: {', '.join(wrong)}.")
    return found


async def chat_files(call, refs: list[Any]) -> list[tuple[str, str, bytes]]:
    """The chat's files named, read off the loop; each at most FILE_MOST."""
    files = []
    for ref in refs or []:
        try:
            path = await call.file(str(ref))
        except ResourceDenied:
            raise Refused(f"{ref} is not a file of this chat's: attach it to the chat, or save it there first.")
        size = path.stat().st_size
        if size > FILE_MOST:
            raise Refused(f"{path.name} is {human_size(size)}; a chat file is at most {human_size(FILE_MOST)}.",
                          "too_large")
        files.append((safe_name(path.name), mime_type(path.name), await asyncio.to_thread(path.read_bytes)))
    return files


def held_to_gmail(files: list[tuple[str, str, bytes]]) -> None:
    total = sum(len(f[2]) for f in files)
    if total > ATTACHMENTS_MOST:
        sizes = ", ".join(f"{name} {human_size(len(data))}" for name, _, data in files)
        raise Refused(f"The attachments come to {human_size(total)} ({sizes}); Gmail sends at most "
                      f"{human_size(ATTACHMENTS_MOST)} in one message. Send fewer, or share a link to the files.",
                      "too_large", size=total, limit=ATTACHMENTS_MOST)


def bodies(text: str, signature: str, quote: tuple[str, str] | None = None,
           forwarded: tuple[list[str], str] | None = None) -> tuple[str, str]:
    """The plain text, and the same as HTML that shows exactly that text, then the signature, then what is quoted."""
    plain, rich = text.rstrip(), text_to_html(text.rstrip())
    if signature.strip():
        plain += "\n\n-- \n" + html_to_text(signature)
        rich += f'<br><div dir="auto" class="gmail_signature">{signature}</div>'
    if quote is not None:
        said, original = quote
        plain += f"\n\n{said}\n{quoted_plain(original)}"
        rich += (f'<br><div class="gmail_quote"><div dir="auto" class="gmail_attr">{html.escape(said)}<br></div>'
                 f'<blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex;border-left:1px solid '
                 f'rgb(204,204,204);padding-left:1ex">{text_to_html(original)}</blockquote></div>')
    if forwarded is not None:
        lines, original = forwarded
        plain += "\n\n---------- Forwarded message ---------\n" + "\n".join(lines) + "\n\n" + original
        rich += ('<br><div class="gmail_quote"><div dir="auto" class="gmail_attr">---------- Forwarded message '
                 '---------<br>' + "<br>".join(html.escape(line) for line in lines) + "<br></div><br>"
                 + text_to_html(original) + "</div>")
    return plain, rich


def cut(text: str) -> str:
    return text if len(text) <= QUOTE_MOST else text[:QUOTE_MOST] + "\n[…]"


class DraftTool(ToolBase):
    id = "draft"

    async def compose(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        try:
            to = people(call.inputs["to"], "to")
            cc, bcc = people(call.inputs.get("cc"), "cc"), people(call.inputs.get("bcc"), "bcc")
            files = await chat_files(call, call.inputs.get("attachments") or [])
            held_to_gmail(files)
            name, address, signature = await sender_for(client, str(call.inputs.get("from") or ""))
            signature = signature if call.inputs.get("signature", True) else ""
            plain, rich = bodies(str(call.inputs["body"]), signature)
            return await self._made(call, client, kind="new", sender=(name, address), to=to, cc=cc, bcc=bcc,
                                    subject=str(call.inputs["subject"]), plain=plain, rich=rich, files=files,
                                    signature=bool(signature.strip()))
        except Refused as refused:
            return refused.answer
        except HttpError as failed:
            return failed.answer()

    async def reply(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        thread_id = str(call.inputs["thread_id"])
        try:
            thread = await client.thread(thread_id)
            target = self._target(thread, str(call.inputs.get("message_id") or ""))
            me = await client.me()
            ours = bare(header(target, "From")) in me
            replied_to = addresses(header(target, "Reply-To")) or addresses(header(target, "From"))
            if ours:
                # Our own last message: the reply goes on to whoever it went to, never back to us.
                to = addresses(header(target, "To"))
                cc = addresses(header(target, "Cc")) if call.inputs.get("reply_all") else []
            else:
                to = replied_to
                cc = (addresses(header(target, "To")) + addresses(header(target, "Cc"))
                      if call.inputs.get("reply_all") else [])
            to = unique(to + people(call.inputs.get("to"), "to"), me)
            cc = unique(cc + people(call.inputs.get("cc"), "cc"), me | {a.lower() for _, a in to})
            bcc = unique(people(call.inputs.get("bcc"), "bcc"), me)
            if not to:
                if not cc:
                    raise Refused("Everyone on that message is this account itself: say who the reply is to.")
                to, cc = cc[:1], cc[1:]
            files = await chat_files(call, call.inputs.get("attachments") or [])
            held_to_gmail(files)
            # Answered from the address the message was sent to, as Gmail does: To before Cc.
            addressed = [a.lower() for _, a in addresses(header(target, "To")) + addresses(header(target, "Cc"))]
            name, address, signature = await sender_for(client, str(call.inputs.get("from") or ""), addressed)
            signature = signature if call.inputs.get("signature", True) else ""
            quote = None
            if call.inputs.get("quote", True):
                original, _, _ = await client.text_of(target)
                quote = (f"On {header(target, 'Date')}, {header(target, 'From')} wrote:", cut(original))
            plain, rich = bodies(str(call.inputs["body"]), signature, quote)
            internet_id = header(target, "Message-ID")
            return await self._made(
                call, client, kind="reply", sender=(name, address), to=to, cc=cc, bcc=bcc,
                subject=reply_subject(header(target, "Subject")), plain=plain, rich=rich, files=files,
                thread_id=str(thread.get("id") or thread_id), in_reply_to=internet_id,
                references=references_for(header(target, "References"), internet_id),
                signature=bool(signature.strip()), extra={"replied_to_message_id": str(target.get("id") or ""),
                                                          "quoted": quote is not None})
        except Refused as refused:
            return refused.answer
        except HttpError as failed:
            return failed.answer()

    async def forward(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        message_id = str(call.inputs["message_id"])
        try:
            original = await client.message(message_id)
            to = unique(people(call.inputs["to"], "to"), set())
            cc = unique(people(call.inputs.get("cc"), "cc"), {a.lower() for _, a in to})
            bcc = people(call.inputs.get("bcc"), "bcc")
            text, attached, inline = await client.text_of(original)
            files = []
            if call.inputs.get("include_attachments", True) and attached:
                total = sum(int(a["size"]) for a in attached)
                if total > ATTACHMENTS_MOST:
                    raise Refused(f"The message's attachments come to {human_size(total)}; Gmail sends at most "
                                  f"{human_size(ATTACHMENTS_MOST)}. Forward it without them "
                                  f"(include_attachments false) and share a link instead.", "too_large",
                                  size=total, limit=ATTACHMENTS_MOST)
                for item in attached:
                    files.append((item["filename"], item["mime_type"],
                                  await self._original_file(client, original, item)))
            files += await chat_files(call, call.inputs.get("attachments") or [])
            held_to_gmail(files)
            name, address, signature = await sender_for(client, str(call.inputs.get("from") or ""))
            signature = signature if call.inputs.get("signature", True) else ""
            lines = [f"From: {header(original, 'From')}", f"Date: {header(original, 'Date')}",
                     f"Subject: {header(original, 'Subject')}", f"To: {header(original, 'To')}"]
            if header(original, "Cc"):
                lines.append(f"Cc: {header(original, 'Cc')}")
            plain, rich = bodies(str(call.inputs.get("body") or ""), signature, forwarded=(lines, cut(text)))
            internet_id = header(original, "Message-ID")
            extra = {"forwarded_message_id": message_id}
            if inline:
                extra["inline_images_left_out"] = inline
            return await self._made(
                call, client, kind="forward", sender=(name, address), to=to, cc=cc, bcc=bcc,
                subject=forward_subject(header(original, "Subject")), plain=plain, rich=rich, files=files,
                thread_id=str(original.get("threadId") or ""), in_reply_to=internet_id,
                references=references_for(header(original, "References"), internet_id),
                signature=bool(signature.strip()), extra=extra)
        except Refused as refused:
            return refused.answer
        except HttpError as failed:
            return failed.answer()

    @staticmethod
    def _target(thread: dict[str, Any], message_id: str) -> dict[str, Any]:
        messages = thread.get("messages") or []
        if message_id:
            found = next((m for m in messages if str(m.get("id")) == message_id), None)
            if found is None:
                raise Refused(f"Message {message_id} is not in that thread.", "not_found")
            return found
        real = [m for m in messages if not {"DRAFT", "TRASH", "SPAM"} & set(m.get("labelIds") or [])]
        real = real or [m for m in messages if "DRAFT" not in (m.get("labelIds") or [])]
        if not real:
            raise Refused("That thread has no message to reply to.", "not_found")
        return real[-1]

    @staticmethod
    async def _original_file(client: Gmail, message: dict[str, Any], item: dict[str, Any]) -> bytes:
        from .mime import find_part

        part = find_part(message.get("payload") or {}, item["part_id"], item["attachment_id"]) or {}
        body = part.get("body") or {}
        if body.get("data") is not None:
            return b64url_decode(body["data"])
        return await client.attachment(str(message.get("id") or ""), str(body.get("attachmentId") or ""))

    async def _made(self, call, client: Gmail, *, kind: str, sender: tuple[str, str], to, cc, bcc, subject: str,
                    plain: str, rich: str, files, thread_id: str = "", in_reply_to: str = "", references: str = "",
                    signature: bool = False, extra: dict[str, Any] | None = None):
        if not to and not cc and not bcc:
            raise Refused("The draft names no one to send it to.")
        try:
            raw = await asyncio.to_thread(build, sender=sender, to=to, cc=cc, bcc=bcc, subject=subject,
                                          plain=plain, rich=rich, files=files, in_reply_to=in_reply_to,
                                          references=references)
        except ValueError as wrong:  # a header that would break the message (a line break in a subject)
            raise Refused(f"The message could not be written: {wrong}") from None
        if len(raw) > RAW_MOST:
            raise Refused(f"The message comes to {human_size(len(raw))} encoded; Gmail takes at most "
                          f"{human_size(RAW_MOST)}.", "too_large", size=len(raw), limit=RAW_MOST)
        if files:
            await call.progress(f"Uploading the draft with {len(files)} attachment(s), {human_size(len(raw))}")
        draft = await client.create_draft(raw, thread_id)
        gmail_draft_id = str(draft.get("id") or "")
        if not gmail_draft_id:
            raise Refused("Gmail answered without a draft.", "http")
        made = draft.get("message") or {}
        account_address = await client.address()
        content = {"from": formatted([sender]), "to": formatted(to), "cc": formatted(cc), "bcc": formatted(bcc),
                   "subject": subject, "text": plain,
                   "attachments": [{"filename": name, "size": len(data)} for name, _, data in files]}
        fields = {"gmail_draft_id": gmail_draft_id, "kind": kind, "status": "prepared",
                  # The account it was made on: sending and discarding follow it.
                  "account": account_address.lower(), "content": content}
        if thread_id:
            fields["thread_id"] = thread_id
        result = {"draft_ref": await call.records.create("draft", fields), "gmail_draft_id": gmail_draft_id,
                  "from": content["from"], "to": content["to"], "subject": subject,
                  "attachments": content["attachments"], "signature_added": signature, **(extra or {})}
        if cc:
            result["cc"] = content["cc"]
        if bcc:
            result["bcc"] = content["bcc"]
        landed = str(made.get("threadId") or "")
        if landed:
            result["thread_id"] = landed
        if thread_id and landed and landed != thread_id:
            result["note"] = "Gmail put this draft in a new conversation rather than the original thread."
        if made.get("id"):
            result["link"] = draft_link(account_address, str(made["id"]))
        return result, "success"

    async def send(self, call):
        draft_ref = str(call.inputs["draft_ref"])
        try:
            record = (await call.records.read("draft", draft_ref))["fields"]
        except ResourceDenied as refused:
            return {"error": f"No draft {draft_ref}: {refused}", "kind": "not_found"}, "error"
        status = str(record.get("status") or "")
        if status == "sent":
            return {"error": f"This draft was already sent (message {record.get('sent_message_id')}); it is not sent "
                             f"again.", "kind": "conflict"}, "error"
        if status == "unknown":
            return {"error": "This draft's last send had no answer from Gmail, so whether it went is unknown. Check "
                             "the Sent folder; it is not sent again from here.", "kind": "unknown"}, "error"
        if status == "discarded":
            return {"error": "This draft was discarded.", "kind": "conflict"}, "error"
        client, why = await gmail_for(call, str(record.get("account") or ""))
        if client is None:
            return no_account(why)
        content = record.get("content") or {}
        await call.progress(f"Sending to {content.get('to') or 'its recipients'}: {content.get('subject') or ''}")
        try:
            sent = await client.send_draft(str(record.get("gmail_draft_id") or ""))
        except HttpError as failed:
            if failed.kind == "unknown":
                # Gmail may or may not have sent it: recorded so, so nothing sends it twice.
                await call.records.update("draft", draft_ref, {"status": "unknown"})
                return {"error": failed.message + " Check the Sent folder before sending anything again.",
                        "kind": "unknown"}, "error"
            if failed.kind == "not_found":
                return {"error": "The draft is no longer in Gmail: it was sent or deleted there. Check the Sent "
                                 "folder.", "kind": "not_found"}, "error"
            return failed.answer()
        message_id = str(sent.get("id") or "")
        if not message_id:
            await call.records.update("draft", draft_ref, {"status": "unknown"})
            return {"error": "Gmail accepted the send but named no sent message, so whether it went is unknown. "
                             "Check the Sent folder.", "kind": "unknown"}, "error"
        await call.records.update("draft", draft_ref, {"status": "sent", "sent_message_id": message_id})
        return {"sent": True, "status": "sent", "message_id": message_id,
                "thread_id": str(sent.get("threadId") or ""),
                "link": link(str(record.get("account") or ""), message_id)}, "success"

    async def discard(self, call):
        draft_ref = str(call.inputs["draft_ref"])
        try:
            record = (await call.records.read("draft", draft_ref))["fields"]
        except ResourceDenied as refused:
            return {"error": f"No draft {draft_ref}: {refused}", "kind": "not_found"}, "error"
        if record.get("status") == "sent":
            return {"error": "A sent message cannot be discarded.", "kind": "conflict"}, "error"
        client, why = await gmail_for(call, str(record.get("account") or ""))
        if client is None:
            return no_account(why)
        try:
            await client.delete_draft(str(record.get("gmail_draft_id") or ""))
        except HttpError as failed:
            if failed.kind != "not_found":
                return failed.answer()
        await call.records.update("draft", draft_ref, {"status": "discarded"})
        return {"discarded": True}, "success"
