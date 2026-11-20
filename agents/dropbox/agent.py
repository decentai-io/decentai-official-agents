"""The Dropbox agent (see manifest.yaml).

Dropbox API v2 on the shared HTTP client: reads the account (its team
space too) freely, brings copies into the chat, changes the Dropbox only
on approval (uploads, folders, moves and restores at level 2; deletes and
shares at level 3), and watches folders so a schedule hands on only what
changed in them.
"""

from standard import AgentBase

from .tools import AccountTool, ChangesTool, FilesTool, SharingTool, WatchesTool


class DropboxAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FilesTool(self), SharingTool(self), ChangesTool(self), WatchesTool(self)]
