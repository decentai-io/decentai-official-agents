import asyncio

from standard import ResourceDenied, ToolBase
from standard.accounts import account as account_secret

from .mailbox import Mailbox
from .servers import Account, MailError

SECRET = "account"


async def account_for(call, account=None, ask=True):
    """The account to work on, or the reason there is none, worded the same way by every function.

    A person may have connected more than one account. The one wanted is named by address: the function's
    ``account`` input, or the account a draft or a watch was made on; matched exactly, then by a part of it ("work"),
    among the accounts this agent may use. Unnamed, the only one answers; none yet, and the person is asked to connect
    one; several is refused, naming them so the assistant can ask."""
    secret, why = await account_secret(call, SECRET, account, ask, "mail")
    if secret is None:
        return None, why
    found = Account(secret)
    problems = found.problems()
    if problems:
        return None, " ".join(problems)
    return found, ""


def failure(exc: MailError):
    """A MailError as the error result the assistant reads: the kind travels so it can tell a refused sign-in from a
    refusal of the platform's from an outcome nobody knows."""
    return {"error": exc.message, "kind": exc.kind}, "error"


async def blocking(work, *arguments):
    """Work that speaks to a mail server, off the agent's loop: every socket, IMAP and SMTP call goes through here."""
    return await asyncio.to_thread(work, *arguments)


class AccountTool(ToolBase):
    id = "account"

    async def list(self, call):
        """The accounts this agent may use, by address: never a password."""
        try:
            rows = await call.records.list(SECRET)
        except ResourceDenied as exc:
            return {"error": f"The connected accounts could not be listed: {exc}", "kind": "auth"}, "error"
        return {"accounts": [{"account": str(row["fields"].get("account") or ""),
                              "provider": str(row["fields"].get("provider") or ""),
                              "default": len(rows) == 1} for row in rows]}, "success"

    async def status(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"connected": False, "email": "", "problem": why}, "success"

        def opened():
            with Mailbox(account) as mailbox:
                mailbox.open(Mailbox.INBOX)

        try:
            await blocking(opened)
        except MailError as exc:
            return {"connected": False, "email": account.email, "problem": exc.message}, "success"
        return {"connected": True, "email": account.email}, "success"
