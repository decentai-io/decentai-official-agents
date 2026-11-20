"""Excel workbooks: find one, see what is inside, read a block or a table, and write on the person's go-ahead.

Microsoft's workbook API exists for work and school accounts only, so a personal account is told so at once. A
workbook someone shared is reached in their drive (its drive_id). Every write is recorded with what it wrote and,
for a range, what the cells held before (as formulas, which is what would put them back).
"""

from __future__ import annotations

from standard import ToolBase
from standard.http import HttpError

from .common import client, failed, ids, refused, work_only
from .drive import item_row
from .workbook import (MAX_COLUMNS, Workbook, as_written, box_address, clip, header_names, keyed, parse_box,
                       split_address)

#: Search matches names and contents alike, so more are asked for than are shown.
SEARCH_WINDOW = 50
MAX_LISTED = 25
WHAT = "Reading and writing Excel workbooks"


async def opened(call):
    """(the workbook, None) or (None, the refusal), for a work or school account only."""
    drive, refusal = await client(call)
    if drive is None:
        return None, refusal
    try:
        refusal = await work_only(drive, WHAT)
    except HttpError as error:
        return None, failed(error)
    if refusal:
        return None, refusal
    item_id, drive_id = ids(call, "workbook_id")
    return Workbook(drive, item_id, drive_id), None


def box_of(address):
    box = parse_box(split_address(address)[1])
    if box is None:
        return None, refused(f"'{address}' is not a block of cells; give an A1 address with row numbers, like A1:D40 "
                             f"or B7.")
    return box, None


