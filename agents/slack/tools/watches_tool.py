"""The person's watches, listed, and closed for good: a schedule that checks a closed one hears nothing more."""

from standard import ResourceDenied, ToolBase
from standard.watch import close

from .slack_api import ts_time
from .watch_tool import status_of


class WatchesTool(ToolBase):
    id = "watches"

    async def list(self, call):
        closed_too = bool(call.inputs.get("include_closed"))
        named = str(call.inputs.get("account") or "").strip().lower()
        rows = []
        for record in await call.records.list("watch"):
            fields, status = record["fields"], status_of(record)
            if (status != "active" and not closed_too) or named not in str(fields.get("account") or "").lower():
                continue
            since = str(fields.get("since") or fields.get("cursor") or "")
            rows.append({"watch_ref": record["ref"], "status": status, "which": str(fields.get("which") or "both"),
                         "account": str(fields.get("account") or ""), "since": ts_time(since) if since else "",
                         "skip_conversations": str(fields.get("skip_conversations") or "")})
        return {"watches": rows}, "success"

    async def close(self, call):
        try:
            closed = await close(call, "watch", str(call.inputs["watch_ref"]))
        except ResourceDenied as exc:
            return {"error": f"There is no such watch: {exc}", "kind": exc.code or "not_found"}, "error"
        return closed, "success"
