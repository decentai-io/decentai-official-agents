"""Organizing mail: labels, archive, read and unread, stars, the trash. Each acts on whole conversations
(``thread_ids``, as Gmail shows them) or on single messages (``message_ids``), and says which were done and which
were not, and why."""

from __future__ import annotations

from typing import Any

from standard import ToolBase
from standard.http import HttpError, segment

from .account_tool import no_account
from .gmail import Gmail, gathered, gmail_for

MARKS = {"read": ([], ["UNREAD"]), "unread": (["UNREAD"], []), "starred": (["STARRED"], []),
         "unstarred": ([], ["STARRED"]), "important": (["IMPORTANT"], []), "not_important": ([], ["IMPORTANT"])}
#: Labels Gmail keeps for itself: they say where mail is, and are not put on or taken off by hand.
SYSTEM_ONLY = {"DRAFT", "SENT", "CHAT"}


def targets(call) -> tuple[list[str], list[str]] | None:
    threads = [str(t) for t in call.inputs.get("thread_ids") or [] if str(t)]
    messages = [str(m) for m in call.inputs.get("message_ids") or [] if str(m)]
    return (threads, messages) if threads or messages else None


NO_TARGET = ({"error": "Name the conversations (thread_ids) or the messages (message_ids) to act on.",
              "kind": "invalid"}, "error")


async def resolve(client: Gmail, names: list[str], create: bool) -> tuple[list[str], list[str]]:
    """Label ids for the names given (a user label's name, any case, or a system label's id), and those unknown."""
    labels = await client.labels()
    by_name = {str(l.get("name") or "").lower(): str(l.get("id")) for l in labels}
    by_id = {str(l.get("id")).lower(): str(l.get("id")) for l in labels}
    ids, unknown = [], []
    for name in names:
        key = str(name).strip().lower()
        found = by_name.get(key) or by_id.get(key)
        if found is None and create:
            made = await client.post("labels", json={"name": str(name).strip(), "labelListVisibility": "labelShow",
                                                     "messageListVisibility": "show"})
            found = str(made.get("id") or "")
            client._labels = None
        if found:
            ids.append(found)
        else:
            unknown.append(str(name))
    return ids, unknown


async def modify(client: Gmail, threads: list[str], messages: list[str], add: list[str],
                 remove: list[str]) -> dict[str, Any]:
    body = {"addLabelIds": add, "removeLabelIds": remove}
    done, failed = [], []
    answers = await gathered(client.post(f"threads/{segment(t)}/modify", json=body) for t in threads)
    for thread_id, answer in zip(threads, answers):
        if isinstance(answer, HttpError):
            failed.append({"thread_id": thread_id, "kind": answer.kind, "error": answer.message})
        else:
            done.append({"thread_id": thread_id})
    for start in range(0, len(messages), 1000):
        chunk = messages[start:start + 1000]
        try:
            await client.post("messages/batchModify", json={"ids": chunk, **body})
            done += [{"message_id": m} for m in chunk]
        except HttpError as refused:
            failed += [{"message_id": m, "kind": refused.kind, "error": refused.message} for m in chunk]
    return {"done": done, "failed": failed}


async def each(client: Gmail, threads: list[str], messages: list[str], action: str) -> dict[str, Any]:
    done, failed = [], []
    jobs = [("thread_id", t, client.post(f"threads/{segment(t)}/{action}")) for t in threads]
    jobs += [("message_id", m, client.post(f"messages/{segment(m)}/{action}")) for m in messages]
    answers = await gathered(job for _, _, job in jobs)
    for (kind, ref, _), answer in zip(jobs, answers):
        if isinstance(answer, HttpError):
            failed.append({kind: ref, "kind": answer.kind, "error": answer.message})
        else:
            done.append({kind: ref})
    return {"done": done, "failed": failed}


def outcome(result: dict[str, Any], **extra: Any):
    if result["failed"] and not result["done"]:
        first = result["failed"][0]
        return {"error": f"Nothing was changed: {first['error']}", "kind": first["kind"],
                "failed": result["failed"]}, "error"
    return {**result, **extra}, "success"


class MailTool(ToolBase):
    id = "mail"

    async def labels(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        try:
            labels = await client.labels()
        except HttpError as failed:
            return failed.answer()
        rows = [{"label_id": str(l.get("id")), "name": str(l.get("name") or l.get("id")),
                 "type": str(l.get("type") or "user")} for l in labels]
        rows.sort(key=lambda r: (r["type"] != "user", r["name"].lower()))
        return {"labels": rows}, "success"

    async def label(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        chosen = targets(call)
        if chosen is None:
            return NO_TARGET
        add_names = [str(n) for n in call.inputs.get("add") or []]
        remove_names = [str(n) for n in call.inputs.get("remove") or []]
        if not add_names and not remove_names:
            return {"error": "Name the labels to add or to remove.", "kind": "invalid"}, "error"
        try:
            add, unknown = await resolve(client, add_names, bool(call.inputs.get("create_missing")))
            remove, unknown_removed = await resolve(client, remove_names, False)
            if unknown:
                known = ", ".join(str(l.get("name")) for l in await client.labels() if l.get("type") == "user")
                return {"error": f"No label named {', '.join(unknown)} (labels: {known or 'none yet'}). Pass "
                                 f"create_missing to make it.", "kind": "not_found"}, "error"
            fixed = sorted({*add, *remove} & SYSTEM_ONLY)
            if fixed:
                return {"error": f"{', '.join(fixed)} are kept by Gmail itself and cannot be put on or taken off.",
                        "kind": "invalid"}, "error"
            result = await modify(client, *chosen, add, remove)
        except HttpError as failed:
            return failed.answer()
        extra = {"not_labelled_already": unknown_removed} if unknown_removed else {}
        return outcome(result, added=add_names, removed=[n for n in remove_names if n not in unknown_removed], **extra)

    async def archive(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        chosen = targets(call)
        if chosen is None:
            return NO_TARGET
        try:
            result = await modify(client, *chosen, [], ["INBOX"])
        except HttpError as failed:
            return failed.answer()
        return outcome(result, note="Archived: out of the inbox, still in All Mail and in search. Undo with "
                                    "mail_label adding INBOX.")

    async def mark(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        chosen = targets(call)
        if chosen is None:
            return NO_TARGET
        add, remove = MARKS[str(call.inputs["as"])]
        try:
            result = await modify(client, *chosen, add, remove)
        except HttpError as failed:
            return failed.answer()
        return outcome(result, marked=str(call.inputs["as"]))

    async def trash(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        chosen = targets(call)
        if chosen is None:
            return NO_TARGET
        try:
            result = await each(client, *chosen, "trash")
        except HttpError as failed:
            return failed.answer()
        return outcome(result, note="Moved to the trash. Gmail keeps it there for 30 days, then deletes it for good; "
                                    "until then mail_restore brings it back.")

    async def restore(self, call):
        client, why = await gmail_for(call)
        if client is None:
            return no_account(why)
        chosen = targets(call)
        if chosen is None:
            return NO_TARGET
        try:
            result = await each(client, *chosen, "untrash")
        except HttpError as failed:
            return failed.answer()
        return outcome(result)
