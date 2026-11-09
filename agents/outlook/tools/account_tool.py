from standard import ResourceDenied, ToolBase

from .graph import GraphClient, GraphError


async def client_for(call, account=None, ask=True):
    """The Graph client over the connected account, or the reason
    there is none — one place, so every function words it the same way.

    A person may have connected more than one account. The one wanted
    is named by address — the function's ``account`` input, or the
    account a draft or a watch was made on — and matched among the
    accounts this agent may use. Unnamed, the only one answers; several
    are refused, and the refusal names them so the assistant can ask.
    None kept: the person is asked to connect one, unless ``ask`` is
    False (a status asks nobody)."""
    wanted = str(account if account is not None
                 else call.inputs.get("account") or "").strip().lower()
    try:
        rows = await call.records.list("microsoft")
        row = _match(rows, wanted) if wanted else None
        if row is None and len(rows) > 1:
            if wanted:
                return None, (f"No connected Microsoft account matches "
                              f"{wanted!r}. Connected: {_names(rows)}.")
            return None, (f"Several Microsoft accounts are connected "
                          f"({_names(rows)}): say which with account.")
        if not rows and not ask:
            return None, "No Microsoft account is connected to this agent."
        # One account connected is the one, whatever it was called
        # when the draft or the watch remembered it.
        secret = await call.records.secret(
            "microsoft", (row or rows[0])["ref"] if rows else None)
    except ResourceDenied as exc:
        return None, f"No Microsoft account is connected to this agent: {exc}"
    return GraphClient(secret), ""


def _account(row):
    return str(row["fields"].get("account") or "")


def _match(rows, wanted):
    """The row whose address is the one asked for, else the one whose
    address contains it — a person says "work" as readily as an address."""
    for row in rows:
        if _account(row).lower() == wanted:
            return row
    for row in rows:
        if wanted in _account(row).lower():
            return row
    return None


def _names(rows):
    return ", ".join(_account(row) or row["ref"] for row in rows)


def failure(exc: GraphError):
    """A GraphError as the error result the assistant reads: the kind
    travels so it can tell an expired connection from a refusal from
    an outcome nobody knows."""
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def list(self, call):
        """The accounts this agent may use, by address — never a token."""
        try:
            rows = await call.records.list("microsoft")
        except ResourceDenied as exc:
            return {"error": f"The connected accounts could not be listed: {exc}",
                    "kind": "auth"}, "error"
        return {"accounts": [{"account": _account(row)} for row in rows]}, "success"

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
