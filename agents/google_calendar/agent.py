"""The Google Calendar agent (see manifest.yaml): Google Calendar and Google Tasks of one or several Google accounts,
as Google's own app pairs them.

Every Google call takes its access token from the connected account named by ``account``. Finding a time is
arithmetic over free/busy, never a model's guess; booking is a separate level-3 action that re-checks the slot first.
Tasks watches hand on what was completed or changed once, passing over this agent's own edits.
"""

from standard import AgentBase

from .tools import AccountTool, CalendarsTool, EventsTool, ListsTool, TasksTool, WatchesTool


class GoogleCalendarAgent(AgentBase):
    def __init__(self):
        #: Each calendar's zone, by (account, calendar id): read from Google once per chat.
        self.zones: dict[tuple[str, str], str] = {}
        super().__init__()

    def tools(self):
        return [AccountTool(self), CalendarsTool(self), EventsTool(self), ListsTool(self), TasksTool(self),
                WatchesTool(self)]
