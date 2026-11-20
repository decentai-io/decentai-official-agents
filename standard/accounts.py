"""Which connected account a call works on, when a person may have connected several.

The account is named by its address: the function's ``account`` input, or the account a draft or a watch was made
on. Matched exactly first, then by a part of it that names one account only ("work" for ahmed@work.example).
Unnamed, the only one answers; several are refused, the refusal naming them so the assistant can ask. None kept:
the person is asked to connect one, unless ``ask`` is False (a status asks nobody).
"""

from __future__ import annotations

from typing import Any

from .agent import ResourceDenied


def named(row: dict[str, Any]) -> str:
    return str((row.get("fields") or {}).get("account") or "")


async def account(call: Any, resource: str, wanted: Any = None, ask: bool = True,
                  service: str = "") -> tuple[dict[str, Any] | None, str]:
    """The secret's fields of the account wanted, or None and why."""
    service = service or "service"
    wanted = str(wanted if wanted is not None else call.inputs.get("account") or "").strip().lower()
    try:
        rows = await call.records.list(resource)
        row = match(rows, wanted) if wanted else None
        if row is None and wanted and len(rows) > 1:
            return None, f"No connected {service} account matches {wanted!r}. Connected: {names(rows)}."
        if row is None and len(rows) > 1:
            return None, f"Several {service} accounts are connected ({names(rows)}): say which with account."
        if not rows and not ask:
            return None, f"No {service} account is connected to this agent."
        # One account connected is the one, whatever it was called when a draft or a watch remembered it.
        secret = await call.records.secret(resource, (row or rows[0])["ref"] if rows else None)
    except ResourceDenied as refused:
        return None, f"No {service} account is connected to this agent: {refused}"
    return secret, ""


def match(rows: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    exact = [r for r in rows if named(r).lower() == wanted]
    if exact:
        return exact[0]
    partial = [r for r in rows if wanted in named(r).lower()]
    return partial[0] if len(partial) == 1 else None


def names(rows: list[dict[str, Any]]) -> str:
    return ", ".join(named(r) or r["ref"] for r in rows)


async def listed(call: Any, resource: str) -> list[str]:
    return [named(r) for r in await call.records.list(resource)]
