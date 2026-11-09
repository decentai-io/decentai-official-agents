from standard import ResourceDenied, ToolBase

from .forms_api import FormsClient, GoogleError


async def client_for(call, ask=True):
    """The Forms client over the one Google account kept for this
    agent, or the reason there is none — one place, so every function
    words it the same way. With none kept, the person is asked to
    connect one, unless ``ask`` is False."""
    try:
        kept = await call.records.list("google")
        if len(kept) > 1:
            named = ", ".join(str(r["fields"].get("account") or r["ref"]) for r in kept)
            return None, (f"Several Google accounts are connected to this agent ({named}); "
                          "it works with one, so leave only that one connected to it.")
        if not kept and not ask:
            return None, "No Google account is connected to this agent."
        secret = await call.records.secret("google", kept[0]["ref"] if kept else None)
    except ResourceDenied as exc:
        return None, f"No Google account is connected to this agent: {exc}"
    return FormsClient(secret), ""


def failure(exc: GoogleError):
    """A GoogleError as the error result the assistant reads: the kind
    travels so it can tell an expired connection from a refusal."""
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        """A status asks nobody: with no account kept, it says so."""
        client, why = await client_for(call, ask=False)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            about = client.about()
        except GoogleError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str((about.get("user") or {}).get("emailAddress") or client.email)}, "success"
