"""Reactions on a message, as the person: everyone in the conversation sees them, so both are level 3."""

import re

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, failure, not_connected
from .slack_api import is_ts

EMOJI = re.compile(r"^[a-z0-9_+\-']{1,100}(::skin-tone-[2-6])?$")


class ReactionsTool(ToolBase):
    id = "reactions"

    async def add(self, call):
        return await self._react(call, True)

    async def remove(self, call):
        return await self._react(call, False)

    async def _react(self, call, add):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        ts = str(call.inputs["ts"]).strip()
        emoji = str(call.inputs["emoji"]).strip().strip(":").lower()
        if not is_ts(ts):
            return {"error": f"ts is a Slack ts such as 1712345678.123456, not {ts!r}.", "kind": "invalid"}, "error"
        if not EMOJI.match(emoji):
            return {"error": f"emoji is a Slack emoji name such as thumbsup or white_check_mark, not {emoji!r}.",
                    "kind": "invalid"}, "error"
        try:
            changed = await client.react(channel, ts, emoji, add)
        except HttpError as exc:
            return failure(exc)
        return {"conversation_id": channel, "ts": ts, "emoji": emoji, "changed": changed}, "success"
