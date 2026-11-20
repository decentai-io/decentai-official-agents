"""Finding and reading mail: Gmail's own search, a thread whole, a long message page by page, an attachment saved
into the chat."""

from pathlib import PurePosixPath

from standard import ToolBase
from standard.http import FILE_MOST, HttpError, human_size

from .account_tool import no_account
from .gmail import gathered, gmail_for, link, summary
from .mime import b64url_decode, find_part, header, header_text, walk

#: The most text one answer carries across a thread's messages; the rest is paged with search_message.
THREAD_TEXT_MOST = 200_000


def message_row(message, text, attachments, inline, offset, most, account_address):
    message_id = str(message.get("id") or "")
    labels = [str(x) for x in message.get("labelIds") or []]
    piece = text[offset:offset + most]
    row = {"message_id": message_id, "internet_id": header(message, "Message-ID"),
           "from": header(message, "From"), "to": header(message, "To"), "cc": header(message, "Cc"),
           "date": header(message, "Date"), "subject": header(message, "Subject"), "body": piece,
           "body_chars": len(text), "truncated": offset + len(piece) < len(text),
           "unread": "UNREAD" in labels, "draft": "DRAFT" in labels,
           "attachments": len(attachments), "inline_images_left_out": inline,
           "link": link(account_address, message_id)}
    if row["truncated"]:
        row["next_offset"] = offset + len(piece)
    return row


class SearchTool(ToolBase):
    id = "search"

    async def find(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        params = {"q": str(call.inputs["query"]), "maxResults": int(call.inputs.get("max_results") or 10)}
        if call.inputs.get("page_token"):
            params["pageToken"] = str(call.inputs["page_token"])
        if call.inputs.get("include_spam_trash"):
            params["includeSpamTrash"] = "true"
        try:
            page = await client.get("messages", params=params)
            names = {str(l.get("id")): str(l.get("name")) for l in await client.labels()}
            stubs = [str(m.get("id") or "") for m in page.get("messages") or []]
            found = await gathered(client.message(i, "metadata") for i in stubs)
        except HttpError as failed:
            return failed.answer()
        address = await client.address()
        rows = []
        for got in found:
            if isinstance(got, HttpError):
                if got.kind == "not_found":   # deleted between the listing and the read
                    continue
                return got.answer()
            rows.append(summary(got, address, names))
        result = {"messages": rows, "total_estimate": int(page.get("resultSizeEstimate") or len(rows))}
        if page.get("nextPageToken"):
            result["next_page_token"] = str(page["nextPageToken"])
        return result, "success"

    async def thread(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        most = int(call.inputs.get("max_body_chars") or 8000)
        try:
            thread = await client.thread(str(call.inputs["thread_id"]))
            address = await client.address()
            messages, attachments, budget = [], [], THREAD_TEXT_MOST
            for message in thread.get("messages") or []:
                text, attached, inline = await client.text_of(message)
                allowed = max(0, min(most, budget))
                row = message_row(message, text, attached, inline, 0, allowed, address)
                budget -= len(row["body"])
                messages.append(row)
                attachments.extend({"message_id": row["message_id"], **a} for a in attached)
        except HttpError as failed:
            return failed.answer()
        subject = next((m["subject"] for m in messages if m["subject"]), "")
        result = {"thread_id": str(thread.get("id") or call.inputs["thread_id"]), "subject": subject,
                  "messages": messages, "attachments": attachments}
        if any(m["truncated"] for m in messages):
            result["note"] = ("Some bodies are longer than shown (truncated: true): read the rest with search_message "
                              "and the message's next_offset.")
        return result, "success"

    async def message(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        offset = int(call.inputs.get("offset") or 0)
        most = int(call.inputs.get("max_chars") or 20000)
        try:
            message = await client.message(str(call.inputs["message_id"]))
            text, attached, inline = await client.text_of(message)
            address = await client.address()
        except HttpError as failed:
            return failed.answer()
        if offset > len(text):
            return {"error": f"The body has {len(text)} characters; offset {offset} is past its end.",
                    "kind": "invalid"}, "error"
        row = message_row(message, text, attached, inline, offset, most, address)
        row["thread_id"] = str(message.get("threadId") or "")
        row["offset"] = offset
        row["attachments"] = attached
        return row, "success"

    async def save_attachment(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        message_id = str(call.inputs["message_id"])
        try:
            # Read again: Gmail gives an attachment a new id on every read, and says its size before the bytes.
            message = await client.message(message_id)
        except HttpError as failed:
            return failed.answer()
        part = find_part(message.get("payload") or {}, str(call.inputs.get("part_id") or ""),
                         str(call.inputs.get("attachment_id") or ""), str(call.inputs.get("filename") or ""))
        given = str(call.inputs.get("attachment_id") or "")
        if part is None and given:
            # Gmail gives a new attachmentId at every read, and still takes an older one: the id listed earlier is
            # used as it is, when no attachment of the message is too large to bring in.
            _, _, listed, _ = walk(message.get("payload") or {})
            if len(listed) == 1:
                part = find_part(message.get("payload") or {}, listed[0]["part_id"])
            elif listed and max(int(a["size"]) for a in listed) <= FILE_MOST:
                part = {"partId": "", "body": {"attachmentId": given, "size": 0}, "mimeType": ""}
        if part is None:
            return {"error": "That message has no such attachment: read the thread again and pass its part_id.",
                    "kind": "not_found"}, "error"
        body = part.get("body") or {}
        filename = safe_name(str(call.inputs.get("filename") or "") or header_text(part.get("filename") or "")
                             or f"attachment-{part.get('partId') or 'file'}")
        size = int(body.get("size") or 0)
        address = await client.address()
        if size > FILE_MOST:
            return {"error": f"{filename} is {human_size(size)} ({size:,} bytes); at most {human_size(FILE_MOST)} "
                             f"({FILE_MOST:,} bytes) can be brought into the chat, so it was not downloaded. Open it "
                             f"in Gmail: {link(address, message_id)}",
                    "kind": "too_large", "size": size, "limit": FILE_MOST}, "error"
        try:
            if body.get("data") is not None:
                raw = b64url_decode(body["data"])
            else:
                raw = await client.attachment(message_id, str(body.get("attachmentId") or ""))
        except HttpError as failed:
            return failed.answer()
        if len(raw) > FILE_MOST:
            return {"error": f"{filename} is {human_size(len(raw))}; at most {human_size(FILE_MOST)} can be brought "
                             f"into the chat. Open it in Gmail: {link(address, message_id)}",
                    "kind": "too_large", "size": len(raw), "limit": FILE_MOST}, "error"
        if not raw and size:
            return {"error": "Gmail returned the attachment empty.", "kind": "http"}, "error"
        await call.progress(f"Saving {filename} ({human_size(len(raw))})")
        return {"file_ref": call.give(filename, raw), "filename": filename, "size": len(raw),
                "mime_type": str(part.get("mimeType") or "")}, "success"


def safe_name(name: str) -> str:
    """A file name only: no folders, no control characters; Arabic and every other script kept."""
    name = PurePosixPath(name.replace("\\", "/")).name
    name = "".join(c for c in name if c >= " " and c not in '<>:"|?*').strip(" .")
    return name[:200] or "attachment"
