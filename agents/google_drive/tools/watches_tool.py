"""The person's watches, of every kind: listed, and closed for good."""

from __future__ import annotations

from standard import ResourceDenied, ToolBase
from standard.watch import close, listing

from .common import refused
from .watching import describe


class WatchesTool(ToolBase):
    id = "watches"

    async def list(self, call):
        found = await listing(call, "watch", describe, include_closed=call.inputs.get("include_closed") is True)
        kind = str(call.inputs.get("kind") or "")
        return {"watches": [w for w in found if not kind or w.get("kind") == kind]}, "success"

    async def close(self, call):
        try:
            return await close(call, "watch", str(call.inputs["watch_ref"])), "success"
        except ResourceDenied as refusal:
            return refused(f"No such watch: {refusal}", "not_found")
