"""What the four watches share: one ``watch`` record each, on ``standard.watch``.

A watch remembers its kind (changes, comments, rows, responses), its account, its target and its cursor, all in keys
(``record.list`` hands on keys only, and none of them is content). A check reads each watch on its own: one that
fails keeps its cursor and is named in ``problems``; one whose target is gone closes itself; cursors are written only
once every watch was read.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from standard.watch import Seen, active, check

from .common import Clients, HttpError, connect

#: How long a check may read before it leaves the rest for the next run; well inside its timeout.
BUDGET = 100.0


class Accounts:
    """The clients for each account the watches of one run name, each connected once."""

    def __init__(self, call: Any):
        self.call = call
        self.made: dict[str, Clients] = {}

    async def __call__(self, watch: dict[str, Any]) -> Clients:
        address = str((watch.get("fields") or {}).get("account") or "")
        if address not in self.made:
            clients, why = await connect(self.call, ask=False, wanted=address)
            if clients is None:
                raise HttpError("auth", why)
            self.made[address] = clients
        return self.made[address]

    def close(self) -> None:
        for clients in self.made.values():
            clients.close()


async def run(call: Any, kind: str, read: Callable[[Clients, dict[str, Any], int], Awaitable[Seen]]) -> dict:
    """Every active watch of this kind (or the one named), read and handed on."""
    watches = active(await call.records.list("watch", {"kind": kind}))
    wanted = str(call.inputs.get("watch_ref") or "")
    if wanted:
        watches = [w for w in watches if w["ref"] == wanted]
    most = int(call.inputs.get("max_results") or 50)
    accounts = Accounts(call)

    async def one(watch: dict[str, Any]) -> Seen:
        return await read(await accounts(watch), watch, most)

    try:
        return await check(call, "watch", watches, one, budget=BUDGET)
    finally:
        accounts.close()


def describe(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    shown = {"kind": str(fields.get("kind") or ""), "label": str(fields.get("label") or ""),
             "account": str(fields.get("account") or ""), "target_id": str(fields.get("target_id") or "")}
    return {k: v for k, v in shown.items() if v}


__all__ = ["Accounts", "BUDGET", "Seen", "describe", "run"]
