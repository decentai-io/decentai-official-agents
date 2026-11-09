from standard import ResourceDenied, ToolBase

from .slack_api import SlackClient, SlackError

NOT_CONNECTED = "No Slack account is connected to this agent"


async def client_for(call):
    """A client for the account kept; with none kept, the person is asked to connect one."""
    try:
        secret = await call.records.secret("slack")
    except ResourceDenied as exc:
        return None, f"{NOT_CONNECTED}: {exc}"
    return SlackClient(secret), ""


def failure(exc: SlackError):
    return {"error": exc.message, "kind": exc.kind}, "error"


def not_connected(why: str):
    return {"error": why, "kind": "auth"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        # A status asks nobody: with no account kept, it says so.
        if not await call.records.list("slack"):
            return {"connected": False, "problem": f"{NOT_CONNECTED}."}, "success"
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "problem": why}, "success"
        try:
            me = client.me()
        except SlackError as exc:
            return {"connected": False, "problem": exc.message}, "success"
        return {"connected": True, "team": str(me.get("team") or ""),
                "user": str(me.get("user") or ""),
                "user_id": str(me.get("user_id") or "")}, "success"
