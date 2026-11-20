"""Which accounts are connected, and what one of them is: its addresses, work or personal, and its time zone."""

from __future__ import annotations

from standard import ResourceDenied, ToolBase
from standard.accounts import named
from standard.graph import graph_for
from standard.http import HttpError

from .common import guarded, today, told, zone_for


class AccountTool(ToolBase):
    id = "account"

    @guarded
    async def list(self, call):
        try:
            rows = await call.records.list("microsoft")
        except ResourceDenied as refused:
            return {"error": f"The connected accounts could not be listed: {refused}", "kind": "auth"}, "error"
        return {"accounts": [{"account": named(r)} for r in rows]}, "success"

    @guarded
    async def status(self, call):
        graph, why = await graph_for(call, None, ask=False)
        if graph is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        call.clients.append(graph)
        try:
            me = await graph.me()
            personal = await graph.personal()
            zone = await zone_for(graph, call.inputs, soft=True)
        except HttpError as failed:
            return {"connected": False, "email": graph.account, "problem": failed.message}, "success"
        return {"connected": True, "email": me.address, "addresses": sorted(me.mine),
                "kind": "personal" if personal else "work or school", "today": today(zone).isoformat(),
                **told(zone)}, "success"
