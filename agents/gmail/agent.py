"""The Gmail agent (see manifest.yaml).

Stateless: every Gmail call takes its access token from the connected account, every draft and watch is a platform
record, and nothing is kept in the process. The agent never sends on its own: a send is a separate level-3 function
on a draft the person has seen.
"""

from standard import AgentBase

from .tools import AccountTool, DraftTool, MailTool, SearchTool, WatchesTool, WatchTool


class GmailAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), SearchTool(self), DraftTool(self), MailTool(self), WatchTool(self),
                WatchesTool(self)]
