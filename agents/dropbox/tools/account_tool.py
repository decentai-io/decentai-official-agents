from standard import ResourceDenied, ToolBase

from .dropbox_api import DropboxClient, DropboxError

NOT_CONNECTED = "No Dropbox account is connected to this agent"


async def client_for(call):
    """A client for the account kept; with none kept, the person is asked to connect one."""
    try:
        secret = await call.records.secret("dropbox")
    except ResourceDenied as exc:
        return None, f"{NOT_CONNECTED}: {exc}"
    return DropboxClient(secret), ""


def failure(exc: DropboxError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        # A status asks nobody: with no account kept, it says so.
        if not await call.records.list("dropbox"):
            return {"connected": False, "email": "", "problem": f"{NOT_CONNECTED}."}, "success"
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            account = client.account()
            space = client.space()
        except DropboxError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        allocation = space.get("allocation") or {}
        return {"connected": True,
                "email": str(account.get("email") or client.email),
                "team": str((account.get("team") or {}).get("name") or ""),
                "used_bytes": int(space.get("used") or 0),
                "total_bytes": int(allocation.get("allocated") or 0)}, "success"