class ExcelTool(ToolBase):
    id = "excel"

    async def find(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        try:
            refusal = await work_only(drive, WHAT)
            if refusal:
                return refusal
            answer = await drive.search(str(call.inputs.get("query") or "").strip() or "xlsx", SEARCH_WINDOW)
        except HttpError as error:
            return failed(error)
        most = int(call.inputs.get("max_results") or 10)
        rows = [r for r in (item_row(i) for i in answer.get("value") or [])
                if r["kind"] == "file" and r["name"].lower().endswith((".xlsx", ".xlsm"))]
        rows.sort(key=lambda r: r["modified"], reverse=True)
        return {"workbooks": rows[:most], "more": len(rows) > most}, "success"

    async def get(self, call):
        book, refusal = await opened(call)
        if book is None:
            return refusal
        try:
            item = await book.drive.item(book.item_id, book.drive_id)
            if not str(item.get("name") or "").lower().endswith((".xlsx", ".xlsm")):
                return refused(f"'{item.get('name')}' is not an .xlsx workbook; only .xlsx files open here.")
            sheets = []
            for sheet in (await book.worksheets())[:MAX_LISTED]:
                name = str(sheet.get("name") or "")
                used = await book.used_range(name)
                sheets.append({"name": name, "used_range": split_address(used.get("address"))[1],
                               "rows": int(used.get("rowCount") or 0), "columns": int(used.get("columnCount") or 0),
                               "visible": str(sheet.get("visibility") or "Visible") == "Visible"})
            tables = []
            for table in (await book.tables())[:MAX_LISTED]:
                region = await book.table_region(str(table.get("name") or ""))
                tables.append({"name": region.name, "worksheet": region.worksheet, "headers": region.headers,
                               "rows": region.rows})
        except HttpError as error:
            return failed(error)
        return {"workbook": item_row(item), "worksheets": sheets, "tables": tables}, "success"

    async def read(self, call):
        book, refusal = await opened(call)
        if book is None:
            return refusal
        sheet = str(call.inputs["worksheet"])
        most = int(call.inputs.get("max_rows") or 20)
        with_header = bool(call.inputs.get("header"))
        try:
            if str(call.inputs.get("address") or "").strip():
                box, problem = box_of(call.inputs["address"])
                if problem:
                    return problem
            else:
                box = parse_box(split_address((await book.used_range(sheet)).get("address"))[1])
                if box is None:
                    return refused(f"Worksheet {sheet} has no block of cells to read.")
            r1, c1, r2, c2 = box
            shown_c2 = min(c2, c1 + MAX_COLUMNS - 1)
            header, first = [], r1
            if with_header:
                header = header_names((await book.cells(sheet, box_address(r1, c1, r1, shown_c2)) or [[]])[0])
                first = r1 + 1
            total = max(0, r2 - first + 1)
            shown = min(total, most)
            rows = await book.cells(sheet, box_address(first, c1, first + shown - 1, shown_c2)) if shown else []
        except HttpError as error:
            return failed(error)
        result = {"worksheet": sheet, "address": box_address(r1, c1, r2, c2),
                  "rows": [[clip(cell) for cell in line] for line in rows], "row_count": total,
                  "rows_cut": total - shown, "columns_cut": c2 - shown_c2}
        if with_header:
            result["header"] = header
        return result, "success"

    async def write(self, call):
        book, refusal = await opened(call)
        if book is None:
            return refusal
        formulas = bool(call.inputs.get("formulas"))
        planned = []
        for given in call.inputs["writes"]:
            box, problem = box_of(given["address"])
            if problem:
                return problem
            r1, c1, r2, c2 = box
            address, values = box_address(r1, c1, r2, c2), given["values"]
            height, width = r2 - r1 + 1, c2 - c1 + 1
            if len(values) != height or any(len(line) != width for line in values):
                return refused(f"{given['worksheet']}!{address} is {height} row(s) by {width} column(s); its values "
                               f"must be exactly that shape. Nothing was written.")
            planned.append((str(given["worksheet"]), address, [[as_written(v, formulas) for v in line]
                                                               for line in values]))
        try:
            item = await book.drive.item(book.item_id, book.drive_id)
            previous = [await book.cells(sheet, address, "formulas") for sheet, address, _ in planned]
            await book.open_session()
        except HttpError as error:
            return failed(error, f"'{book.item_id}'")
        done, problem = [], None
        try:
            for sheet, address, values in planned:
                try:
                    await book.set_range(sheet, address, values)
                    done.append((sheet, address, values))
                except HttpError as error:
                    problem = (sheet, address, values, error)
                    break
        finally:
            await book.close_session()
        keys = {"kind": "update_range", "workbook_id": book.item_id,
                **({"drive_id": book.drive_id} if book.drive_id else {}), "workbook": str(item.get("name") or ""),
                "target": ", ".join(f"{s}!{a}" for s, a, _ in planned)[:500], "rows": sum(len(v) for *_, v in planned),
                **({"account": book.drive.account} if book.drive.account else {})}
        kept = {"written": {"ranges": [{"worksheet": s, "address": a, "rows": v} for s, a, v in planned]},
                "previous": {"ranges": [{"worksheet": s, "address": a, "rows": p}
                                        for (s, a, _), p in zip(planned, previous)]}}
        if problem is not None:
            sheet, address, _, error = problem
            status = "unknown" if error.kind == "unknown" else "partial" if done else ""
            if not status:
                return failed(error, f"'{item.get('name')}'")
            ref = await call.records.create("write", {**keys, "status": status, **kept})
            return refused(f"{len(done)} of {len(planned)} ranges were written; {sheet}!{address} "
                           f"{'may or may not have been' if status == 'unknown' else 'was not'} written: "
                           f"{error.message} Read it before writing again.", error.kind, write_ref=ref)
        ref = await call.records.create("write", {**keys, "status": "written", **kept})
        return {"write_ref": ref, "written": [{"worksheet": s, "address": a, "cells": len(v) * len(v[0])}
                                              for s, a, v in done],
                "previous": [{"worksheet": s, "address": a, "rows": [[clip(c) for c in line] for line in p]}
                             for (s, a, _), p in zip(planned, previous)]}, "success"

    async def rows(self, call):
        book, refusal = await opened(call)
        if book is None:
            return refusal
        most = int(call.inputs.get("max_results") or 20)
        token = str(call.inputs.get("page_token") or "0").strip()
        if not token.isdigit():
            return refused("page_token is the next_page_token of an earlier answer; pass it on unchanged.")
        start = int(token)
        try:
            region = await book.table_region(str(call.inputs["table"]))
            lines = await book.read_rows(region, start, start + most)
        except HttpError as error:
            return failed(error)
        result = {"table": region.name, "worksheet": region.worksheet, "headers": region.headers,
                  "total": region.rows, "rows": [keyed(region.headers, line) for line in lines]}
        if start + most < region.rows:
            result["next_page_token"] = str(start + most)
        return result, "success"

    async def add_rows(self, call):
        book, refusal = await opened(call)
        if book is None:
            return refusal
        formulas = bool(call.inputs.get("formulas"))
        try:
            item = await book.drive.item(book.item_id, book.drive_id)
            region = await book.table_region(str(call.inputs["table"]))
        except HttpError as error:
            return failed(error)
        if region.columns_cut:
            return refused(f"Table {region.name} is wider than this agent reads; no rows were added.")
        # Each row is matched to the header by name, case aside; a column not given is left empty, and a name the
        # table does not have is refused rather than dropped, so nothing the person said is lost.
        position = {h.strip().lower(): i for i, h in enumerate(region.headers)}
        values, unknown = [], []
        for row in call.inputs["rows"]:
            line = [""] * len(region.headers)
            for name, value in row.items():
                i = position.get(str(name).strip().lower())
                if i is None:
                    unknown += [name] if name not in unknown else []
                    continue
                line[i] = as_written(value, formulas)
            values.append(line)
        if unknown:
            return refused(f"Table {region.name} has no column named {', '.join(repr(u) for u in unknown)}; its "
                           f"columns are {', '.join(region.headers)}. No rows were added.")
        keys = {"kind": "add_rows", "workbook_id": book.item_id,
                **({"drive_id": book.drive_id} if book.drive_id else {}), "workbook": str(item.get("name") or ""),
                "target": region.name, "rows": len(values),
                **({"account": book.drive.account} if book.drive.account else {})}
        written = {"written": {"headers": region.headers, "rows": values}}
        try:
            await book.open_session()
            await book.add_rows(region.name, values)
        except HttpError as error:
            if error.kind == "unknown":
                ref = await call.records.create("write", {**keys, "status": "unknown", **written})
                return refused(error.message + " Read the table before adding these rows again.", "unknown",
                               write_ref=ref)
            return failed(error, f"'{item.get('name')}'")
        finally:
            await book.close_session()
        ref = await call.records.create("write", {**keys, "status": "written", **written})
        return {"write_ref": ref, "table": region.name, "added": len(values),
                "total_rows": region.rows + len(values)}, "success"
