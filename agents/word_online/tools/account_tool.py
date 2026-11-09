from standard import ResourceDenied, ToolBase

from .docx_file import WordPackage
from .graph_word import GraphError, WordClient, is_docx

#: How large a document the agent downloads to read. The bytes stay
#: inside its program — they are never handed on — so this only
#: keeps one call's memory and time reasonable.
MAX_READ_BYTES = 25 * 1024 * 1024


async def client_for(call, ask=True):
    """The client over the one connected account. None kept: the person
    is asked to connect one, unless ``ask`` is False (a status asks nobody)."""
    try:
        if not ask and not await call.records.list("microsoft"):
            return None, "No Microsoft account is connected to this agent."
        secret = await call.records.secret("microsoft")
    except ResourceDenied as exc:
        return None, f"No Microsoft account is connected to this agent: {exc}"
    return WordClient(secret), ""


def failure(exc: GraphError):
    return {"error": exc.message, "kind": exc.kind}, "error"


def open_document(client, item_id: str):
    """(item, package) for a Word file, or raise GraphError saying why not."""
    item = client.item(item_id)
    name = str(item.get("name") or "")
    if not is_docx(item):
        raise GraphError("invalid", f"'{name}' is not a Word (.docx) document.")
    size = int(item.get("size") or 0)
    if size > MAX_READ_BYTES:
        raise GraphError("too_large", f"'{name}' is {size // 1024:,} KB, more than the "
                                      f"{MAX_READ_BYTES // 1024:,} KB this agent reads.")
    raw = client.content(item_id)
    try:
        return item, WordPackage(raw)
    except Exception as exc:
        raise GraphError("invalid", f"'{name}' could not be read as a Word document: {exc}")


class AccountTool(ToolBase):
    id = "account"

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
