"""Whose account this is, and whether Teams can be used with it."""

from standard import ToolBase
from standard.accounts import listed

from .client import HttpError, client_for


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        graph, refused = await client_for(call, ask=False)
        if graph is None:
            accounts = await listed(call, "microsoft")
            return {"connected": False, "email": "", "problem": refused["error"],
                    **({"accounts": accounts} if accounts else {})}, "success"
        try:
            me = await graph.me()
        except HttpError as failed:
            return {"connected": False, "email": graph.account, "problem": failed.message}, "success"
        return {"connected": True, "email": me.address or graph.account, "name": me.name,
                "accounts": await listed(call, "microsoft")}, "success"
