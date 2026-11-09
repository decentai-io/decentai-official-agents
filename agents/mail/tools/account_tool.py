from standard import ResourceDenied, ToolBase

from .mailbox import Mailbox
from .servers import Account, MailError

SECRET = "account"


async def account_for(call, account=None):
    """The account to work on, or the reason there is none — one place,
    so every function words it the same way.

    A person may have connected more than one account. The one wanted
    is named by address — the function's ``account`` input, or the
    account a draft or a watch was made on — and matched among the
    accounts this agent may use. Unnamed, the only one answers; none
    yet, and the person is asked to connect one; several is refused,
    and the refusal names them so the assistant can ask."""
    wanted = str(account if account is not None
                 else call.inputs.get("account") or "").strip().lower()
    try:
        row = None
        if wanted:
            rows = await call.records.list(SECRET)
            row = _match(rows, wanted)
            if row is None and len(rows) > 1:
                return None, (f"No connected mail account matches {wanted!r}. "
                              f"Connected: {_names(rows)}.")
        secret = await call.records.secret(SECRET, row["ref"] if row is not None else None)
    except ResourceDenied as exc:
        rows = await call.records.list(SECRET)
        if len(rows) > 1:
            return None, (f"Several mail accounts are connected ({_names(rows)}): "
                          f"say which with account.")
        return None, f"No mail account is connected to this agent: {exc}"
    found = Account(secret)
    problems = found.problems()
    if problems:
        return None, " ".join(problems)
    return found, ""


def _match(rows, wanted):
    """The row whose address is the one asked for, else the one whose
    address contains it — a person says "work" as readily as an address."""
    for row in rows:
        if str(row["fields"].get("account") or "").lower() == wanted:
            return row
    for row in rows:
        if wanted in str(row["fields"].get("account") or "").lower():
            return row
    return None


def _names(rows):
    return ", ".join(str(row["fields"].get("account") or row["ref"]) for row in rows)


def _summary(row, only):
    """An account by address; the default is the one answering when none is named: the only one."""
    return {"account": str(row["fields"].get("account") or ""),
            "provider": str(row["fields"].get("provider") or ""),
            "default": only}


def failure(exc: MailError):
    """A MailError as the error result the assistant reads: the kind
    travels so it can tell a refused sign-in from a refusal of the
    platform's from an outcome nobody knows."""
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def list(self, call):
        """The accounts this agent may use, by address — never a
        password."""
        try:
            rows = await call.records.list(SECRET)
        except ResourceDenied as exc:
            return {"error": f"The connected accounts could not be listed: {exc}",
                    "kind": "auth"}, "error"
        return {"accounts": [_summary(row, len(rows) == 1) for row in rows]}, "success"

    async def status(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            with Mailbox(account) as mailbox:
                mailbox.open(Mailbox.INBOX)
        except MailError as exc:
            return {"connected": False, "email": account.email,
                    "problem": exc.message}, "success"
        return {"connected": True, "email": account.email}, "success"
