"""Which Microsoft accounts are connected, each with its drive's kind and how full it is. Asks nobody."""

from __future__ import annotations

from standard import ResourceDenied, ToolBase
from standard.accounts import named
from standard.http import HttpError

from .drive import Drive

MOST = 10


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        try:
            rows = await call.records.list("microsoft")
        except ResourceDenied as refused:
            return {"connected": False, "accounts": [], "problem": f"No Microsoft account is connected: {refused}"}, \
                "success"
        if not rows:
            return {"connected": False, "accounts": [],
                    "problem": "No Microsoft account is connected to this agent."}, "success"
        wanted = str(call.inputs.get("account") or "").strip().lower()
        accounts = []
        for row in rows[:MOST]:
            if wanted and wanted not in named(row).lower():
                continue
            entry = {"account": named(row) or row["ref"]}
            try:
                drive = Drive(await call.records.secret("microsoft", row["ref"]))
                found = await drive.drive()
                quota = found.get("quota") or {}
                entry.update(connected=True, drive_type=str(found.get("driveType") or ""),
                             personal=await drive.personal(), used_bytes=int(quota.get("used") or 0),
                             total_bytes=int(quota.get("total") or 0))
            except (HttpError, ResourceDenied) as failed:
                entry.update(connected=False, problem=str(getattr(failed, "message", failed)))
            accounts.append(entry)
        return {"connected": any(a.get("connected") for a in accounts), "accounts": accounts}, "success"
