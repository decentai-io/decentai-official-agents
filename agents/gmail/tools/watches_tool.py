"""Every watch this agent keeps, listed, and any of them stopped for good."""

from standard import ResourceDenied, ToolBase
from standard.watch import close

KINDS = {"reply": "watch", "inbox": "inbox"}
LIVE = {"active", "waiting", "watching"}


class WatchesTool(ToolBase):
    id = "watches"

    async def list(self, call):
        include_closed = bool(call.inputs.get("include_closed"))
        rows = []
        for kind, resource in KINDS.items():
            for record in await call.records.list(resource):
                status = str(record["fields"].get("status") or "active")
                if status not in LIVE and not include_closed:
                    continue
                fields = (await call.records.read(resource, record["ref"]))["fields"]
                row = {"watch_ref": record["ref"], "kind": kind,
                       "status": "active" if status in LIVE else status,
                       "account": str(fields.get("account") or "")}
                if kind == "reply":
                    row.update(thread_id=str(fields.get("thread_id") or ""), subject=str(fields.get("subject") or ""))
                else:
                    row.update(since=str(fields.get("since") or ""), only_from=str(fields.get("only_from") or ""),
                               skip_from=str(fields.get("skip_from") or ""))
                if fields.get("note"):
                    row["note"] = str(fields["note"])
                rows.append(row)
        return {"watches": rows}, "success"

    async def close(self, call=None):
        if call is None:  # the agent closing: this tool holds nothing
            return None
        ref = str(call.inputs["watch_ref"])
        for kind, resource in KINDS.items():
            try:
                closed = await close(call, resource, ref)
            except ResourceDenied as refused:
                if refused.code in ("not_found", "invalid", ""):
                    continue
                return {"error": str(refused), "kind": refused.code}, "error"
            return {**closed, "kind": kind}, "success"
        return {"error": f"No watch {ref}: list them with watches_list.", "kind": "not_found"}, "error"
