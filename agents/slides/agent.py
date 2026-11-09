"""The Presentations agent (see manifest.yaml).

Reads the decks a person uploaded, slide by slide and honestly about
slides that carry only pictures; produces decks from an outline, in the
company's own template when one is given, and reads every one back
before returning it. Stateless: a deck read is handed in or kept by the
person, a deck produced is handed to the chat, and noted as a record.
"""

from standard import AgentBase

from .tools import ProduceTool, ReadTool


class SlidesAgent(AgentBase):
    def tools(self):
        return [ReadTool(self), ProduceTool(self)]
