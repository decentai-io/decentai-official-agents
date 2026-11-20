"""The conversations the person is in, and what was said in them.

Everything here is a read. Answers stay small: at most 25 messages, the text of each clipped, and ``more`` says
whether there is more, without guessing.
"""

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, failure, not_connected
from .slack_api import People, conversation_name, is_said, is_ts, kind_of, message_row, ts_number

TYPES = "public_channel,private_channel,mpim,im"
#: Pages read when ``oldest`` is given, to reach the first messages after it.
OLDEST_PAGES = 5


def bad_ts(label, value):
    return {"error": f"{label} is a Slack ts such as 1712345678.123456, not {value!r}.", "kind": "invalid"}, "error"


class ConversationsTool(ToolBase):
    id = "conversations"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        most = int(call.inputs.get("max_results") or 50)
        wanted = str(call.inputs.get("kind") or "").strip()
        try:
            me_id = str((await client.me()).get("user_id") or "")
            found = await client.my_conversations(TYPES)
            if wanted:
                found = [c for c in found if kind_of(c) == wanted]
            people = People(client)
            rows = [{"conversation_id": str(c.get("id") or ""), "kind": kind_of(c),
                     "name": await conversation_name(c, people, client, me_id)} for c in found[:most]]
        except HttpError as exc:
            return failure(exc)
        return {"conversations": rows, "more": len(found) > most}, "success"

    async def messages(self, call):
        """The newest messages; with ``oldest`` alone, the first ones after it (pages run newest first, so every
        page back to ``oldest`` is read)."""
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        most = int(call.inputs.get("max_results") or 20)
        oldest = str(call.inputs.get("oldest") or "").strip()
        latest = str(call.inputs.get("latest") or "").strip()
        for label, value in (("oldest", oldest), ("latest", latest)):
            if value and not is_ts(value):
                return bad_ts(label, value)
        note = ""
        try:
            me = await client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            people = People(client)
            name = await conversation_name(await client.conversation(channel), people, client, me_id)
            if oldest and not latest:
                found, done = await client.window(channel, oldest, pages=OLDEST_PAGES)
                found = [m for m in found if is_said(m)]
                more = len(found) > most or not done
                if not done:
                    note = ("More messages came after oldest than one answer reads: these are the earliest read. "
                            "Give latest to read the ones before them.")
                found = found[:most]
            else:
                page = await client.history(channel, most + 1, oldest=oldest, latest=latest)
                found = [m for m in page.get("messages") or [] if is_said(m)]
                more = len(found) > most or bool(page.get("has_more"))
                found = sorted(found[:most], key=lambda m: ts_number(m.get("ts")))
            rows = [await message_row(m, channel, people, me_id, team_url) for m in found]
        except HttpError as exc:
            return failure(exc)
        return {"conversation_id": channel, "name": name, "messages": rows, "more": more,
                **({"note": note} if note else {})}, "success"

    async def replies(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        thread_ts = str(call.inputs["thread_ts"]).strip()
        most = int(call.inputs.get("max_results") or 20)
        if not is_ts(thread_ts):
            return bad_ts("thread_ts", thread_ts)
        try:
            me = await client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            # The parent comes first and is counted; one more than asked for says whether the thread goes on.
            page = await client.replies(channel, thread_ts, most + 1)
            found = [m for m in page.get("messages") or [] if is_said(m)]
            people = People(client)
            rows = [await message_row(m, channel, people, me_id, team_url) for m in found[:most]]
        except HttpError as exc:
            return failure(exc)
        return {"conversation_id": channel, "thread_ts": thread_ts, "messages": rows,
                "more": len(found) > most or bool(page.get("has_more"))}, "success"
