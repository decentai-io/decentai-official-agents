"""Excel workbooks through Graph's workbook API, which Microsoft offers for work and school accounts only.

A workbook is a drive item: everything inside it lives under ``{item}/workbook``, in the person's drive or in the
drive of whoever shared it. Worksheets and tables are addressed by name, escaped; ranges by A1 address.

Cells are read as their text (what a person sees: a date as a date, an amount as formatted) and, where code needs
to decide something (a response's id, its times), as their values too.

Writes go through one persistent session per call (``createSession``; every write names it in
``workbook-session-id``), so several ranges written by one call land together. A text that starts like a formula
("=", or "+", "-", "@" before a letter) is written as text, with Excel's apostrophe, unless the person asked for a
formula. The workbook API throttles often: 429, 503 and 504 are waited out by the library on reads; a write that
got no answer is reported as unknown, never sent again.
"""

from __future__ import annotations

import re
from typing import Any

from standard.http import HttpError, segment

from .drive import Drive, where

#: The widest block read. A sheet can be 16,384 columns wide; a result the model reads cannot.
MAX_COLUMNS = 50
#: The longest cell text handed on; the rest is cut with an ellipsis.
MAX_CELL_CHARS = 300

CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")
FORMULA_LIKE = re.compile(r"^(=|[+\-@][A-Za-z(=$])")


def column_number(letters: str) -> int:
    number = 0
    for ch in letters.upper():
        number = number * 26 + (ord(ch) - 64)
    return number


def column_letters(number: int) -> str:
    letters = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def split_address(address: Any) -> tuple[str, str]:
    """ "'Q3 Orders'!A1:D9" as ("Q3 Orders", "A1:D9"); no sheet is ""."""
    text = str(address or "").strip()
    if "!" not in text:
        return "", text
    sheet, _, local = text.rpartition("!")
    if len(sheet) >= 2 and sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, local


def parse_box(local: Any) -> tuple[int, int, int, int] | None:
    """ "B2:D9" as (first row, first column, last row, last column), or None for anything that is not one cell or
    one block of cells (whole columns included: those are a million rows)."""
    parts = [p.strip() for p in str(local or "").split(":")]
    if len(parts) not in (1, 2):
        return None
    cells = [CELL.match(p) for p in parts]
    if not all(cells):
        return None
    points = [(int(m.group(2)), column_number(m.group(1))) for m in cells]
    if len(points) == 1:
        points.append(points[0])
    (ra, ca), (rb, cb) = points
    if min(ra, rb) < 1 or max(ca, cb) > 16384:
        return None
    return min(ra, rb), min(ca, cb), max(ra, rb), max(ca, cb)


def box_address(r1: int, c1: int, r2: int, c2: int) -> str:
    return f"{column_letters(c1)}{r1}:{column_letters(c2)}{r2}"


def clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS - 1] + "…"
    return value


def blank(line: list[Any]) -> bool:
    return all(str(cell if cell is not None else "").strip() == "" for cell in line)


def header_names(cells: list[Any]) -> list[str]:
    """A header row as names a row can be keyed by: a blank header is "Column 3", a repeated one "Amount (2)"."""
    names: list[str] = []
    for i, cell in enumerate(cells, start=1):
        name = str(cell if cell is not None else "").strip() or f"Column {i}"
        candidate, n = name, 2
        while candidate in names:
            candidate = f"{name} ({n})"
            n += 1
        names.append(candidate)
    return names


def keyed(headers: list[str], line: list[Any]) -> dict[str, Any]:
    return {h: clip(line[i] if i < len(line) else "") for i, h in enumerate(headers)}


def as_written(value: Any, formulas: bool) -> Any:
    """A value as it is sent: a text that Excel would take for a formula is kept as text, unless formulas are
    wanted."""
    if isinstance(value, str) and not formulas and FORMULA_LIKE.match(value):
        return "'" + value
    return value


class Region:
    """Rows under a header row: a table, or a worksheet's used range read as one. Data rows are counted from 0
    below the header."""

    def __init__(self, kind: str, name: str, worksheet: str, header_row: int, first_col: int, last_col: int,
                 last_row: int, headers: list[str], columns_cut: int = 0):
        self.kind, self.name, self.worksheet = kind, name, worksheet
        self.header_row, self.first_col, self.last_col, self.last_row = header_row, first_col, last_col, last_row
        self.headers, self.columns_cut = headers, columns_cut

    @property
    def rows(self) -> int:
        return max(0, self.last_row - self.header_row)

    def row_number(self, index: int) -> int:
        """The worksheet row a data row sits on, as Excel numbers it."""
        return self.header_row + 1 + index

    def address(self, start: int, stop: int) -> str:
        return box_address(self.row_number(start), self.first_col, self.row_number(stop - 1), self.last_col)


