"""A live Google spreadsheet: its tabs, a window of rows, values found and totalled in code, rows written on the
person's go-ahead, and new spreadsheets and tabs.

- **A read is a window**, never the whole tab: from a row, so many rows. The header is the row the caller names
  (``header_row``, 1 by default, 0 for none), read on every call; a continued read never takes a data row for it.
- **Writes are RAW** unless the person asks for formulas: a value written is the value stored, so text from an email
  that starts with = + - or @ stays text and never becomes a live formula. A number is written as a number when it
  is given as one.
- **Totals are counted here**, in exact decimals, from the unformatted values (so a sheet shown in a decimal-comma
  locale still adds up right).
"""

from __future__ import annotations

import json
from decimal import Decimal

from standard import ToolBase

from .common import ONE, SPREADSHEET, HttpError, connect, failed, find_folder, no_account, refused, segment
from .sheets_model import (COLUMNS, META, ONE_CELL, a1, blank, cell_value, clip, column_letter, column_number,
                           decimal, find_tab, header_names, no_tab, plain, tab_row)

READ_MOST = 200
CELLS_SHOWN = 2000
RECORD_CHARS = 7000
WINDOW = 2000
SCAN_ROWS = 100_000
GROUPS_MOST = 100
RENDER = {"formatted": "FORMATTED_VALUE", "raw": "UNFORMATTED_VALUE", "formulas": "FORMULA"}


def values_path(spreadsheet_id: str, cells: str) -> str:
    return f"spreadsheets/{segment(spreadsheet_id)}/values/{segment(cells)}"


def keepable(rows):
    """What was written, as a record can hold it: cells clipped, rows dropped from the end until it fits."""
    kept = [[clip(c) for c in row] for row in rows]
    cut = False
    while kept and len(json.dumps(kept, ensure_ascii=False)) > RECORD_CHARS:
        kept.pop()
        cut = True
    return {"rows": kept, "clipped": cut}


async def spreadsheet(sheets, spreadsheet_id: str) -> dict:
    return await sheets.get(f"spreadsheets/{segment(spreadsheet_id)}", params={"fields": META})


async def batch(sheets, spreadsheet_id: str, ranges: list[str], render: str = "FORMATTED_VALUE") -> list[list]:
    answer = await sheets.get(f"spreadsheets/{segment(spreadsheet_id)}/values:batchGet",
                              params={"ranges": ranges, "majorDimension": "ROWS", "valueRenderOption": render})
    return [r.get("values") or [] for r in answer.get("valueRanges") or []]


def columns_of(text: str, tab: dict) -> tuple[int, int] | None:
    """(first, last) column numbers of "B:F", held to the tab's grid; the whole grid when not given."""
    if not text:
        return 1, max(1, tab["grid_columns"])
    match = COLUMNS.match(text.replace("$", "").strip())
    if not match:
        return None
    first, last = sorted((column_number(match.group(1)), column_number(match.group(2))))
    return first, min(last, max(1, tab["grid_columns"]))


