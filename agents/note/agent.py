"""The Note agent — the example: one agent, every feature the platform
can enforce (see manifest.yaml and README.md).

Stateless by design: every note, document and secret flows through
``call.records`` — the platform's data layer, held per call to what the
function declared — so the agent itself holds nothing. The sync REMOTE
stays simulated (no network), but its connection secret is real.

Copy this file and rename the class: the program finds the one agent
class in agents/<id>/agent.py.
"""

from standard import AgentBase

from .tools import ArchiveTool, NotesTool, SyncTool


class NoteAgent(AgentBase):
    def tools(self):
        return [NotesTool(self), ArchiveTool(self), SyncTool(self)]
