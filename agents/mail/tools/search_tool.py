"""Finding and reading mail, and the account's folders. Every mail-server call runs off the agent's loop."""

from datetime import datetime

from standard import ToolBase
from standard.http import FILE_MOST, human_size

from . import messages
from .account_tool import account_for, blocking, failure
from .mailbox import Mailbox, quoted
from .servers import MailError

#: How many of the newest matches are read to be narrowed here, where a sender or a subject is written in letters
#: a mail server's search does not take.
NARROWED = 200
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class NotADate(ValueError):
    pass


def day(value) -> str:
    """A date as a mail server reads one: 3-Sep-2026. A time with an offset is the day it falls on in its own zone."""
    text = str(value or "").strip()
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise NotADate(f"'{text}' is not a date: write it as 2026-09-03.")
    return f"{when.day}-{MONTHS[when.month - 1]}-{when.year}"


class Wanted:
    """What a search asks for, as a mail server is asked and as this agent narrows what the server cannot."""

    FIELDS = (("from", "FROM", "From"), ("to", "TO", "To"), ("subject", "SUBJECT", "Subject"))

    def __init__(self, inputs):
        self.criteria = []
        #: (header, words): matched here, against the decoded header.
        self.narrowed = []
        for name, keyword, header in self.FIELDS:
            words = str(inputs.get(name) or "").strip()
            if not words:
                continue
            if words.isascii():
                self.criteria += [keyword, quoted(words)]
            else:
                self.narrowed.append((header, words.lower()))
        if inputs.get("since"):
            self.criteria += ["SINCE", day(inputs["since"])]
        if inputs.get("before"):
            self.criteria += ["BEFORE", day(inputs["before"])]
        if inputs.get("unread_only"):
            self.criteria.append("UNSEEN")
        self.text = str(inputs.get("query") or "").strip()

    def keeps(self, message) -> bool:
        return all(words in messages.header(message, header).lower() for header, words in self.narrowed)


def folder_of(mailbox, wanted) -> str:
    """The folder a search reads: the inbox, the Sent folder, the account's archive of everything, or a folder by
    its own name (as folders_list shows it, in any alphabet)."""
    wanted = str(wanted or "").strip()
    folders = mailbox.folders()
    if not wanted:
        return mailbox.scope()[0]
    if wanted.lower() == "inbox":
        return Mailbox.INBOX
    if wanted.lower() in ("sent", "all", "drafts", "trash", "junk", "archive"):
        found = folders.get(wanted.lower())
        if not found and wanted.lower() == "all":
            return Mailbox.INBOX
        if not found:
            raise MailError("not_found", f"This account has no {wanted.lower()} folder that the mail server names "
                                         f"as one: folders_list shows its folders.")
        return found
    known = {f["name"].lower(): f["name"] for f in mailbox.listing() if f["selectable"]}
    if wanted.lower() not in known:
        raise MailError("not_found", f"This account has no folder {wanted!r}: folders_list shows its folders.")
    return known[wanted.lower()]


def conversation(mailbox, thread_id):
    """Every message of a conversation, oldest first. The id may be the conversation's own or that of any message in
    it: a person points at the message they are looking at."""
    found = mailbox.conversation(thread_id)
    if not found:
        raise MailError("not_found", f"The mailbox has no conversation {thread_id}.")
    return found


def root_of(found) -> str:
    """The conversation's id: what its oldest message names first as what it answers, else that message itself."""
    return messages.thread_id(found[0][0])