class Workbook:
    """One workbook, in the person's drive or a shared one, with the session its writes ride on."""

    def __init__(self, drive: Drive, item_id: str, drive_id: str = ""):
        self.drive, self.item_id, self.drive_id = drive, item_id, drive_id
        self.base = f"{where(item_id, drive_id)}/workbook"
        self.session = ""

    def headers(self) -> dict[str, str]:
        return {"workbook-session-id": self.session} if self.session else {}

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self.drive.get(f"{self.base}/{path}", params=params or None, headers=self.headers())

    def sheet(self, name: str) -> str:
        return f"worksheets/{segment(name)}"

    def table(self, name: str) -> str:
        return f"tables/{segment(name)}"

    # -- reading -----------------------------------------------------------------------------------------------
    async def worksheets(self) -> list[dict[str, Any]]:
        return (await self.get("worksheets", **{"$select": "id,name,position,visibility"})).get("value") or []

    async def tables(self) -> list[dict[str, Any]]:
        return (await self.get("tables", **{"$select": "id,name,showHeaders,showTotals"})).get("value") or []

    async def used_range(self, sheet: str) -> dict[str, Any]:
        """The block holding values; valuesOnly leaves out cells that are only formatted."""
        return await self.get(f"{self.sheet(sheet)}/usedRange(valuesOnly=true)",
                              **{"$select": "address,rowCount,columnCount"})

    async def block(self, sheet: str, address: str, select: str = "text") -> dict[str, Any]:
        return await self.get(f"{self.sheet(sheet)}/range(address='{segment(address)}')", **{"$select": select})

    async def cells(self, sheet: str, address: str, field: str = "text") -> list[list[Any]]:
        return (await self.block(sheet, address, field)).get(field) or []

    async def table_region(self, table: str) -> Region:
        found = await self.get(self.table(table), **{"$select": "id,name,showHeaders,showTotals"})
        box = await self.get(f"{self.table(table)}/range", **{"$select": "address"})
        sheet, local = split_address(box.get("address"))
        parsed = parse_box(local)
        if parsed is None:
            raise HttpError("http", f"Microsoft gave table {table} an address this agent cannot read: "
                                    f"{box.get('address')!r}.")
        r1, c1, r2, c2 = parsed
        shown_c2 = min(c2, c1 + MAX_COLUMNS - 1)
        if found.get("showHeaders") is not False:
            header_cells, header_row = (await self.cells(sheet, box_address(r1, c1, r1, shown_c2)) or [[]])[0], r1
        else:
            header_cells, header_row = [""] * (shown_c2 - c1 + 1), r1 - 1
        region = Region("table", str(found.get("name") or table), sheet, header_row, c1, shown_c2,
                        r2 - 1 if found.get("showTotals") else r2, header_names(header_cells), c2 - shown_c2)
        # An Excel table always keeps one body row, even with no data: a blank one is not a row of data.
        if region.rows == 1:
            first = await self.read_rows(region, 0, 1)
            if not first or blank(first[0]):
                region.last_row = region.header_row
        return region

    async def sheet_region(self, sheet: str) -> Region:
        """A worksheet's used range read as a table: its first row the header, every row under it data."""
        used = await self.used_range(sheet)
        parsed = parse_box(split_address(used.get("address"))[1])
        if parsed is None:
            raise HttpError("http", f"Microsoft gave worksheet {sheet} a used range this agent cannot read: "
                                    f"{used.get('address')!r}.")
        r1, c1, r2, c2 = parsed
        shown_c2 = min(c2, c1 + MAX_COLUMNS - 1)
        header_cells = (await self.cells(sheet, box_address(r1, c1, r1, shown_c2)) or [[]])[0]
        if r1 == r2 and blank(header_cells):
            return Region("worksheet", sheet, sheet, r1, c1, shown_c2, r1, [])
        return Region("worksheet", sheet, sheet, r1, c1, shown_c2, r2, header_names(header_cells), c2 - shown_c2)

    async def read_rows(self, region: Region, start: int, stop: int, field: str = "text") -> list[list[Any]]:
        stop = min(stop, region.rows)
        if stop <= start:
            return []
        return await self.cells(region.worksheet, region.address(start, stop), field)

    async def read_both(self, region: Region, start: int, stop: int) -> tuple[list[list[Any]], list[list[Any]]]:
        """Data rows [start, stop) as (values, text), in one request."""
        stop = min(stop, region.rows)
        if stop <= start:
            return [], []
        found = await self.block(region.worksheet, region.address(start, stop), "values,text")
        return found.get("values") or [], found.get("text") or []

    # -- writing -----------------------------------------------------------------------------------------------
    async def open_session(self) -> None:
        """A persistent session for this call's writes. One that cannot be made is not a reason to refuse: without
        one, each write is saved on its own."""
        try:
            answer = await self.drive.post(f"{self.base}/createSession", json={"persistChanges": True})
            self.session = str(answer.get("id") or "")
        except HttpError as error:
            if error.kind in ("auth", "scope", "not_found", "denied", "locked", "invalid"):
                raise
            self.session = ""

    async def close_session(self) -> None:
        """Best effort: an unclosed session expires on Microsoft's side within minutes, its writes already saved."""
        if not self.session:
            return
        try:
            await self.drive.post(f"{self.base}/closeSession", json={}, headers=self.headers())
        except HttpError:
            pass
        self.session = ""

    async def set_range(self, sheet: str, address: str, values: list[list[Any]]) -> dict[str, Any]:
        return await self.drive.patch(f"{self.base}/{self.sheet(sheet)}/range(address='{segment(address)}')",
                                      json={"values": values}, headers=self.headers())

    async def add_rows(self, table: str, values: list[list[Any]]) -> dict[str, Any]:
        return await self.drive.post(f"{self.base}/{self.table(table)}/rows/add",
                                     json={"index": None, "values": values}, headers=self.headers())


def is_workbook(item: dict[str, Any]) -> bool:
    return "file" in item and str(item.get("name") or "").lower().endswith((".xlsx", ".xlsm"))
