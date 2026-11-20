from standard import ToolBase
from standard.accounts import account
from standard.http import HttpError

from .dropbox_api import Dropbox

SECRET = "dropbox"
NOT_CONNECTED = "No Dropbox account is connected to this agent"


async def client_for(call, wanted=None, ask=True, root=None):
    """A client for the account named (the call's ``account``, or the one a watch remembers), its root read; or None
    and why. With none connected, the person is asked to connect one."""
    secret, why = await account(call, SECRET, wanted=wanted, ask=ask, service="Dropbox")
    if secret is None:
        return None, why or f"{NOT_CONNECTED}."
    return Dropbox(secret, root=root), ""


def not_connected(why):
    return {"error": why, "kind": "auth"}, "error"


async def ready(call):
    """The client, prepared (its team root read), or the error result."""
    client, why = await client_for(call)
    if client is None:
        return None, not_connected(why)
    try:
        return await client.prepared(), None
    except HttpError as failed:
        return None, failed.answer()


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        # A status asks nobody: with no account kept, it says so.
        if not await call.records.list(SECRET):
            return {"connected": False, "email": "", "problem": f"{NOT_CONNECTED}."}, "success"
        client, why = await client_for(call, ask=False)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            await client.prepared()
            found = await client.account()
            space = await client.space()
        except HttpError as failed:
            return {"connected": False, "email": client.email, "problem": failed.message,
                    "kind": failed.kind}, "success"
        allocation = space.get("allocation") or {}
        allocated = int(allocation.get("allocated") or 0)
        return {"connected": True, "email": str(found.get("email") or client.email),
                "team": str((found.get("team") or {}).get("name") or ""),
                "team_space": bool(client.root), "home_path": client.home_path,
                "used_bytes": int(space.get("used") or 0), "total_bytes": allocated}, "success"
