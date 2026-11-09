"""The Web Watch agent (see manifest.yaml).

Watches public pages for a change that matters — any change to the
text, a phrase appearing or disappearing, or the value a pattern picks
out — and, run on a schedule, reports only the pages that changed.
Each watch is a record holding its last reading; a page that could not
be fetched is counted, never mistaken for a change.
"""

from standard import AgentBase

from .tools import WatchesTool


class WebWatchAgent(AgentBase):
    def tools(self):
        return [WatchesTool(self)]
