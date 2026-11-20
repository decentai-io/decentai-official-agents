"""Microsoft Forms responses, read from the Excel workbook a form syncs them to (work and school accounts).

``forms_find`` looks inside the drive's workbooks for a response table. It opens a few workbooks at once, each
held to its own time, and the whole look to a budget well inside the function's timeout: what it did not reach is
counted and said, never waited for.
"""

from __future__ import annotations

import asyncio
import time

from standard import ToolBase
from standard.http import HttpError

from .common import client, failed, ids, refused, work_only
from .drive import item_row
from .responses import questions, response_table, responses
from .workbook import Workbook

WHAT = "Reading Microsoft Forms responses (from the Excel workbook a form syncs to)"
SEARCH_WINDOW = 50
AT_ONCE = 4
#: The whole look, and one workbook's, in seconds: forms_find's timeout is 60.
BUDGET = 35.0
ONE_MOST = 15.0
MAX_TABLES = 6
MAX_QUESTIONS = 50


class FormsTool(ToolBase):
    id = "forms"

    async def find(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        started = time.monotonic()
        most = int(call.inputs.get("max_results") or 8)
        try:
            refusal = await work_only(drive, WHAT)
            if refusal:
                return refusal
            answer = await drive.search(str(call.inputs.get("query") or "").strip() or "xlsx", SEARCH_WINDOW)
        except HttpError as error:
            return failed(error)
        found = [r for r in (item_row(i) for i in answer.get("value") or [])
                 if r["kind"] == "file" and r["name"].lower().endswith((".xlsx", ".xlsm"))]
        found.sort(key=lambda r: r["modified"], reverse=True)
        chosen, gate = found[:most], asyncio.Semaphore(AT_ONCE)

        async def look(row):
            async with gate:
                left = BUDGET - (time.monotonic() - started)
                if left <= 1:
                    return {**row, "checked": False}
                book = Workbook(drive, row["item_id"], row.get("drive_id", ""))
                try:
                    hit = await asyncio.wait_for(response_table(book, most_tables=MAX_TABLES), min(ONE_MOST, left))
                except asyncio.TimeoutError:
                    return {**row, "checked": False}
                except HttpError as error:
                    if error.kind in ("auth", "scope"):
                        raise
                    # One workbook Excel cannot open (locked, damaged) is no reason to hide the others.
                    return {**row, "checked": True, "is_response_workbook": False, "problem": error.message}
                if hit is None:
                    return {**row, "checked": True, "is_response_workbook": False}
                region, layout = hit
                return {**row, "checked": True, "is_response_workbook": True, "table": region.name,
                        "questions": questions(region.headers, layout)[:MAX_QUESTIONS], "responses": region.rows}

        try:
            rows = await asyncio.gather(*(look(r) for r in chosen))
        except HttpError as error:
            return failed(error)
        rows = sorted(rows, key=lambda r: (not r.get("is_response_workbook"), not r["checked"]))
        unchecked = sum(not r["checked"] for r in rows)
        result = {"workbooks": rows, "more": len(found) > most, "not_checked": unchecked}
        if unchecked:
            result["note"] = (f"{unchecked} workbook(s) were not looked inside within the time allowed; ask again with "
                              f"a narrower query (the form's or workbook's name).")
        return result, "success"

    async def responses(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call, "workbook_id")
        most = int(call.inputs.get("max_results") or 10)
        token = str(call.inputs.get("page_token") or "").strip()
        if token and not token.isdigit():
            return refused("page_token is the next_page_token of an earlier answer; pass it on unchanged.")
        try:
            refusal = await work_only(drive, WHAT)
            if refusal:
                return refusal
            book = Workbook(drive, item_id, drive_id)
            hit = await response_table(book, str(call.inputs.get("table") or ""))
            if hit is None:
                return refused("This workbook has no Forms response table (its first columns the response id, start "
                               "and completion times, email and name). Only a form whose responses sync to an "
                               "Excel workbook can be read here.", "not_found")
            region, layout = hit
            stop = min(int(token), region.rows) if token else region.rows
            start = max(0, stop - most)
            found = await responses(book, region, layout, start, stop)
        except HttpError as error:
            return failed(error)
        result = {"table": region.name, "total": region.rows, "questions": questions(region.headers, layout),
                  "responses": sorted(found, key=lambda r: r["response_id"], reverse=True)}
        if start > 0:
            result["next_page_token"] = str(start)
        return result, "success"
