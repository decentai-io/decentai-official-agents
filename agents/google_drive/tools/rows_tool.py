"""A new-rows watch on a spreadsheet tab: how form responses, sign-ups and orders typed by someone else arrive.

A count of rows alone is fooled by a row deleted and another added between two checks: the count is the same, and the
new row is never handed on. So the watch keeps, besides the last row handed on (the cursor), a fingerprint of each of
the last few rows up to it. A check reads a window around the cursor and finds where those rows are now:

- where they still are, the rows after the cursor are new;
- moved up (rows deleted above), or down (rows put in above), the rows after them are new;
- the newest of them gone or changed, the one before it anchors, and what follows is handed on;
- none of them anywhere near: the tab was sorted or rewritten, and nothing can be called new without guessing, so the
  watch starts again from the rows there are now and says so (an event ``reset``).

The header row is the one the watch was given, read at every check, so a column added later gets its name. The tab
is remembered by its sheet id, so a renamed tab is still the one watched; a deleted tab or spreadsheet closes the watch.
"""

from __future__ import annotations

import hashlib
import json

from standard import ToolBase
from standard.watch import Gone

from .common import HttpError, connect, failed, no_account
from .sheets_model import a1, blank, clip, column_letter, find_tab, header_names, no_tab, tab_row, trimmed
from .sheets_tool import batch, spreadsheet
from .watching import Seen, run

KEPT = 5
SEARCH = 60


def print_of(row) -> str:
    return hashlib.sha256(json.dumps(trimmed(row), ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def prints(rows_by_number: dict[int, list], last: int, header_row: int) -> str:
    """The fingerprints of the last KEPT rows up to ``last``, oldest first, as "number:print,…"."""
    first = max(header_row + 1, last - KEPT + 1)
    return ",".join(f"{n}:{print_of(rows_by_number.get(n, []))}" for n in range(first, last + 1))


def anchor(rows_by_number: dict[int, list], kept: str, low: int, high: int) -> int | None:
    """Where the newest row still found of those kept stands now; None when none is found."""
    remembered = [(int(n), p) for n, p in (pair.split(":", 1) for pair in kept.split(",") if ":" in pair)]
    for position, (number, wanted) in reversed(list(enumerate(remembered))):
        before = remembered[position - 1][1] if position else None
        hits = [n for n in range(low, high + 1) if print_of(rows_by_number.get(n, [])) == wanted
                and not blank(rows_by_number.get(n, []))]
        # Where several rows read the same, the one whose row before also matches, nearest where it was.
        hits.sort(key=lambda n: (before is not None and print_of(rows_by_number.get(n - 1, [])) != before,
                                 abs(n - number), -n))
        if hits:
            return hits[0]
    return None


async def present(sheets, spreadsheet_id: str, tab: dict) -> tuple[int, dict[int, list]]:
    """The last row holding anything, and the rows near it: the whole tab read once."""
    right = column_letter(max(1, tab["grid_columns"]))
    values = (await batch(sheets, spreadsheet_id, [a1(tab["tab"], f"A1:{right}{max(1, tab['grid_rows'])}")]))[0]
    last = len(values)
    return last, {n + 1: values[n] for n in range(max(0, last - KEPT), last)}


class RowsTool(ToolBase):
    id = "rows"

    async def watch(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        header_row = int(inputs.get("header_row", 1))
        try:
            meta = await spreadsheet(clients.sheets, spreadsheet_id)
            tab = find_tab(meta, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            last, near = await present(clients.sheets, spreadsheet_id, tab)
        except HttpError as error:
            return failed(error)
        cursor = max(last, header_row)
        label = f"{(meta.get('properties') or {}).get('title') or ''} / {tab['tab']}"
        watch_ref = await call.records.create("watch", {
            "kind": "rows", "status": "active", "account": clients.address, "target_id": spreadsheet_id,
            "label": label, "sheet_id": tab["sheet_id"], "header_row": header_row, "cursor": str(cursor),
            "fingerprint": prints(near, cursor, header_row), "note": str(inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "label": label, "rows_so_far": max(0, last - header_row),
                "cursor": cursor}, "success"

    async def new(self, call):
        return await run(call, "rows", self._read), "success"

    @staticmethod
    async def _read(clients, watch, most):
        fields = watch["fields"]
        spreadsheet_id = str(fields.get("target_id") or "")
        header_row = int(float(fields.get("header_row") or 0))
        cursor = int(fields.get("cursor") or 0)
        kept = str(fields.get("fingerprint") or "")
        meta = await spreadsheet(clients.sheets, spreadsheet_id)
        tab = next((tab_row(s) for s in meta.get("sheets") or []
                    if tab_row(s)["sheet_id"] == int(float(fields.get("sheet_id") or 0))), None)
        if tab is None:
            raise Gone("The tab it watched is no longer in the spreadsheet.")
        right = column_letter(max(1, tab["grid_columns"]))
        low = max(header_row + 1, cursor - SEARCH - KEPT)
        high = min(tab["grid_rows"], cursor + SEARCH + most + 1)
        ranges = ([a1(tab["tab"], f"A{header_row}:{right}{header_row}")] if header_row >= 1 else [])
        ranges.append(a1(tab["tab"], f"A{low}:{right}{max(low, high)}"))
        answers = await batch(clients.sheets, spreadsheet_id, ranges)
        header = answers[0][0] if header_row >= 1 and answers[0] else []
        window = answers[-1] if low <= high else []
        rows_by_number = {low + i: row for i, row in enumerate(window)}
        base = {"watch_ref": watch["ref"], "label": str(fields.get("label") or ""), "tab": tab["tab"]}
        if not kept:
            at = header_row if cursor <= header_row else None
        else:
            at = anchor(rows_by_number, kept, low, high)
        if at is None:
            last, near = await present(clients.sheets, spreadsheet_id, tab)
            cursor = max(last, header_row)
            return Seen([{**base, "event": "reset", "row": cursor,
                          "message": f"The last rows handed on are no longer where they were, or were changed (the "
                                     f"tab was sorted, cleared or rewritten), so nothing was handed on. The watch "
                                     f"goes on after row {cursor}, the last row there is now."}],
                        {"cursor": str(cursor), "fingerprint": prints(near, cursor, header_row)})
        names = header_names(header, max(len(header), max((len(r) for r in window), default=0)))
        found, last_handed, numbers = [], at, [n for n in range(at + 1, low + len(window))]
        for number in numbers:
            if len(found) >= most:
                break
            cells = rows_by_number.get(number, [])
            if blank(cells):
                continue
            found.append({**base, "event": "row", "row": number,
                          "cells": {names[i]: clip(c) for i, c in enumerate(cells) if str(c).strip()}})
            last_handed = number
        # More waits when rows past those handed on were left for later, or the window was full and the tab goes on.
        more = any(not blank(rows_by_number.get(n, [])) for n in numbers if n > last_handed) or (
            len(window) == high - low + 1 and high < tab["grid_rows"])
        new_cursor = last_handed
        cursor_fields = {"cursor": str(new_cursor), "fingerprint": prints(rows_by_number, new_cursor, header_row)}
        return Seen(found, cursor_fields, more=more)
