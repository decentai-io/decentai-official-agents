"""The Google Drive agent (see manifest.yaml): a person's Drive, shared drives included, and the Docs, Sheets and
Forms in it, over one or several connected Google accounts."""

from standard import AgentBase

from .tools import (AccountTool, ChangesTool, CommentsTool, DocsTool, EditsTool, FilesTool, FormsTool, ResponsesTool,
                    RowsTool, SharingTool, SheetsTool, WatchesTool)


class GoogleDriveAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FilesTool(self), SharingTool(self), ChangesTool(self), DocsTool(self),
                EditsTool(self), CommentsTool(self), SheetsTool(self), RowsTool(self), FormsTool(self),
                ResponsesTool(self), WatchesTool(self)]
