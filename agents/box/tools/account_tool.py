from standard import ResourceDenied, ToolBase

from .box_api import BoxClient, BoxError

NOT_CONNECTED = "No Box account is connected to this agent"


async def client_for(call):
    """A client for the account kept; with none kept, the person is asked to connect one."""
    try:
        secret = await call.records.secret("box")
    except ResourceDenied as exc:
        return None, f"{NOT_CONNECTED}: {exc}"
    return BoxClient(secret), ""


def failure(exc: BoxError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        # A status asks nobody: with no account kept, it says so.
        if not await call.records.list("box"):
            return {"connected": False, "email": "", "problem": f"{NOT_CONNECTED}."}, "success"
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            me = client.me()
        except BoxError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(me.get("login") or client.email),
                "enterprise": str((me.get("enterprise") or {}).get("name") or ""),
                "used_bytes": int(me.get("space_used") or 0),
                "total_bytes": int(me.get("space_amount") or 0)}, "success"
