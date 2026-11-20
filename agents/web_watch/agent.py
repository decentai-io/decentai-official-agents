"""The Web Watch agent (see manifest.yaml).

Watches public pages and feeds for the change that matters: any change
to the text or to one part of it, a phrase, a pattern's value, a number
crossing a threshold, or a feed's new items. Run on a schedule, it hands
each change on once, through ``standard.watch``: a watch that fails is
listed apart and loses nothing, and one whose page is gone closes.
"""

from standard import AgentBase

from .tools import FeedsTool, WatchesTool


class WebWatchAgent(AgentBase):
    def tools(self):
        return [WatchesTool(self), FeedsTool(self)]
