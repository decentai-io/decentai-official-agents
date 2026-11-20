"""The Notion agent (see manifest.yaml).

One Notion workspace, as far as the person shared it with the
integration while signing in: pages and databases found, read and
queried freely; pages, rows and blocks written or trashed on the
person's go-ahead (level 2), comments, which others see, at level 3;
a database watched for new and changed rows on a schedule.
"""

from standard import AgentBase

from .tools import (AccountTool, BlocksTool, CommentsTool, DatabasesTool,
                    PagesTool, SearchTool, WatchesTool, WatchTool)


class NotionAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), SearchTool(self), PagesTool(self),
                DatabasesTool(self), BlocksTool(self), CommentsTool(self),
                WatchTool(self), WatchesTool(self)]
