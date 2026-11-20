"""The Todoist agent (see manifest.yaml).

Stateless: every Todoist call carries the token the platform handed it for the connected account, and the only
thing kept is a completion watch's cursor, as a platform record. Changes to the person's own Todoist are level 2;
deleting a task and commenting (which the people a project is shared with see) are level 3.
"""

from standard import AgentBase

from .tools import (AccountTool, CommentsTool, LabelsTool, ProjectsTool, SectionsTool, SyncTool, TasksTool,
                    WatchesTool)


class TodoistAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), ProjectsTool(self), SectionsTool(self), LabelsTool(self), TasksTool(self),
                CommentsTool(self), SyncTool(self), WatchesTool(self)]
