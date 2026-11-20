"""Watches: a schedulable function that looks at several things a person asked to hear about, and hands on what is
new in each, once.

- **Each watch is read on its own.** One that fails says so in ``problems`` and keeps its cursor, so what it would
  have handed on comes next time; the others are handed on now.
- **A watch whose target is gone closes itself** (a deleted thread, folder or page), and says so in ``closed``, so
  it never fails every run after.
- **Cursors are written once every watch has been read,** together with the rows they hand on: never a cursor moved
  past rows that were not returned.
- **A time budget:** a watch not reached within it is left for the next run, and ``more`` says so.

A watch is a data record of the agent's with a ``status`` keys field (``active`` or ``closed``). ``read`` is the
agent's own: given one watch, it returns ``Seen(rows, cursor)``, where ``cursor`` is the fields to write on the
watch once the run ends (None for none), or raises.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .agent import ResourceDenied
from .errors import HttpError


@dataclass
class Seen:
    rows: list[dict[str, Any]] = field(default_factory=list)
    cursor: dict[str, Any] | None = None
    #: There is more past these rows; the next run goes on from the cursor.
    more: bool = False
    #: Close the watch after this run, with these words: its target is gone, or it is done.
    close: str = ""


class Gone(Exception):
    """The watch's target no longer exists: the watch is closed, with these words."""


def active(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if str((r.get("fields") or {}).get("status") or "active") == "active"]


async def check(call: Any, resource: str, watches: list[dict[str, Any]],
                read: Callable[[dict[str, Any]], Awaitable[Seen]], budget: float = 120.0,
                name: Callable[[dict[str, Any]], str] | None = None) -> dict[str, Any]:
    """Every watch read, its rows handed on, its cursor written: ``{rows, checked, problems, closed, more}``."""
    started, rows, problems, closed, writes, more, checked = time.monotonic(), [], [], [], [], False, 0
    named = name or (lambda w: str((w.get("fields") or {}).get("label") or w.get("ref")))
    for watch in watches:
        if time.monotonic() - started > budget:
            more = True
            break
        checked += 1
        try:
            seen = await read(watch)
        except (Gone, HttpError) as failed:
            gone = isinstance(failed, Gone) or failed.kind == "not_found"
            if gone:
                closed.append({"watch_ref": watch["ref"], "watch": named(watch),
                               "why": str(failed) if isinstance(failed, Gone) else "Its target no longer exists."})
                writes.append((watch["ref"], {"status": "closed"}))
            else:
                problems.append({"watch_ref": watch["ref"], "watch": named(watch), "kind": failed.kind,
                                 "error": failed.message})
            continue
        except ResourceDenied as failed:
            problems.append({"watch_ref": watch["ref"], "watch": named(watch), "kind": failed.code or "refused",
                             "error": str(failed)})
            continue
        rows.extend(seen.rows)
        more = more or seen.more
        cursor = dict(seen.cursor or {})
        if seen.close:
            closed.append({"watch_ref": watch["ref"], "watch": named(watch), "why": seen.close})
            cursor["status"] = "closed"
        if cursor:
            writes.append((watch["ref"], cursor))
    for ref, fields in writes:
        try:
            await call.records.update(resource, ref, fields)
        except ResourceDenied as failed:
            # Its rows go out now and may come again next run: twice, never lost.
            problems.append({"watch_ref": ref, "watch": ref, "kind": failed.code or "refused",
                             "error": f"Its place could not be kept, so these may be handed on again: {failed}"})
    return {"rows": rows, "checked": checked, "problems": problems, "closed": closed, "more": more}


async def listing(call: Any, resource: str, describe: Callable[[dict[str, Any]], dict[str, Any]],
                  include_closed: bool = False) -> list[dict[str, Any]]:
    """The person's watches of this kind, each as ``describe`` shows it, with its ref and status."""
    records = await call.records.list(resource)
    return [{"watch_ref": r["ref"], "status": str(r["fields"].get("status") or "active"), **describe(r)}
            for r in records if include_closed or str(r["fields"].get("status") or "active") == "active"]


async def close(call: Any, resource: str, ref: str) -> dict[str, Any]:
    """A watch stopped for good: a schedule that checks it hears nothing more from it."""
    record = await call.records.read(resource, ref)
    if str(record["fields"].get("status") or "active") == "closed":
        return {"watch_ref": ref, "closed": True, "already": True}
    await call.records.update(resource, ref, {"status": "closed"})
    return {"watch_ref": ref, "closed": True, "already": False}
