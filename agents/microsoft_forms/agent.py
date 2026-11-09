"""The Microsoft Forms agent (see manifest.yaml).

Microsoft Forms has no supported API for forms or their responses, so
this agent reads the one place a form's responses are kept that Graph
does serve: an Excel workbook in OneDrive whose table gains a row per
response. It finds such workbooks, lists responses, and watches one so
a schedule hands on only the responses that arrived. It
changes nothing, in Forms or in the workbook.
"""

from standard import AgentBase

from .tools import AccountTool, FormsTool, ResponsesTool


class MicrosoftFormsAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FormsTool(self), ResponsesTool(self)]
