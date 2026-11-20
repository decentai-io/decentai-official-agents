"""The Spreadsheets agent (see manifest.yaml).

Every number is read the way its column writes it and added in code, every match is a rule the person gave, every
produced file is read back before it is returned, and the original is never modified. JSON and JSON Lines files are
read, checked against a schema, compared and turned into tables here too.
"""

from standard import AgentBase

from .tools import ArrangeTool, CalcTool, CheckTool, CleanTool, CombineTool, JsonTool, ReadTool, ReconcileTool


class SheetsAgent(AgentBase):
    def tools(self):
        return [ReadTool(self), CheckTool(self), ReconcileTool(self), CleanTool(self), CombineTool(self),
                CalcTool(self), ArrangeTool(self), JsonTool(self)]
