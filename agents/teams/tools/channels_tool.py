"""Teams and their channels: listed, read (where an administrator has consented to it), posted to and replied in on
the person's go-ahead."""

from __future__ import annotations

from standard import ToolBase

from .client import (PAGE, HttpError, TeamsGraph, body_for, client_for, exact, is_message, message_row, pick_people,
                     segment)
from .sending import deliver


def even(most: int) -> int:
    from .chats_tool import even as evened
    return evened(most)

ADMIN = ("Reading channel messages needs Microsoft's ChannelMessage.Read.All permission, which only an administrator "
         "of the organization can consent to, and the Microsoft sign-in this agent shares with the other Microsoft "
         "agents does not ask for it. Posting to a channel and replying in one work without it. ({words})")


def channel_refusal(failed: HttpError) -> HttpError:
    if failed.kind in ("scope", "denied"):
        return HttpError("scope", ADMIN.format(words=failed.message), failed.status)
    return failed


async def read_replies(graph: TeamsGraph, team_id: str, channel_id: str, message_id: str,
                       pages: int = 10) -> list[dict]:
    """Every reply to a channel message, oldest first, page by page (50 a page, 10 pages at most)."""
    path = f"teams/{segment(team_id)}/channels/{segment(channel_id)}/messages/{segment(message_id)}/replies"
    try:
        found = await graph.pages(path, params={"$top": PAGE}, most=PAGE * pages)
    except HttpError as failed:
        raise channel_refusal(failed) from None
    return sorted(found, key=lambda m: exact(m.get("createdDateTime")))


async def people_in_chats(graph: TeamsGraph, pages: int = 3) -> list[dict]:
    """The members of the account's recent chats: where a channel mention finds a person's Teams id, since this
    agent may not list a team's members."""
    from .chats_tool import chats_page

    members, answer, read = {}, await chats_page(graph), 1
    while True:
        for chat in answer.get("value") or []:
            for member in chat.get("members") or []:
                members.setdefault(str(member.get("userId") or ""), member)
        following = str(answer.get("@odata.nextLink") or "")
        if not following or read >= pages:
            return [m for k, m in members.items() if k]
        answer = await chats_page(graph, absolute=following)
        read += 1


class TeamsTool(ToolBase):
    id = "teams"

    async def list(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        try:
            teams = await graph.pages("me/joinedTeams")
        except HttpError as failed:
            return failed.answer()
        return {"teams": [{"team_id": str(t.get("id") or ""), "name": str(t.get("displayName") or ""),
                           "description": str(t.get("description") or "")} for t in teams]}, "success"


class ChannelsTool(ToolBase):
    id = "channels"

    async def list(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        try:
            channels = await graph.pages(f"teams/{segment(call.inputs['team_id'])}/channels")
        except HttpError as failed:
            return failed.answer()
        return {"channels": [{"channel_id": str(c.get("id") or ""), "name": str(c.get("displayName") or ""),
                              "kind": str(c.get("membershipType") or "standard")} for c in channels]}, "success"

    async def read(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        team_id, channel_id = str(call.inputs["team_id"]), str(call.inputs["channel_id"])
        most = int(call.inputs.get("max_results") or 20)
        token = str(call.inputs.get("page_token") or "")
        try:
            me = await graph.my_id()
            if token:
                answer = await graph.get("", absolute=token)
            else:
                answer = await graph.get(f"teams/{segment(team_id)}/channels/{segment(channel_id)}/messages",
                                         params={"$top": even(most)})
            found = []
            while True:
                found += answer.get("value") or []
                following = str(answer.get("@odata.nextLink") or "")
                if not following or len(found) >= most:
                    break
                answer = await graph.get("", absolute=following)
        except HttpError as failed:
            return channel_refusal(failed).answer()
        found.sort(key=lambda m: exact(m.get("createdDateTime")))
        rows = [message_row(m, {}, me) for m in found if is_message(m)]
        result = {"team_id": team_id, "channel_id": channel_id, "messages": rows}
        if following:
            result["next_page_token"] = following
        return result, "success"

    async def replies(self, call):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        try:
            me = await graph.my_id()
            found = await read_replies(graph, str(call.inputs["team_id"]), str(call.inputs["channel_id"]),
                                       str(call.inputs["message_id"]))
        except HttpError as failed:
            return failed.answer()
        return {"message_id": str(call.inputs["message_id"]),
                "replies": [message_row(m, {}, me) for m in found if is_message(m)]}, "success"

    async def post(self, call):
        return await self._send(call, reply_to="")

    async def reply(self, call):
        return await self._send(call, reply_to=str(call.inputs["message_id"]))

    async def _send(self, call, reply_to: str):
        graph, refused = await client_for(call)
        if graph is None:
            return refused, "error"
        team_id, channel_id = str(call.inputs["team_id"]), str(call.inputs["channel_id"])
        text = str(call.inputs["text"]).strip()
        asked = [str(m) for m in call.inputs.get("mention") or []]
        if not text:
            return {"error": "There is nothing to post.", "kind": "invalid"}, "error"
        try:
            teams = await graph.pages("me/joinedTeams")
            team = next((t for t in teams if str(t.get("id")) == team_id), None)
            if team is None:
                return {"error": "The account is not in that team; teams_list shows the ones it is in. Nothing was "
                                 "posted.", "kind": "not_found"}, "error"
            try:
                channel = await graph.get(f"teams/{segment(team_id)}/channels/{segment(channel_id)}")
            except HttpError as failed:
                if failed.kind != "not_found":
                    raise
                return {"error": "That team has no such channel; channels_list lists them. Nothing was posted.",
                        "kind": "not_found"}, "error"
            mentioned = []
            if asked:
                mentioned, problem = pick_people(asked, await people_in_chats(graph))
                if problem:
                    return {"error": f"A person is mentioned by their Teams id, which this agent finds among the "
                                     f"people in the account's chats: {problem}. Nothing was posted.",
                            "kind": "invalid"}, "error"
        except HttpError as failed:
            return failed.answer()
        where_said = f"{team.get('displayName')} / {channel.get('displayName')}"
        payload = body_for(text, mentioned)
        path = f"teams/{segment(team_id)}/channels/{segment(channel_id)}/messages"
        if reply_to:
            path += f"/{segment(reply_to)}/replies"
        else:
            subject = str(call.inputs.get("subject") or "").strip()
            payload = {**({"subject": subject} if subject else {}), **payload}
        where = {"kind": "reply" if reply_to else "channel", "chat_id": channel_id, "team_id": team_id,
                 "parent_id": reply_to}
        return await deliver(call, graph, where=where, to=where_said, text=text, path=path, payload=payload,
                             again=bool(call.inputs.get("again")))
