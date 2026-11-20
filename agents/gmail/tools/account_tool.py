"""Which Google accounts are connected, and whether one works."""

from standard import ResourceDenied, ToolBase
from standard.http import HttpError

from .gmail import gmail_for


def no_account(why: str):
    return {"error": why, "kind": "auth"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def list(self, call):
        """The accounts this agent may use, by address; never a token."""
        try:
            rows = await call.records.list("google")
        except ResourceDenied as refused:
            return {"error": f"The connected accounts could not be listed: {refused}", "kind": "auth"}, "error"
        return {"accounts": [{"account": str(row["fields"].get("account") or "")} for row in rows]}, "success"

    async def status(self, call):
        """A status asks nobody: with no account kept, it says so. It names the addresses the account sends as."""
        client, why = await gmail_for(call, ask=False)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            profile = await client.profile()
            aliases = await client.send_as()
        except HttpError as failed:
            return {"connected": False, "email": client.account, "problem": failed.message}, "success"
        result = {"connected": True, "email": str(profile.get("emailAddress") or client.account),
                  "send_as": [{"address": str(a.get("sendAsEmail") or ""), "name": str(a.get("displayName") or ""),
                               "default": a.get("isDefault") is True,
                               "has_signature": bool(str(a.get("signature") or "").strip())}
                              for a in aliases if a.get("sendAsEmail")]}
        return result, "success"
