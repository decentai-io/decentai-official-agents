"""Every watch this agent keeps, listed, and any of them stopped for good."""

from __future__ import annotations

from standard import ResourceDenied, ToolBase
from standard.watch import close

from .common import guarded

KINDS = {"reply": "watch", "inbox": "inbox", "tasks": "taskwatch"}


def described(kind: str, fields: dict) -> dict:
    found = {"kind": kind, "account": str(fields.get("account") or "")}
    if kind == "reply":
        found.update(thread_id=str(fields.get("thread_id") or ""), subject=str(fields.get("subject") or ""),
                     since=str(fields.get("since") or ""))
    elif kind == "inbox":
        found.update(folder=str(fields.get("folder_name") or fields.get("folder") or ""),
                     since=str(fields.get("since") or ""), only_from=str(fields.get("only_from") or ""),
                     skip_from=str(fields.get("skip_from") or ""))
    else:
        found.update(list=str(fields.get("list_name") or "(every list)"), report=str(fields.get("report") or ""))
    if fields.get("note"):
        found["note"] = str(fields["note"])
    return {k: v for k, v in found.items() if v != "" or k in ("kind", "account")}


class WatchesTool(ToolBase):
    id = "watches"

    @guarded
    async def list(self, call):
        include_closed = bool(call.inputs.get("include_closed"))
        rows = []
        for kind, resource in KINDS.items():
            for record in await call.records.list(resource):
                status = str(record["fields"].get("status") or "active")
                if status != "active" and not include_closed:
                    continue
                fields = (await call.records.read(resource, record["ref"]))["fields"]
                rows.append({"watch_ref": record["ref"], "status": status, **described(kind, fields)})
        return {"watches": rows}, "success"

    async def close(self, call=None):
        if call is None:  # the agent closing: this tool holds nothing
            return None
        return await self._close(call)

    @guarded
    async def _close(self, call):
        ref = str(call.inputs["watch_ref"])
        for kind, resource in KINDS.items():
            try:
                closed = await close(call, resource, ref)
            except ResourceDenied as refused:
                if refused.code in ("not_found", "invalid", ""):
                    continue
                raise
            return {**closed, "kind": kind}, "success"
        return {"error": f"There is no watch {ref}: list them with watches_list.", "kind": "not_found"}, "error"
