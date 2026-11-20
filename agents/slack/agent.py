"""The Slack agent (see manifest.yaml).

Reads the conversations a person is in, finds messages and people, says whether anyone answered, and brings a
shared file into the chat; sends, reacts and messages someone directly as the person only as level-3 actions that
wait for them, keeping what it sent; and watches mentions and direct messages so a schedule hands on only what
arrived.
"""

from standard import AgentBase

from .tools import (AccountTool, ConversationsTool, FilesTool, MessagesTool, PeopleTool, ReactionsTool, WatchTool,
                    WatchesTool)


class SlackAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), ConversationsTool(self), FilesTool(self), MessagesTool(self), PeopleTool(self),
                ReactionsTool(self), WatchTool(self), WatchesTool(self)]
