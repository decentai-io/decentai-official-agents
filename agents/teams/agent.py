"""The Microsoft Teams agent (see manifest.yaml).

Reads a work or school account's chats and channels as text; sends a chat message, posts to a channel or replies
in one only as a level-3 action that waits for the person, one POST each, recorded whatever its outcome; tells what
was missed; and watches chats for new messages on a schedule.
"""

from standard import AgentBase

from .tools import AccountTool, ChannelsTool, ChatsTool, TeamsTool, WatchesTool


class TeamsAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), ChatsTool(self), TeamsTool(self), ChannelsTool(self), WatchesTool(self)]
