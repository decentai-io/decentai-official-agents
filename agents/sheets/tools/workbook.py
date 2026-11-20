"""Writing files someone can open, and reading each back before it is returned.

Cells are written as what they were: a date as a date, a number as a number, an identifier as text. **A formula is
written as the value the file computed**, with a note on the cell giving the formula and where it stood: a formula
moved to another row, sheet or file points at other cells than it did, so copying its text would put a wrong
answer in the new file without a word. A formula the file never computed (a file no spreadsheet program has saved)
is left empty, with the same note. Text that starts with "=" is written as text, never turned into a live formula.

A sheet whose header is Arabic is shown right to left. A CSV is written in UTF-8 with its byte-order mark, so Excel
opens Arabic in it as Arabic.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Tuple

from .tables import Cell

NOTES_MOST = 2000
_ARABIC = re.compile(r"[؀-ۿ]")


class Written:
    """What a build did to formulas."""

    def __init__(self):
        self.formulas = 0
        self.without_value = 0

    def log(self) -> List[Tuple[str, str, str]]:
        if not self.formulas:
            return []
        said = (f"{self.formulas} formula cell(s) written as the values the file computed, each with a note giving "
                f"its formula; a formula moved elsewhere would point at other cells")
        if self.without_value:
            said += f"; {self.without_value} had no computed value in the file and are left empty"
        return [("", "formulas", said)]


def _write_cell(ws, row: int, column: int, cell: Cell, written: Written) -> None:
    from openpyxl.comments import Comment

    if cell.formula:
        written.formulas += 1
        value = cell.cached
        if value is None:
            written.without_value += 1
        target = ws.cell(row=row, column=column, value=_safe(value))
        if isinstance(value, str) and value.startswith("="):
            target.data_type = "s"
        if written.formulas <= NOTES_MOST:
            note = f"Formula in the source{f' ({cell.where})' if cell.where else ''}: {cell.formula}"
            note += "" if value is not None else " (no computed value in the file)"
            target.comment = Comment(note[:1000], "Spreadsheets agent")
        return
    if cell.value is None:
        return
    target = ws.cell(row=row, column=column, value=_safe(cell.value))
    if isinstance(cell.value, str) and cell.value.startswith("="):
        target.data_type = "s"           # text that looks like a formula stays text


def _safe(value: Any) -> Any:
    """What openpyxl writes as itself: a type it doesn't know becomes its text."""
    from datetime import date, datetime, time
    from decimal import Decimal

    if value is None or isinstance(value, (str, int, float, Decimal, bool, date, datetime, time)):
        return value
    return str(value)


def _dress(ws, columns: List[str]) -> None:
    """A header someone can find, columns wide enough to read, and Arabic shown right to left."""
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    if _arabic(ws, columns):
        ws.sheet_view.rightToLeft = True
    for index, column in enumerate(ws.iter_cols(values_only=True, max_row=min(ws.max_row, 2000)), 1):
        longest = max((len(str(v)) for v in column if v is not None), default=0)
        ws.column_dimensions[get_column_letter(index)].width = min(max(longest + 2, 8), 60)


_LATIN = re.compile(r"[A-Za-z]")


def _arabic(ws, columns: List[str]) -> bool:
    """Whether the sheet reads right to left: more of its words (header and first rows) are Arabic than Latin."""
    texts = [str(c) for c in columns] + [str(v) for row in ws.iter_rows(min_row=2, max_row=min(ws.max_row, 51),
                                                                        values_only=True) for v in row
                                         if isinstance(v, str)]
    arabic = sum(1 for t in texts if _ARABIC.search(t))
    return arabic > sum(1 for t in texts if _LATIN.search(t) and not _ARABIC.search(t))


def build(sheets: List[Tuple[str, List[str], List[List[Cell]]]],
          change_log: List[Tuple[Any, str, str]]) -> Tuple[bytes, Written]:
    """sheets: (name, columns, rows of Cells). change_log: (row, action, detail), written as the last sheet."""
    from openpyxl import Workbook

    written = Written()
    wb = Workbook()
    wb.remove(wb.active)
    for name, columns, rows in sheets:
        ws = wb.create_sheet(title=name[:31])
        for column, header in enumerate(columns, 1):
            ws.cell(row=1, column=column, value=str(header))
        for r, row in enumerate(rows, 2):
            for column, cell in enumerate(row, 1):
                _write_cell(ws, r, column, cell if isinstance(cell, Cell) else Cell(cell), written)
        _dress(ws, columns)
    log = wb.create_sheet(title="Change Log")
    for column, header in enumerate(("Row", "Action", "Detail"), 1):
        log.cell(row=1, column=column, value=header)
    for r, (row, action, detail) in enumerate(list(change_log) + written.log(), 2):
        log.cell(row=r, column=1, value=row)
        log.cell(row=r, column=2, value=action)
        target = log.cell(row=r, column=3, value=detail)
        if isinstance(detail, str) and detail.startswith("="):
            target.data_type = "s"
    _dress(log, ["Row", "Action", "Detail"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue(), written


def verify(raw: bytes) -> Dict[str, Any]:
    """Open what was written and count what is there."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True)
    try:
        return {"sheets": wb.sheetnames, "rows": {name: wb[name].max_row for name in wb.sheetnames}}
    finally:
        wb.close()


def named(filename: str, suffix: str) -> str:
    """A file name of the person's, with its suffix; Arabic and other letters kept."""
    name = re.sub(r"[^\w.\- ]", "_", str(filename)).strip(" .") or "data"
    return name if name.lower().endswith(suffix) else name + suffix


def deliver(call, filename: str, raw: bytes) -> Tuple[str, Dict[str, Any]]:
    """The workbook, read back, then handed to the person as the call's file: its path, which the runtime makes the
    chat file's ref."""
    name = named(filename, ".xlsx")
    check = verify(raw)
    return call.give(name, raw), {"filename": name, **check}


def write_csv(columns: List[str], rows: List[List[str]], delimiter: str = ",") -> bytes:
    """UTF-8 with its byte-order mark: Excel then opens Arabic as Arabic."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def check_csv(raw: bytes, rows: int, delimiter: str = ",") -> None:
    """Read a CSV back: the same number of records, or it isn't handed over."""
    text = raw.decode("utf-8-sig")
    found = sum(1 for _ in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)) - 1
    if found != rows:
        raise ValueError(f"{found} rows came back of {rows} written")


def text_rows(columns: List[str], rows: List[List[Cell]], extra: Dict[str, List[Any]] = None):
    """Rows as Cells again, with extra columns appended (row numbers, reasons), for the sheets a job writes."""
    extra = extra or {}
    out = []
    for index, row in enumerate(rows):
        cells = list(row) + [Cell(None)] * (len(columns) - len(row))
        for name in extra:
            cells.append(Cell(extra[name][index]))
        out.append(cells)
    return list(columns) + list(extra), out
