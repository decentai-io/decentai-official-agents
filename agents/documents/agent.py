"""The Documents agent (see manifest.yaml).

Reads PDFs, Word files, decks, pictures and text to their end, page by page, and reads what only a picture holds
through the model's picture input, marked as such; extracts fields over the whole document in windows, each value
checked against its quote and each quote against its page; compares versions in code; produces Word, PDF and
PowerPoint files in Arabic as well as Latin script, read back before they are returned; and merges, splits, rotates
and fills PDF forms.
"""

from standard import AgentBase

from .tools import CompareTool, PdfTool, ProduceTool, ReadTool


class DocumentsAgent(AgentBase):
    def tools(self):
        return [ReadTool(self), CompareTool(self), ProduceTool(self), PdfTool(self)]
