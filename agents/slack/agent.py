"""The Slack agent (see manifest.yaml).

Reads the conversations a person is in, finds messages and says whether
anyone answered; sends as the person only as a level-3 action that waits
for the user, keeping what it sent; and watches mentions and direct
messages so a schedule hands on only what arrived.
"""

from standard import AgentBase

from .tools import AccountTool, ConversationsTool, MessagesTool, WatchTool


class SlackAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), ConversationsTool(self), MessagesTool(self), WatchTool(self)]
