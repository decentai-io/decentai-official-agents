from standard import ResourceDenied, ToolBase

from .graph_calendar import CalendarClient, GraphError
from .zones import resolve_zone


async def client_for(call, ask=True):
    """The client over the one connected account. None kept: the person
    is asked to connect one, unless ``ask`` is False (a status asks nobody)."""
    try:
        if not ask and not await call.records.list("microsoft"):
            return None, "No Microsoft account is connected to this agent."
        secret = await call.records.secret("microsoft")
    except ResourceDenied as exc:
        return None, f"No Microsoft account is connected to this agent: {exc}"
    return CalendarClient(secret), ""


def failure(exc: GraphError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call, ask=False)
        if client is None:
            return {"connected": False, "email": "", "timezone": "",
                    "problem": why}, "success"
        try:
            profile = client.profile()
            raw = client.mailbox_timezone()
        except GraphError as exc:
            return {"connected": False, "email": client.email, "timezone": "",
                    "problem": exc.message}, "success"
        _, name, problem = resolve_zone(raw)
        result = {"connected": True,
                  "email": str(profile.get("mail") or profile.get("userPrincipalName")
                               or client.email),
                  "timezone": "" if problem else name,
                  "mailbox_timezone": raw}
        if problem:
            result["problem"] = problem
        return result, "success"