class SheetsTool(ToolBase):
    id = "sheets"

    async def _tab(self, clients, spreadsheet_id, name):
        meta = await spreadsheet(clients.sheets, spreadsheet_id)
        return meta, find_tab(meta, name)

    async def get(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        try:
            meta = await spreadsheet(clients.sheets, str(call.inputs["spreadsheet_id"]))
        except HttpError as error:
            return failed(error)
        tabs = [tab_row(s) for s in meta.get("sheets") or []]
        properties = meta.get("properties") or {}
        return {"spreadsheet_id": str(meta.get("spreadsheetId") or ""), "title": str(properties.get("title") or ""),
                "link": str(meta.get("spreadsheetUrl") or ""), "locale": str(properties.get("locale") or ""),
                "time_zone": str(properties.get("timeZone") or ""), "tab_count": len(tabs),
                "tabs": tabs[:100]}, "success"

    async def read(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        header_row = int(inputs.get("header_row", 1))
        start = int(inputs.get("from_row") or header_row + 1)
        most = int(inputs.get("max_rows") or 50)
        try:
            meta, tab = await self._tab(clients, spreadsheet_id, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            span = columns_of(str(inputs.get("columns") or ""), tab)
            if span is None:
                return refused(f"columns is a span like 'A:F', not {inputs['columns']!r}.")
            left, right = column_letter(span[0]), column_letter(span[1])
            result = {"tab": tab["tab"], "header_row": header_row, "grid_rows": tab["grid_rows"], "first_row": start,
                      "header": [], "rows": []}
            if start > tab["grid_rows"]:
                return {**result, "note": f"The tab has {tab['grid_rows']} rows; there is nothing from row {start}."}, \
                    "success"
            end = min(tab["grid_rows"], start + most - 1)
            ranges = ([a1(tab["tab"], f"{left}{header_row}:{right}{header_row}")] if header_row >= 1 else []) + \
                [a1(tab["tab"], f"{left}{start}:{right}{end}")]
            answers = await batch(clients.sheets, spreadsheet_id, ranges,
                                  RENDER[str(inputs.get("values") or "formatted")])
        except HttpError as error:
            return failed(error)
        header = [clip(c) for c in (answers[0][0] if header_row >= 1 and answers[0] else [])]
        window = answers[-1]
        rows, cells, clipped = [], 0, 0
        for row in window:
            if rows and cells + len(row) > CELLS_SHOWN:
                break
            clipped += sum(1 for c in row if isinstance(c, str) and len(c) > 500)
            rows.append([clip(c) for c in row])
            cells += len(row)
        result.update(header=header, rows=rows, shown_rows=len(rows), last_row=start + len(rows) - 1)
        if clipped:
            result["clipped_cells"] = clipped
        if len(rows) < len(window):
            result["next_row"] = start + len(rows)
        elif end < tab["grid_rows"] and len(window) == end - start + 1:
            result["next_row"] = end + 1
        return result, "success"

    # -- writes ----------------------------------------------------------------------------------------------------
    @staticmethod
    def _rows(inputs):
        return [[cell_value(c) for c in row] for row in inputs["rows"]]

    @staticmethod
    def _option(inputs):
        return "USER_ENTERED" if inputs.get("formulas") is True else "RAW"

    async def append(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        rows = self._rows(inputs)
        try:
            meta, tab = await self._tab(clients, spreadsheet_id, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            # INSERT_ROWS, not Google's default OVERWRITE: an append never lands on cells further down.
            answer = await clients.sheets.post(
                values_path(spreadsheet_id, a1(tab["tab"])) + ":append",
                params={"valueInputOption": self._option(inputs), "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": rows})
        except HttpError as error:
            return failed(error)
        updates = answer.get("updates") or {}
        updated_range = str(updates.get("updatedRange") or "")
        write_ref = await call.records.create("write", {
            "spreadsheet_id": spreadsheet_id, "tab": tab["tab"], "kind": "append", "range": updated_range,
            "rows": int(updates.get("updatedRows") or len(rows)), "account": clients.address,
            "spreadsheet": str((meta.get("properties") or {}).get("title") or ""), "written": keepable(rows)})
        return {"write_ref": write_ref, "updated_range": updated_range,
                "updated_rows": int(updates.get("updatedRows") or len(rows)),
                "as": "formulas" if self._option(inputs) == "USER_ENTERED" else "values"}, "success"

    async def update(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        start = ONE_CELL.match(str(inputs["start_cell"]).replace("$", "").strip())
        if not start:
            return refused("start_cell is one cell like 'B2'.")
        rows = self._rows(inputs)
        left, top = column_number(start.group(1)), int(start.group(2))
        right, bottom = left + max(len(r) for r in rows) - 1, top + len(rows) - 1
        area = f"{column_letter(left)}{top}:{column_letter(right)}{bottom}"
        try:
            meta, tab = await self._tab(clients, spreadsheet_id, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            if bottom > tab["grid_rows"] or right > tab["grid_columns"]:
                return refused(f"{area} runs past the tab '{tab['tab']}', which has {tab['grid_rows']} rows and "
                               f"{tab['grid_columns']} columns. To add rows at the end, use sheets_append.")
            target = a1(tab["tab"], area)
            # What the cells held, formulas as formulas, read before anything is written: a record of an overwrite is
            # honest only with this.
            before = (await batch(clients.sheets, spreadsheet_id, [target], "FORMULA"))[0]
            answer = await clients.sheets.put(values_path(spreadsheet_id, target),
                                              params={"valueInputOption": self._option(inputs)},
                                              json={"range": target, "majorDimension": "ROWS", "values": rows})
        except HttpError as error:
            return failed(error)
        width = right - left + 1
        previous = [(list(row) + [""] * width)[:width] for row in before]
        previous += [[""] * width for _ in range(len(rows) - len(previous))]
        write_ref = await call.records.create("write", {
            "spreadsheet_id": spreadsheet_id, "tab": tab["tab"], "kind": "update", "account": clients.address,
            "range": str(answer.get("updatedRange") or target), "rows": len(rows),
            "spreadsheet": str((meta.get("properties") or {}).get("title") or ""), "written": keepable(rows),
            "previous": keepable(previous)})
        return {"write_ref": write_ref, "updated_range": str(answer.get("updatedRange") or target),
                "updated_cells": int(answer.get("updatedCells") or 0), "previous": previous[:50]}, "success"

    async def create(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        title = str(inputs["title"]).strip()
        names = [str(t).strip() for t in inputs.get("tabs") or [] if str(t).strip()] or ["Sheet1"]
        if len({n.lower() for n in names}) < len(names):
            return refused("Two tabs may not have the same name.")
        header = [cell_value(c) for c in inputs.get("header") or []]
        try:
            body = {"name": title, "mimeType": SPREADSHEET}
            if inputs.get("folder_id") or inputs.get("folder_path"):
                folder = await find_folder(clients.drive, inputs.get("folder_id"), inputs.get("folder_path"))
                body["parents"] = [folder["id"]]
            made = await clients.drive.post("files", params={**ONE, "fields": "id,name,webViewLink"}, json=body)
            spreadsheet_id = str(made["id"])
            meta = await spreadsheet(clients.sheets, spreadsheet_id)
            first = tab_row((meta.get("sheets") or [{}])[0])
            requests = [{"updateSheetProperties": {"properties": {"sheetId": first["sheet_id"], "title": names[0]},
                                                   "fields": "title"}}]
            requests += [{"addSheet": {"properties": {"title": n}}} for n in names[1:]]
            await clients.sheets.post(f"spreadsheets/{segment(spreadsheet_id)}:batchUpdate",
                                      json={"requests": requests})
            if header:
                await clients.sheets.post(f"spreadsheets/{segment(spreadsheet_id)}/values:batchUpdate", json={
                    "valueInputOption": "RAW",
                    "data": [{"range": a1(n, "A1"), "majorDimension": "ROWS", "values": [header]} for n in names]})
        except HttpError as error:
            return failed(error)
        return {"spreadsheet_id": spreadsheet_id, "title": title, "tabs": names,
                "link": str(made.get("webViewLink") or "")}, "success"

    async def add_tab(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        spreadsheet_id, name = str(call.inputs["spreadsheet_id"]), str(call.inputs["title"]).strip()
        header = [cell_value(c) for c in call.inputs.get("header") or []]
        try:
            meta, tab = await self._tab(clients, spreadsheet_id, name)
            if tab is not None and tab["tab"].lower() == name.lower():
                return refused(f"The spreadsheet already has a tab named '{tab['tab']}'.", "conflict")
            answer = await clients.sheets.post(f"spreadsheets/{segment(spreadsheet_id)}:batchUpdate",
                                               json={"requests": [{"addSheet": {"properties": {"title": name}}}]})
            if header:
                await clients.sheets.put(values_path(spreadsheet_id, a1(name, "A1")), params={"valueInputOption": "RAW"},
                                         json={"range": a1(name, "A1"), "majorDimension": "ROWS", "values": [header]})
        except HttpError as error:
            return failed(error)
        added = tab_row({"properties": ((answer.get("replies") or [{}])[0].get("addSheet") or {}).get("properties")
                         or {}})
        return {"tab": added["tab"] or name, "sheet_id": added["sheet_id"]}, "success"

    # -- found and counted in code ---------------------------------------------------------------------------------
    async def _windows(self, clients, spreadsheet_id, tab, first_row, last_row, render):
        """(row number, cells) for each row from first to last, a window at a time."""
        right = column_letter(max(1, tab["grid_columns"]))
        start = max(1, first_row)
        last_row = min(last_row, tab["grid_rows"])
        while start <= last_row:
            end = min(last_row, start + WINDOW - 1)
            got = (await batch(clients.sheets, spreadsheet_id, [a1(tab["tab"], f"A{start}:{right}{end}")], render))[0]
            for offset, row in enumerate(got):
                yield start + offset, row
            if len(got) < end - start + 1:
                return
            start = end + 1

    async def find(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        needle = str(inputs["text"]).strip().casefold()
        exact = str(inputs.get("match") or "contains") == "exact"
        most = int(inputs.get("max_results") or 20)
        matches, scanned, cut_short = [], 0, False
        try:
            meta = await spreadsheet(clients.sheets, spreadsheet_id)
            if inputs.get("tab"):
                tab = find_tab(meta, str(inputs["tab"]))
                if tab is None:
                    return no_tab(meta, str(inputs["tab"]))
                tabs = [tab]
            else:
                tabs = [tab_row(s) for s in meta.get("sheets") or []]
            for tab in tabs:
                async for number, row in self._windows(clients, spreadsheet_id, tab, 1, tab["grid_rows"],
                                                       "FORMATTED_VALUE"):
                    scanned += 1
                    if scanned > SCAN_ROWS:
                        cut_short = True
                        break
                    for index, value in enumerate(row):
                        text = str(value).strip().casefold()
                        if text and (text == needle if exact else needle in text):
                            matches.append({"tab": tab["tab"], "cell": f"{column_letter(index + 1)}{number}",
                                            "row": number, "value": clip(str(value))})
                    if len(matches) >= most:
                        break
                if len(matches) >= most or cut_short:
                    break
        except HttpError as error:
            return failed(error)
        more = len(matches) > most
        return {"matches": matches[:most], "scanned_rows": min(scanned, SCAN_ROWS), "cut_short": cut_short,
                **({"more": True} if more else {})}, "success"

    async def totals(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        header_row = int(inputs.get("header_row", 1))
        try:
            meta, tab = await self._tab(clients, spreadsheet_id, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            names = []
            if header_row >= 1:
                right = column_letter(max(1, tab["grid_columns"]))
                got = (await batch(clients.sheets, spreadsheet_id,
                                   [a1(tab["tab"], f"A{header_row}:{right}{header_row}")]))[0]
                names = header_names(got[0] if got else [], tab["grid_columns"])
            wanted = self._columns(inputs["columns"], names, tab)
            if isinstance(wanted, str):
                return refused(wanted)
            group = None
            if inputs.get("group_by"):
                group = self._columns([inputs["group_by"]], names, tab)
                if isinstance(group, str):
                    return refused(group)
                group = group[0]
            first = int(inputs.get("from_row") or header_row + 1)
            last = int(inputs.get("to_row") or tab["grid_rows"])
            sums = {c: {"sum": Decimal(0), "count": 0, "min": None, "max": None, "not_numbers": 0}
                    for c, _ in wanted}
            groups: dict[str, dict[int, list]] = {}
            rows, cut_short = 0, False
            async for _, row in self._windows(clients, spreadsheet_id, tab, first, last, "UNFORMATTED_VALUE"):
                if blank(row):
                    continue
                rows += 1
                if rows > SCAN_ROWS:
                    cut_short = True
                    break
                key = str(row[group[0] - 1]).strip() if group and group[0] - 1 < len(row) else ""
                for column, _ in wanted:
                    value = row[column - 1] if column - 1 < len(row) else ""
                    number = decimal(value)
                    if number is None:
                        if str(value).strip():
                            sums[column]["not_numbers"] += 1
                        continue
                    held = sums[column]
                    held["sum"] += number
                    held["count"] += 1
                    held["min"] = number if held["min"] is None else min(held["min"], number)
                    held["max"] = number if held["max"] is None else max(held["max"], number)
                    if group:
                        if key not in groups and len(groups) >= GROUPS_MOST:
                            continue
                        slot = groups.setdefault(key, {}).setdefault(column, [Decimal(0), 0])
                        slot[0] += number
                        slot[1] += 1
        except HttpError as error:
            return failed(error)
        columns = []
        for column, name in wanted:
            held = sums[column]
            row = {"column": column_letter(column), "name": name, "count": held["count"], "sum": plain(held["sum"]),
                   "not_numbers": held["not_numbers"]}
            if held["count"]:
                row.update(min=plain(held["min"]), max=plain(held["max"]),
                           average=plain((held["sum"] / held["count"]).quantize(Decimal("0.000001"))))
            columns.append(row)
        result = {"tab": tab["tab"], "rows": min(rows, SCAN_ROWS), "from_row": first, "columns": columns,
                  "cut_short": cut_short}
        if group:
            result["groups"] = [{"group": key, "column": column_letter(c), "sum": plain(v[0]), "count": v[1]}
                                for key, by in sorted(groups.items()) for c, v in sorted(by.items())]
            result["groups_cut"] = len(groups) >= GROUPS_MOST
        return result, "success"

    @staticmethod
    def _columns(given, names, tab):
        """[(column number, name)] from letters or header names; or the words saying which wasn't found."""
        found = []
        for item in given:
            text = str(item).strip()
            if text in names:
                number = names.index(text) + 1
            elif text.lower() in [n.lower() for n in names]:
                number = [n.lower() for n in names].index(text.lower()) + 1
            elif text.isascii() and text.isalpha() and len(text) <= 3:
                number = column_number(text)
            else:
                return (f"No column is named {text!r}" + (f"; the header has {', '.join(names[:30])}." if names
                                                           else "; name a column by its letter."))
            if number > max(1, tab["grid_columns"]):
                return f"Column {text} is past the tab's {tab['grid_columns']} columns."
            found.append((number, names[number - 1] if number <= len(names) else column_letter(number)))
        return found
