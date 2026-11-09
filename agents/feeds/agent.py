"""The Feeds agent (see manifest.yaml).

Subscribes to RSS and Atom feeds — or finds the feed a web page
advertises — and hands on what is new since the last look, oldest
first, each item naming its feed. Each subscription is a record holding
its cursor; run on a schedule, it reports only the items that arrived.
"""

from standard import AgentBase

from .tools import FeedsTool


class FeedsAgent(AgentBase):
    def tools(self):
        return [FeedsTool(self)]
