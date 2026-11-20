"""Watches: the two things a schedule can ask this mailbox, each run through ``standard.watch``: every watch is read
on its own, one that fails is listed in ``problems`` and keeps its place, one whose conversation is gone closes, and
what is handed on and where each watch now stands are written together once all were read.

A REPLY watch remembers where a conversation stood (its last message, and that message's time); check() reads the
conversation again and asks whether anyone else has written since. Should that last message no longer be in the
conversation (deleted, or moved out of the folders read), its time stands in for it.

An INBOX watch remembers how far the inbox has been read, in the mail server's own count: every message in a folder
has a number that only grows within the folder's validity, so "what came after number n" loses nothing and shows
nothing twice. When the server renumbers the folder (a new UIDVALIDITY), the numbers mean nothing any more: what
arrived since the last check is found by its arrival time instead, and the run says so in ``notices``.
"""

from datetime import datetime, timezone

from standard import ResourceDenied, ToolBase
from standard.http import HttpError
from standard.watch import Seen, check, close

from . import messages
from .account_tool import account_for, blocking, failure
from .mailbox import Mailbox
from .search_tool import conversation, day, root_of
from .servers import MailError

#: The time a scheduled run may spend before leaving watches to the next one (the functions' timeout is 120 s).
BUDGET = 80.0


