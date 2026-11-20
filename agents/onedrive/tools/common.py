"""What every tool of the agent shares: the client for a call's account, and the refusals it says out loud."""

from __future__ import annotations

from typing import Any

from standard.http import HttpError

from .drive import Drive, drive_for

WORK_ONLY = ("{what} needs a work or school (Microsoft 365) account; {account} is a personal Microsoft account, where "
             "Microsoft does not offer it.")


def refused(words: str, kind: str = "invalid", **extra: Any) -> tuple[dict[str, Any], str]:
    return {"error": words, "kind": kind, **extra}, "error"


def failed(error: HttpError, about: str = "") -> tuple[dict[str, Any], str]:
    """A Graph failure as the result the assistant reads, its kind kept so it can tell a reconnect from a refusal."""
    words = error.message
    if error.kind == "locked":
        words = (f"{about or 'The file'} is locked: someone has it open or checked out in Word, Excel or OneDrive, "
                 f"and Microsoft refused the change ({error.message}). Nothing was changed; try again once they "
                 f"close it.")
    return {"error": words, "kind": error.kind}, "error"


async def client(call: Any, wanted: Any = None, ask: bool = True) -> tuple[Drive | None, tuple[dict, str] | None]:
    """(the client, None), or (None, the refusal to return)."""
    drive, why = await drive_for(call, wanted, ask)
    if drive is None:
        return None, refused(why, "auth")
    return drive, None


async def work_only(drive: Drive, what: str) -> tuple[dict, str] | None:
    """The refusal of a work-only feature on a personal account, or None."""
    if await drive.personal():
        return refused(WORK_ONLY.format(what=what, account=drive.account or "This account"), "unsupported")
    return None


def ids(call: Any, item: str = "item_id", drive: str = "drive_id") -> tuple[str, str]:
    return str(call.inputs.get(item) or "").strip(), str(call.inputs.get(drive) or "").strip()
