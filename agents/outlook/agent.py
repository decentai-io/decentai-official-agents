"""The Outlook agent (see manifest.yaml): mail, calendar and To Do of a Microsoft 365 or Outlook.com account.

Stateless: every request carries the access token the platform handed it for the connected account, every draft,
proposal and watch is a platform record, and nothing is kept in the process. It never sends on its own: a send, an
invitation, a cancellation or an answer to one is a separate level-3 function the person approves.
"""

from standard import AgentBase

from .tools import AccountTool, CalendarTool, DraftTool, MailTool, TasksTool, WatchesTool, WatchTool


class OutlookAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), MailTool(self), DraftTool(self), CalendarTool(self), TasksTool(self),
                WatchTool(self), WatchesTool(self)]
