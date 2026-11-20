from standard import ToolBase
from standard.http import HttpError

from .notion_api import client_for


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        # A status asks nobody: with no workspace kept, it says so.
        client, why = await client_for(call, ask=False)
        if client is None:
            return {"connected": False, "workspace": "", "problem": why}, "success"
        try:
            me = await client.me()
        except HttpError as failed:
            return {"connected": False, "workspace": "", "problem": failed.message}, "success"
        bot = me.get("bot") or {}
        result = {"connected": True, "workspace": str(bot.get("workspace_name") or ""),
                  "integration": str(me.get("name") or "")}
        if client.account:
            result["account"] = client.account
        return result, "success"
