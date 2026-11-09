from standard import ResourceDenied, ToolBase

from .graph_workbook import GraphError, WorkbookClient


async def client_for(call, ask=True):
    """The client over the one connected account. None kept: the person
    is asked to connect one, unless ``ask`` is False (a status asks nobody)."""
    try:
        if not ask and not await call.records.list("microsoft"):
            return None, "No Microsoft account is connected to this agent."
        secret = await call.records.secret("microsoft")
    except ResourceDenied as exc:
        return None, f"No Microsoft account is connected to this agent: {exc}"
    return WorkbookClient(secret), ""


def failure(exc: GraphError):
    """A GraphError as the error result the assistant reads: the kind
    travels so it can tell an expired connection from a refusal from
    an outcome nobody knows."""
    return {"error": exc.message, "kind": exc.kind}, "error"


def no_client(why: str):
    return {"error": why, "kind": "auth"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call, ask=False)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            profile = client.profile()
        except GraphError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(profile.get("mail") or profile.get("userPrincipalName")
                             or client.email)}, "success"
