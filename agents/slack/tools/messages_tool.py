"""Finding messages, whether someone answered, and sending on the person's go-ahead.

Sending is level 3 in the manifest, so by the time it runs here it has waited for the person. Every message sent is
kept as a ``sent`` record holding the ts Slack gave it, which is what messages_answered takes.
"""

from urllib.parse import parse_qs, urlparse

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, failure, not_connected
from .people_tool import resolve
from .slack_api import (People, conversation_name, is_said, is_ts, kind_of, message_row, permalink, ts_number,
                        ts_time)

MOST_ROWS = 25


async def search_row(match, people: People):
    """One search.messages match, shaped like a message row plus where it was said. Search does not return
    thread_ts; its permalink does."""
    channel = match.get("channel") or {}
    link = str(match.get("permalink") or "")
    thread_ts = (parse_qs(urlparse(link).query).get("thread_ts") or [""])[0]
    kind = kind_of(channel)
    name = str(channel.get("name") or channel.get("id") or "")
    if kind == "dm" and name[:1] in ("U", "W"):
        # A DM's "name" in search is the other person's user id.
        name = await people.name(name)
    ts = str(match.get("ts") or "")
    return {"conversation_id": str(channel.get("id") or ""), "conversation": name, "kind": kind, "ts": ts,
            "thread_ts": thread_ts, "sent": ts_time(ts), "author": await people.author(match),
            "author_id": str(match.get("user") or ""), "text": await people.readable(match.get("text")),
            "link": link}


class MessagesTool(ToolBase):
    id = "messages"

    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        query = str(call.inputs["query"]).strip()
        most = int(call.inputs.get("max_results") or 20)
        if not query:
            return {"error": "Give something to search for.", "kind": "invalid"}, "error"
        try:
            found = await client.search(query, most)
            people = People(client)
            rows = [await search_row(m, people) for m in (found.get("matches") or [])[:most]]
        except HttpError as exc:
            return failure(exc)
        paging = found.get("paging") or {}
        total = int(found.get("total") or paging.get("total") or len(rows))
        return {"messages": rows, "total": total, "more": total > len(rows)}, "success"

    async def answered(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        ts = str(call.inputs["ts"]).strip()
        if not is_ts(ts):
            return {"error": f"ts is a Slack ts such as 1712345678.123456, not {ts!r}.", "kind": "invalid"}, "error"
        after = ts_number(ts)
        try:
            me = await client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            thread = (await client.replies(channel, ts, 200)).get("messages") or []
            target = next((m for m in thread if str(m.get("ts")) == ts), None)
            if target is None:
                return {"error": f"There is no message {ts} in {channel}.", "kind": "not_found"}, "error"
            root = str(target.get("thread_ts") or ts)
            if root != ts:
                # The message is itself a reply: its thread is its parent's.
                thread = (await client.replies(channel, root, 200)).get("messages") or []
            later = (await client.history(channel, 100, oldest=ts)).get("messages") or []
            seen, answers = set(), []
            for where, messages in (("thread", thread), ("conversation", later)):
                for message in messages:
                    key = str(message.get("ts") or "")
                    if (key in seen or ts_number(key) <= after or not is_said(message)
                            or str(message.get("user") or "") == me_id):
                        continue
                    seen.add(key)
                    answers.append((where, message))
            answers.sort(key=lambda pair: ts_number(pair[1].get("ts")))
            people = People(client)
            rows = [{**await message_row(m, channel, people, me_id, team_url), "where": where}
                    for where, m in answers[:MOST_ROWS]]
        except HttpError as exc:
            return failure(exc)
        by = []
        for row in rows:
            if row["author"] not in by:
                by.append(row["author"])
        return {"answered": bool(answers), "by": by, "replies": rows, "more": len(answers) > MOST_ROWS}, "success"

    async def send(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        text = str(call.inputs["text"]).strip()
        thread_ts = str(call.inputs.get("thread_ts") or "").strip()
        if not text:
            return {"error": "There is nothing to send.", "kind": "invalid"}, "error"
        if thread_ts and not is_ts(thread_ts):
            return {"error": f"thread_ts is a Slack ts such as 1712345678.123456, not {thread_ts!r}.",
                    "kind": "invalid"}, "error"
        try:
            me = await client.me()
            # Read before writing: an unknown conversation is refused here, and the record names where it went.
            name = await conversation_name(await client.conversation(channel), People(client), client,
                                           str(me.get("user_id") or ""))
            await call.progress(f"Sending to {name}")
        except HttpError as exc:
            return failure(exc)
        return await self._post(call, client, me, channel, name, text, thread_ts)

    async def direct(self, call):
        """A direct message to one person, named by address, name or user id: found, the conversation opened (it
        may never have been), then sent."""
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        to = str(call.inputs["to"]).strip()
        text = str(call.inputs["text"]).strip()
        if not text:
            return {"error": "There is nothing to send.", "kind": "invalid"}, "error"
        try:
            me = await client.me()
            person, candidates, problem = await resolve(client, to)
            if person is None:
                answer = {"error": problem, "kind": "invalid" if candidates else "not_found"}
                return ({**answer, "candidates": candidates} if candidates else answer), "error"
            if person["user_id"] == str(me.get("user_id") or ""):
                return {"error": "That is the person's own account: a message to themselves is not sent.",
                        "kind": "invalid"}, "error"
            await call.progress(f"Sending to {person['name']}")
            channel = await client.open_direct(person["user_id"])
        except HttpError as exc:
            return failure(exc)
        return await self._post(call, client, me, channel, person["name"], text, "",
                                extra={"user_id": person["user_id"]})

    async def _post(self, call, client, me, channel, name, text, thread_ts, extra=None):
        try:
            made = await client.post_message(channel, text, thread_ts)
        except HttpError as exc:
            return failure(exc)
        ts = str(made.get("ts") or "")
        channel = str(made.get("channel") or channel)
        link = permalink(str(me.get("url") or ""), channel, ts, thread_ts)
        sent_ref = await call.records.create("sent", {
            "account": client.account, "conversation_id": channel, "conversation": name, "thread_ts": thread_ts,
            "text": text, "ts": ts, "sent_at": ts_time(ts), "link": link})
        return {"sent_ref": sent_ref, "conversation_id": channel, "conversation": name, "ts": ts,
                "thread_ts": thread_ts, "link": link, **(extra or {})}, "success"
