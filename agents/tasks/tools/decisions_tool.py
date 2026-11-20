from standard import ToolBase

from .agreement_rows import agreement_row
from .clock import day_or_today


class DecisionsTool(ToolBase):
    id = "decisions"

    async def record(self, call):
        inputs = call.inputs
        ref = str(inputs["agreement_ref"])
        agreement = agreement_row(await call.records.read("agreement", ref))
        if agreement["status"] != "active":
            return {"error": f"The agreement is {agreement['status']}; there is nothing left to decide."}, "error"
        decided_on, _, problem = day_or_today(inputs, "decided_on")
        if problem:
            return {"error": problem}, "error"
        decision = str(inputs["decision"])
        by = str(inputs["by"]).strip()
        decision_ref = await call.records.create("decision", {
            "agreement_ref": ref, "decision": decision, "by": by, "decided_on": decided_on.isoformat(),
            "note": str(inputs.get("note") or "")})
        await call.records.update("agreement", ref, {"decision": decision, "decision_by": by})
        return {"decision_ref": decision_ref, "agreement_ref": ref, "decision": decision,
                "status": "active"}, "success"
