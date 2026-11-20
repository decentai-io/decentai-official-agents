from standard import ResourceDenied, ToolBase
from standard.accounts import account
from standard.http import HttpError

from .slack_api import SlackClient

NOT_CONNECTED = "No Slack account is connected to this agent"


async def client_for(call, wanted=None, ask=True):
    """A client for the account named (the ``account`` input, or the one a watch remembers); with none kept, the
    person is asked to connect one."""
    secret, why = await account(call, "slack", wanted, ask, "Slack")
    if secret is None:
        return None, why
    return SlackClient(secret), ""


def failure(exc: HttpError):
    return exc.answer()


def not_connected(why: str):
    return {"error": why, "kind": "auth"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        # A status asks nobody: with no account kept, it says so.
        try:
            kept = await call.records.list("slack")
        except ResourceDenied as exc:
            return {"connected": False, "problem": f"{NOT_CONNECTED}: {exc}"}, "success"
        if not kept:
            return {"connected": False, "problem": f"{NOT_CONNECTED}."}, "success"
        wanted = str(call.inputs.get("account") or "").strip()
        rows = []
        for record in kept:
            named = str(record["fields"].get("account") or "")
            if wanted and wanted.lower() not in named.lower():
                continue
            row = {"account": named}
            try:
                client = SlackClient(await call.records.secret("slack", record["ref"]))
                me = await client.me()
                row.update({"connected": True, "team": str(me.get("team") or ""), "user": str(me.get("user") or ""),
                            "user_id": str(me.get("user_id") or "")})
            except (HttpError, ResourceDenied) as exc:
                row.update({"connected": False, "problem": getattr(exc, "message", str(exc))})
            rows.append(row)
        if not rows:
            return {"connected": False, "problem": f"No connected Slack account matches {wanted!r}."}, "success"
        first = rows[0]
        return {**{k: v for k, v in first.items() if k != "account"}, "accounts": rows}, "success"
