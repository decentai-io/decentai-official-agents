"""This repository's own library for the agents standard (decentai documentation/agents.md §9), carried inside each
agent's program. The platform has no SDK: this is the publisher's code, and it needs nothing but Python.

    agent.py    AgentBase, ToolBase, ResourceDenied: an agent of tools, each function a method
    call.py     Call, Said, Records, Screen: one call's inputs, and record.*, model, chat and network.open within it
    program.py  Program: ready, the messages in and out, calls run together on the agent's loop
    net.py      Tunnel, TunnelRefused, BaseAddress: connections through the sandbox's way out

For the agents that connect an account, imported where needed (they need ``requests``):

    http.py     Http, HttpError: async HTTP off the agent's loop, busy services waited out, writes never sent twice
    graph.py    Graph, Me, graph_for: Microsoft Graph, its ids, its accounts' addresses, its query rules
    google.py   Google, google_for: Google's APIs, their rate limits and page tokens, shared drives
    accounts.py account: which of several connected accounts a call works on
    watch.py    check, Seen, Gone, listing, close: watches that hand on what is new once, and never lose it
"""

from .agent import AgentBase, ResourceDenied, ToolBase
from .call import Said, Call, Records, Screen
from .net import BaseAddress, Tunnel, TunnelRefused
from .program import Program

__all__ = ["AgentBase", "BaseAddress", "Call", "Program", "Records", "ResourceDenied", "Said", "Screen", "ToolBase", "Tunnel",
           "TunnelRefused"]
