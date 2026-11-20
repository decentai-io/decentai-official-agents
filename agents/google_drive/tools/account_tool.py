"""The connected Google accounts: which, how full, and whether there is an organization to share with."""

from __future__ import annotations

from standard import ResourceDenied, ToolBase

from .common import HttpError, addresses, connect, refused


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        """A status asks nobody: with no account kept, it says so."""
        clients, why = await connect(call, ask=False)
        connected = await addresses(call)
        if clients is None:
            return {"connected": False, "accounts": connected, "problem": why}, "success"
        try:
            about = await clients.drive.get("about", params={"fields": "user(emailAddress,displayName),"
                                                                       "storageQuota(limit,usage)"})
            domain = await clients.domain()
        except HttpError as error:
            return {"connected": False, "account": clients.address, "accounts": connected,
                    "problem": error.message, "kind": error.kind}, "success"
        quota = about.get("storageQuota") or {}
        result = {"connected": True, "accounts": connected,
                  "account": str((about.get("user") or {}).get("emailAddress") or clients.address),
                  "personal": not domain}
        if domain:
            result["organization"] = domain
        if quota.get("usage") not in (None, ""):
            result["used_bytes"] = int(quota["usage"])
        # A pooled or unlimited Workspace plan states no limit; none is made up.
        if quota.get("limit") not in (None, ""):
            result["total_bytes"] = int(quota["limit"])
        return result, "success"

    async def connect(self, call):
        """Another Google account (or the first): the person signs in on the platform's own form."""
        try:
            ref = await call.records.create("google", {})
            secret = (await call.records.read("google", ref))["fields"]
        except ResourceDenied as refusal:
            return refused(f"No account was connected: {refusal}", "refused")
        address = str(secret.get("account") or "")
        return {"connected": True, "account": address, "accounts": await addresses(call)}, "success"
