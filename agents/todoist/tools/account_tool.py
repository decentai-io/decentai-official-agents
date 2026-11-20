from standard import ToolBase
from standard.accounts import account
from standard.http import HttpError

from .todoist_api import Todoist

SECRET = "todoist"


async def client_for(call, wanted=None, ask=True):
    """The Todoist client over the account wanted (the call's ``account`` input by default), or None and why. With
    none connected the person is asked to connect one, unless ``ask`` is False."""
    secret, why = await account(call, SECRET, wanted, ask=ask, service="Todoist")
    if secret is None:
        return None, why
    return Todoist(secret), ""


def not_connected(why):
    return {"error": why, "kind": "auth"}, "error"


def failure(exc: HttpError):
    """An HttpError as the error result the assistant reads: the kind travels so it can tell a revoked connection
    from a refusal, a busy service, or an outcome nobody knows."""
    return exc.answer()


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        # A status asks nobody: with no account kept, it says so.
        client, why = await client_for(call, ask=False)
        if client is None:
            return {"connected": False, "email": "", "full_name": "", "timezone": "", "problem": why}, "success"
        try:
            user = await client.user()
        except HttpError as exc:
            return {"connected": False, "email": client.account, "full_name": "", "timezone": "",
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(user.get("email") or client.account),
                "full_name": str(user.get("full_name") or ""),
                "timezone": str((user.get("tz_info") or {}).get("timezone") or "")}, "success"
