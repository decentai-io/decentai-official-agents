"""The Mail agent (see manifest.yaml).

Stateless: every call signs in to the account's own mail server, does its work off the agent's loop, and signs out.
What is kept is kept by the platform: drafts, their attached files, and watches, as records.
"""

from standard import AgentBase

from .tools import AccountTool, DraftTool, FoldersTool, SearchTool, WatchesTool, WatchTool


class MailAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), SearchTool(self), FoldersTool(self), DraftTool(self), WatchTool(self),
                WatchesTool(self)]