def as_http(exc: MailError) -> HttpError:
    """A mail failure as the shared watch library reads one: not_found closes the watch, anything else is a problem
    of this run."""
    return HttpError(exc.kind, exc.message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Accounts:
    """The account each watch was made on, asked for once per run however many watches share it."""

    def __init__(self, call):
        self.call, self.found = call, {}

    async def of(self, fields):
        named = str(fields.get("account") or "").lower()
        if named not in self.found:
            self.found[named] = await account_for(self.call, account=named, ask=False)
        account, why = self.found[named]
        if account is None:
            raise HttpError("auth", why)
        return account


class WatchTool(ToolBase):
    id = "watch"

    # -- reply watches ----------------------------------------------------------------------------------------------
    async def await_reply(self, call):
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
        subject = messages.header(found[0][0], "Subject")
        last, received = found[-1]
        thread_id = root_of(found)
        ref = await call.records.create("watch", {
            "thread_id": thread_id, "subject": subject, "since_message_id": messages.message_id(last),
            "since_at": messages.sent_at(last, received) or now(), "status": "waiting",
            "account": account.email.lower(), "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": ref, "subject": subject, "thread_id": thread_id,
                "since_message_id": messages.message_id(last)}, "success"

    @staticmethod
    def reply_since(found, fields, me):
        """The first message from someone else after the watch began: after its anchor message where that is still
        in the conversation, else after the anchor's time, else (a watch kept from before times were kept) after the
        account's own last message."""
        anchor = str(fields.get("since_message_id") or "")
        ids = [messages.message_id(m) for m, _ in found]
        if anchor in ids:
            later = found[ids.index(anchor) + 1:]
        elif fields.get("since_at"):
            since = messages.moment(str(fields["since_at"]))
            later = [(m, r) for m, r in found if messages.moment(messages.sent_at(m, r)) > since]
        else:
            mine = [n for n, (m, _) in enumerate(found) if messages.address(m.get("From")) == me]
            later = found[mine[-1] + 1:] if mine else []
        return next(((m, r) for m, r in later if messages.address(m.get("From")) != me), None)

    async def check(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            watches = [await call.records.read("watch", wanted)]
        else:
            watches = await call.records.list("watch", {"status": "waiting"})
        watches = [w for w in watches if w["fields"].get("status") == "waiting"]
        accounts = Accounts(call)

        async def read(watch):
            fields = watch["fields"]
            if "subject" not in fields:  # listed records carry only their keys
                fields = (await call.records.read("watch", watch["ref"]))["fields"]
            account = await accounts.of(fields)

            def conversation_now():
                with Mailbox(account) as mailbox:
                    return conversation(mailbox, str(fields.get("thread_id") or ""))

            try:
                found = await blocking(conversation_now)
            except MailError as exc:
                raise as_http(exc) from None
            reply = self.reply_since(found, fields, account.email.lower())
            entry = {"watch_ref": watch["ref"], "thread_id": str(fields.get("thread_id") or ""),
                     "subject": str(fields.get("subject") or "")}
            if reply is None:
                return Seen([{**entry, "answered": False, "note": str(fields.get("note") or "")}])
            return Seen([{**entry, "answered": True, "reply_from": messages.header(reply[0], "From"),
                          "reply_date": messages.sent_at(*reply), "message_id": messages.message_id(reply[0])}],
                        {"status": "replied"})

        found = await check(call, "watch", watches, read, budget=BUDGET,
                            name=lambda w: str(w["fields"].get("thread_id") or w["ref"]))
        unanswered = [{k: v for k, v in r.items() if k != "answered"} for r in found["rows"] if not r["answered"]]
        answered = [{k: v for k, v in r.items() if k != "answered"} for r in found["rows"] if r["answered"]]
        return {"checked": found["checked"], "unanswered": unanswered, "answered": answered,
                "problems": found["problems"], "closed": found["closed"], "more": found["more"]}, "success"

    # -- inbox watches ----------------------------------------------------------------------------------------------
    async def inbox(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        asked = str(call.inputs.get("since") or "").strip()
        since, note = None, ""
        if asked:
            try:
                since = datetime.fromisoformat(asked.replace("Z", "+00:00"))
            except ValueError:
                return {"error": f"since must be an ISO 8601 date-time, not {asked!r}.", "kind": "invalid"}, "error"
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            # A start in the future is a time written in the person's own zone and read as UTC. Nobody means to start
            # watching later tonight; it is taken as "from now", and said.
            if since > datetime.now(timezone.utc):
                note = (f"since {asked!r} is in the future, so the watch starts from now. Give a time with its "
                        f"offset, like 2026-09-20T17:51:00+04:00.")
                since = None

        def opened():
            with Mailbox(account) as mailbox:
                validity, following = mailbox.open(Mailbox.INBOX)
                last = following - 1
                if since is not None:
                    last = self._before(mailbox, since, last)
                return validity, last

        try:
            validity, last = await blocking(opened)
        except MailError as exc:
            return failure(exc)
        started = (since or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        ref = await call.records.create("inbox", {
            "since": started, "folder_validity": str(validity), "last_number": str(max(last, 0)),
            "status": "watching", "account": account.email.lower(),
            "only_from": self._senders(call.inputs.get("only_from")),
            "skip_from": self._senders(call.inputs.get("skip_from")), "note": str(call.inputs.get("note") or "")})
        result = {"inbox_ref": ref, "since": started}
        if note:
            result["note"] = note
        return result, "success"

    @staticmethod
    def _before(mailbox, since, newest):
        """The number just before the first message that arrived at or after ``since``. A mail server searches by
        the day; the hour is read from each message it names."""
        for uid in mailbox.search(["SINCE", day(since.isoformat())]):
            message, received, _, _ = mailbox.glance(uid)
            arrived = messages.moment(messages.received_at(received) or messages.sent_at(message))
            if arrived >= since:
                return uid - 1
        return newest

    async def new_mail(self, call):
        wanted = str(call.inputs.get("inbox_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.records.read("inbox", wanted)]
        else:
            watches = await call.records.list("inbox", {"status": "watching"})
        watches = [w for w in watches if w["fields"].get("status") == "watching"]
        accounts, notices = Accounts(call), []

        async def read(watch):
            fields, ref = watch["fields"], watch["ref"]
            account = await accounts.of(fields)
            me = account.email.lower()

            def arrived():
                with Mailbox(account) as mailbox:
                    validity, following = mailbox.open(Mailbox.INBOX)
                    renumbered = str(validity) != str(fields.get("folder_validity") or "")
                    if renumbered:
                        # The numbers kept mean nothing now: what arrived since the last check is found by its time.
                        since = messages.moment(str(fields.get("since") or ""))
                        uids = []
                        for uid in mailbox.search(["SINCE", day(since.isoformat())]):
                            message, received, seen, text = mailbox.glance(uid)
                            if messages.moment(messages.received_at(received) or messages.sent_at(message)) > since:
                                uids.append((uid, message, received, text))
                    else:
                        last = int(str(fields.get("last_number") or "0") or 0)
                        uids = [(uid, None, "", "") for uid in mailbox.after(last)]
                    more = len(uids) > most
                    uids = uids[:most]
                    rows = []
                    for uid, message, received, text in uids:
                        if message is None:
                            message, received, _, text = mailbox.glance(uid)
                        sender = messages.address(message.get("From"))
                        if sender != me and self._wanted(sender, fields):
                            rows.append({**messages.row(message, received, text), "inbox_ref": ref})
                    end = uids[-1][0] if uids else (following - 1 if renumbered else None)
                    return validity, renumbered, rows, end, more

            try:
                validity, renumbered, rows, end, more = await blocking(arrived)
            except MailError as exc:
                raise as_http(exc) from None
            cursor = {}
            if end is not None:
                cursor = {"last_number": str(max(end, 0)), "since": now()}
            if renumbered:
                cursor.update({"folder_validity": str(validity),
                               "note": "The mail server renumbered the inbox; what arrived since the last check was "
                                       "found by its time."})
                notices.append({"inbox_ref": ref, "notice": f"The mail server renumbered the inbox (its UIDVALIDITY "
                                                            f"changed), so this check found new mail by arrival time: "
                                                            f"{len(rows)} message(s) since {fields.get('since')}."})
            return Seen(rows, cursor or None, more=more)

        found = await check(call, "inbox", watches, read, budget=BUDGET,
                            name=lambda w: str(w["fields"].get("account") or w["ref"]))
        result = {"checked": found["checked"], "messages": found["rows"], "more": found["more"],
                  "problems": found["problems"], "closed": found["closed"]}
        if notices:
            result["notices"] = notices
        return result, "success"

    @staticmethod
    def _senders(text) -> str:
        """'Dana@x.example, @harbourline.example' → lower-case, tidy."""
        return ",".join(part.strip().lower() for part in str(text or "").split(",") if part.strip())

    @classmethod
    def _wanted(cls, sender: str, keys: dict) -> bool:
        only = [p for p in str(keys.get("only_from") or "").split(",") if p]
        skip = [p for p in str(keys.get("skip_from") or "").split(",") if p]
        if any(cls._sender_is(sender, p) for p in skip):
            return False
        return not only or any(cls._sender_is(sender, p) for p in only)

    @staticmethod
    def _sender_is(sender: str, pattern: str) -> bool:
        """An address matches itself; '@domain' matches the domain."""
        if pattern.startswith("@"):
            return sender.endswith(pattern)
        return sender == pattern


class WatchesTool(ToolBase):
    """Every watch of either kind, listed and stopped."""

    id = "watches"
    KINDS = (("watch", "reply"), ("inbox", "inbox"))

    async def list(self, call):
        closed = bool(call.inputs.get("include_closed"))
        rows = []
        for resource, kind in self.KINDS:
            for record in await call.records.list(resource):
                fields = record["fields"]
                status = str(fields.get("status") or "")
                if status == "closed" and not closed:
                    continue
                row = {"watch_ref": record["ref"], "kind": kind, "status": status,
                       "account": str(fields.get("account") or "")}
                if kind == "reply":
                    row["thread_id"] = str(fields.get("thread_id") or "")
                else:
                    row["read_up_to"] = str(fields.get("since") or "")
                rows.append(row)
        return {"watches": rows}, "success"

    async def close(self, call):
        ref = str(call.inputs["watch_ref"])
        for resource, _ in self.KINDS:
            try:
                return await close(call, resource, ref), "success"
            except ResourceDenied:
                continue
        return {"error": f"No watch {ref}: watches_list shows them.", "kind": "not_found"}, "error"