class SearchTool(ToolBase):
    id = "search"

    async def find(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        limit = int(call.inputs.get("max_results") or 10)
        try:
            start = int(str(call.inputs.get("page_token") or "0"))
        except ValueError:
            return {"error": "page_token is what a search returned as next_page_token, unchanged.",
                    "kind": "invalid"}, "error"
        try:
            wanted = Wanted(call.inputs)
        except NotADate as exc:
            return {"error": str(exc), "kind": "invalid"}, "error"

        def found():
            with Mailbox(account) as mailbox:
                folder = folder_of(mailbox, call.inputs.get("folder"))
                mailbox.open(folder)
                newest_first = list(reversed(mailbox.search(wanted.criteria, wanted.text)))
                if wanted.narrowed:
                    newest_first = newest_first[:NARROWED]
                rows, position = [], start
                while position < len(newest_first) and len(rows) < limit:
                    message, received, seen, text = mailbox.glance(newest_first[position])
                    position += 1
                    if wanted.keeps(message):
                        rows.append({**messages.row(message, received, text), "unread": not seen,
                                     "folder": folder})
                return folder, newest_first, rows, position

        try:
            folder, newest_first, rows, position = await blocking(found)
        except MailError as exc:
            return failure(exc)
        result = {"messages": rows, "folder": folder, "total_estimate": len(newest_first)}
        if position < len(newest_first):
            result["next_page_token"] = str(position)
        return result, "success"

    async def thread(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        cap = int(call.inputs.get("max_body_chars") or 4000)

        def read():
            with Mailbox(account) as mailbox:
                return conversation(mailbox, str(call.inputs["thread_id"]))

        try:
            found = await blocking(read)
        except MailError as exc:
            return failure(exc)
        listed, attachments = [], []
        for message, received in found:
            text, attached = messages.body_and_attachments(message)
            message_id = messages.message_id(message)
            listed.append({"message_id": message_id, "from": messages.header(message, "From"),
                           "to": messages.header(message, "To"), "cc": messages.header(message, "Cc"),
                           "date": messages.sent_at(message, received), "subject": messages.header(message, "Subject"),
                           "body": text[:cap], "truncated": len(text) > cap})
            attachments.extend({"message_id": message_id, **item} for item in attached)
        return {"thread_id": root_of(found), "subject": listed[0]["subject"], "messages": listed,
                "attachments": attachments}, "success"

    async def save_attachment(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        filename = str(call.inputs["filename"])

        def read():
            with Mailbox(account) as mailbox:
                where = mailbox.by_id(str(call.inputs["message_id"]))
                if where is None:
                    raise MailError("not_found", f"The mailbox has no message {call.inputs['message_id']}.")
                mailbox.open(where[0])
                return mailbox.whole(where[1])[0]

        try:
            message = await blocking(read)
        except MailError as exc:
            return failure(exc)
        try:
            raw = messages.attachment_bytes(message, str(call.inputs["attachment_id"]))
        except KeyError:
            return {"error": f"That message has no attachment {call.inputs['attachment_id']}.",
                    "kind": "not_found"}, "error"
        if not raw:
            return {"error": "The attachment is empty.", "kind": "invalid"}, "error"
        if len(raw) > FILE_MOST:
            return {"error": f"“{filename}” is {human_size(len(raw))}, and a file in the chat is at most "
                             f"{human_size(FILE_MOST)}. It was not saved. Nothing is wrong with the message; open it "
                             f"in the mail program, or ask the sender for a link.",
                    "kind": "too_large", "size": len(raw), "limit": FILE_MOST}, "error"
        await call.progress(f"Saving {filename} ({human_size(len(raw))})")
        return {"file_ref": call.give(filename, raw), "filename": filename, "size": len(raw)}, "success"


class FoldersTool(ToolBase):
    id = "folders"

    async def list(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        counts = bool(call.inputs.get("counts"))

        def listed():
            with Mailbox(account) as mailbox:
                rows = []
                for folder in mailbox.listing():
                    row = {"name": folder["name"], "kind": folder["kind"], "selectable": folder["selectable"]}
                    if counts and folder["selectable"] and len(rows) < 100:
                        row.update(mailbox.status(folder["name"]))
                    rows.append(row)
                return rows

        try:
            return {"folders": await blocking(listed)}, "success"
        except MailError as exc:
            return failure(exc)
