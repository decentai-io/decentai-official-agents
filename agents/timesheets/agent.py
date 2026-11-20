"""The Timesheets agent (see manifest.yaml).

Where the week went, by project: hours logged by hand or turned from
calendar meetings by the user's own rules or run on a timer, a week
summed with its gaps (checked on a schedule if wanted),
locked when it is handed in, and a workbook to send.
"""

from standard import AgentBase

from .tools import EntriesTool, ProjectsTool, TimerTool, WeeksTool


class TimesheetsAgent(AgentBase):
    def tools(self):
        return [ProjectsTool(self), EntriesTool(self), TimerTool(self), WeeksTool(self)]
