"""The person's completion watches: listed, and stopped for good."""

from standard import ResourceDenied, ToolBase
from standard.watch import close, listing

ACTIVE = ("active", "watching")   # "watching" is what 1.0.0 wrote


def describe(record):
    fields = record["fields"]
    return {"project": str(fields.get("project") or ""), "account": str(fields.get("account") or ""),
            "since": str(fields.get("since") or ""), "source": str(fields.get("source") or "completed")}


class WatchesTool(ToolBase):
    id = "watches"

    async def list(self, call):
        wanted = str(call.inputs.get("account") or "").strip().lower()
        everything = bool(call.inputs.get("include_closed"))
        rows = []
        for row in await listing(call, "watch", describe, include_closed=True):
            row["status"] = "active" if row["status"] in ACTIVE else row["status"]
            if (everything or row["status"] == "active") and (not wanted or wanted in row["account"].lower()):
                rows.append(row)
        return {"watches": rows, "total": len(rows)}, "success"

    async def close(self, call=None):
        if call is None:   # the agent letting go of its tools as its process ends
            return None
        try:
            return await close(call, "watch", str(call.inputs["watch_ref"])), "success"
        except ResourceDenied as exc:
            return {"error": f"There is no watch {call.inputs['watch_ref']}: {exc}",
                    "kind": exc.code or "not_found"}, "error"
