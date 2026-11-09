from standard import ResourceDenied, ToolBase

from .graph_drive import DriveClient, GraphError


async def client_for(call, ask=True):
    """The client over the one connected account. None kept: the person
    is asked to connect one, unless ``ask`` is False (a status asks nobody)."""
    try:
        if not ask and not await call.records.list("microsoft"):
            return None, "No Microsoft account is connected to this agent."
        secret = await call.records.secret("microsoft")
    except ResourceDenied as exc:
        return None, f"No Microsoft account is connected to this agent: {exc}"
    return DriveClient(secret), ""


def failure(exc: GraphError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call, ask=False)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            profile = client.profile()
            drive = client.drive()
        except GraphError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        quota = drive.get("quota") or {}
        return {"connected": True,
                "email": str(profile.get("mail") or profile.get("userPrincipalName")
                             or client.email),
                "drive_type": str(drive.get("driveType") or ""),
                "used_bytes": int(quota.get("used") or 0),
                "total_bytes": int(quota.get("total") or 0)}, "success"
