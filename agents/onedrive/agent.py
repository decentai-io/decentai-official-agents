"""The OneDrive agent (see manifest.yaml): a person's OneDrive, and the Word, Excel and Forms files in it.

Reads are free; a download comes into the chat as its file; changes to the person's own drive (upload, folder,
move, copy, delete to the recycle bin, a Word edit, cells, rows) are level 2; sharing is level 3. Watches on a
document, a table or a form's responses are checked by one schedulable function.
"""

from standard import AgentBase

from .tools import AccountTool, ExcelTool, FilesTool, FormsTool, SharingTool, WatchesTool, WordTool


class OneDriveAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FilesTool(self), SharingTool(self), WordTool(self), ExcelTool(self),
                FormsTool(self), WatchesTool(self)]
