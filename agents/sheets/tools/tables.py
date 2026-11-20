"""A spreadsheet as tables of cells that remember what they were.

A cell keeps its original value (a number, a date, a string) or its formula with the value the file last computed,
and its text as a person sees it. Identifiers with leading zeros stay text; dates stay dates.

Reading is honest about the file's shape:

- **Every row or none.** A sheet past ``MAX_ROWS`` rows of data, or a file past ``MAX_CELLS`` cells, is refused
  with its count, never cut.
- **The header is found,** not assumed to be row 1: title rows, a report date and blank rows above it are skipped
  (or the person names ``header_row``). Row numbers are always the sheet's own.
- **Merged cells** carry their value over the whole merged range, so a merged region label isn't read as blanks.
- **A CSV's encoding is found:** UTF-8 with or without its mark, UTF-16, and the Windows code pages Excel saves in
  (cp1256 when the text is Arabic, cp1252 otherwise). Its delimiter is found too: comma, semicolon, tab or bar.
- Parsing runs in a thread, off the agent's loop; a file larger than a chat file may be is refused before reading.
"""

from __future__ import annotations

import asyncio
import codecs
import csv
import io
import math
import re
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from standard.http import FILE_MOST, human_size

from . import numbers

MAX_ROWS = 100_000
MAX_CELLS = 1_500_000
HEADER_LOOK = 30
_LEADING_ZERO = re.compile(r"^0\d+$")


class Refused(Exception):
    """A file or a request this agent won't read on, in words with the numbers."""

    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind

    def answer(self):
        return {"error": str(self), "kind": self.kind}, "error"


class Cell:
    __slots__ = ("value", "formula", "cached", "where")

    def __init__(self, value: Any, formula: str = "", cached: Any = None, where: str = ""):
        self.value = value          # what the file holds (number/date/str/None)
        self.formula = formula      # "=SUM(A2:A9)" when the cell is a formula
        self.cached = cached        # the formula's last computed value, if the file carries one
        self.where = where          # "Sales!G6": where a formula was

    @property
    def shown(self) -> Any:
        return self.cached if self.formula else self.value

    @property
    def text(self) -> str:
        return shown_text(self.shown)

    @property
    def blank(self) -> bool:
        return not self.formula and (self.value is None or str(self.value).strip() == "")


def shown_text(shown: Any) -> str:
    if shown is None:
        return ""
    if isinstance(shown, datetime):
        return shown.date().isoformat() if shown.time() == time.min else shown.isoformat(sep=" ")
    if isinstance(shown, (date, time)):
        return shown.isoformat()
    if isinstance(shown, bool):
        return "TRUE" if shown else "FALSE"
    if isinstance(shown, float) and shown.is_integer():
        return str(int(shown))
    if isinstance(shown, Decimal):
        return numbers.text(shown)
    return str(shown)


class Sheet:
    def __init__(self, name: str, columns: List[str], rows: List[List[Cell]], header_row: int = 1,
                 merged: int = 0, found_header: bool = False):
        self.name = name
        self.columns = columns
        self.rows = rows                # index 0 = the sheet row under the header
        self.header_row = header_row    # the header's own row number in the sheet
        self.merged = merged
        self.found_header = found_header
        self._detected: Dict[int, numbers.Column] = {}

    def column(self, name: str) -> int:
        wanted = " ".join(str(name).split()).casefold()
        for index, column in enumerate(self.columns):
            if " ".join(column.split()).casefold() == wanted:
                return index
        raise Refused(f"No column '{name}' in sheet '{self.name}' (columns: {', '.join(self.columns)})")

    def cell(self, row: List[Cell], name_or_index) -> Cell:
        index = name_or_index if isinstance(name_or_index, int) else self.column(name_or_index)
        return row[index] if index < len(row) else Cell(None)

    def row_number(self, index: int) -> int:
        return self.header_row + 1 + index

    def detected(self, index: int) -> numbers.Column:
        if index not in self._detected:
            self._detected[index] = numbers.detect(
                c.shown for c in (self.cell(r, index) for r in self.rows) if isinstance(c.shown, str))
        return self._detected[index]

    def numbers(self, name: str, decimal: Optional[str] = None):
        """How to read the column's cells as numbers; Refused when its decimal mark can't be told."""
        index = self.column(name)
        try:
            read = numbers.reader(self.columns[index], self.detected(index), decimal)
        except numbers.Unsettled as unsettled:
            raise Refused(f"{unsettled} (sheet '{self.name}')")

        def of(row: List[Cell]) -> Optional[Decimal]:
            return read(self.cell(row, index).shown)
        of.mark = read.mark
        return of

    def profile(self) -> List[Dict[str, Any]]:
        out = []
        for index, name in enumerate(self.columns):
            kinds, blanks, formulas, zeros = set(), 0, 0, False
            for row in self.rows:
                cell = self.cell(row, index)
                formulas += bool(cell.formula)
                if cell.blank:
                    blanks += 1
                    continue
                shown = cell.shown
                if shown is None:
                    continue            # a formula never computed says nothing
                if isinstance(shown, (datetime, date)):
                    kinds.add("date")
                elif isinstance(shown, bool):
                    kinds.add("text")
                elif isinstance(shown, (int, float)):
                    kinds.add("number")
                else:
                    text = str(shown).strip()
                    if _LEADING_ZERO.match(text.translate(numbers.DIGITS)):
                        zeros = True
                        kinds.add("text")
                    elif numbers.shape(text) is not None:
                        kinds.add("number")
                    else:
                        kinds.add("text")
            entry = {"name": name, "type": "empty" if not kinds else (kinds.pop() if len(kinds) == 1 else "mixed"),
                     "blanks": blanks, "formulas": formulas, "leading_zeros": zeros}
            found = self.detected(index)
            if found.state in (".", ",", "ambiguous", "mixed"):
                entry["decimal"] = found.state
            if found.examples and found.state == "ambiguous":
                entry["ambiguous_examples"] = found.examples
            if found.currencies:
                entry["currencies"] = found.currencies
            out.append(entry)
        return out


class Workbook:
    def __init__(self, file_ref: str, filename: str, kind: str, sheets: List[Sheet], problem: str = "",
                 encoding: str = "", delimiter: str = ""):
        self.file_ref = file_ref
        self.filename = filename
        self.kind = kind
        self.sheets = sheets
        self.problem = problem
        self.encoding = encoding
        self.delimiter = delimiter

    def sheet(self, name: str = "") -> Sheet:
        if not self.sheets:
            raise Refused(self.problem or "The file has no sheets.")
        if not name:
            return self.sheets[0]
        wanted = " ".join(name.split()).casefold()
        for sheet in self.sheets:
            if " ".join(sheet.name.split()).casefold() == wanted:
                return sheet
        raise Refused(f"No sheet '{name}' (sheets: {', '.join(s.name for s in self.sheets)})")


def normalize(text: str, how: str) -> str:
    text = str(text or "")
    if how == "none":
        return text
    text = text.translate(numbers.DIGITS)
    if how == "digits":
        return re.sub(r"\D", "", text)
    text = " ".join(text.split())
    return text.casefold() if how == "lowercase" else text


# -- reading a file ------------------------------------------------------------------------------------------------

async def raw_of(call, resource_id: str, file_ref: str) -> Tuple[str, bytes]:
    """A file handed to the call, or one of the resource's by its ref: its name and bytes; refused past a chat file's
    size before a byte is read."""
    path = await call.file(file_ref, resource_id)
    size = path.stat().st_size
    if size > FILE_MOST:
        raise Refused(f"{path.name} is {human_size(size)}; a file here is at most {human_size(FILE_MOST)}. "
                      f"Split it, or keep only the sheets and columns needed.", "too_large")
    return path.name, await asyncio.to_thread(path.read_bytes)


async def load(call, resource_id: str, file_ref: str, sheet: str = "", header_row: Optional[int] = None) -> Workbook:
    """A file read into sheets, off the loop. ``header_row`` (the sheet's own row number) applies to ``sheet``, or to
    the first sheet; every other sheet's header is found."""
    filename, raw = await raw_of(call, resource_id, file_ref)
    return await asyncio.to_thread(parse, file_ref, filename, raw, sheet, header_row)


def parse(file_ref: str, filename: str, raw: bytes, sheet: str = "", header_row: Optional[int] = None) -> Workbook:
    name = filename.lower()
    if raw[:2] == b"PK" or name.endswith(".xlsx"):
        return _xlsx(file_ref, filename, raw, sheet, header_row)
    if name.endswith((".csv", ".tsv", ".txt")) or text_like(raw):
        return _csv(file_ref, filename, raw, header_row)
    return Workbook(file_ref, filename, "unsupported", [], "Not a CSV or an Excel .xlsx file.")


def text_like(raw: bytes) -> bool:
    head = raw[:4096]
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF8)):
        return True
    return b"\x00" not in head or _utf16_guess(head) is not None


def _utf16_guess(head: bytes) -> Optional[str]:
    """UTF-16 without its mark: ASCII text has a zero in every other byte."""
    if len(head) < 4:
        return None
    even, odd = head[0::2].count(0), head[1::2].count(0)
    half = len(head) // 2
    if odd > half * 0.3 and even < half * 0.05:
        return "utf-16-le"
    if even > half * 0.3 and odd < half * 0.05:
        return "utf-16-be"
    return None


#: Bytes Windows-1252 leaves undefined; a file holding one isn't cp1252.
_NOT_1252 = {0x81, 0x8D, 0x8F, 0x90, 0x9D}


def decode(raw: bytes) -> Tuple[str, str]:
    """The text, and the encoding it was read in."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", "replace"), "utf-16"
    guessed = _utf16_guess(raw[:4096])
    if guessed:
        return raw.decode(guessed, "replace"), guessed
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    return raw.decode(single_byte(raw), "replace"), single_byte(raw)


def single_byte(raw: bytes) -> str:
    """cp1256 (Arabic Windows) or cp1252 (Western Windows). Arabic words are runs of high bytes with no Latin letter
    beside them; a Western accent is a single high byte inside a Latin word ("Café", "Müller")."""
    if any(b in _NOT_1252 for b in raw):
        return "cp1256"
    arabic = latin = 0
    for run in re.finditer(rb"[A-Za-z]*[\x80-\xff]+[A-Za-z]*", raw):
        word = run.group()
        high = sum(1 for b in word if b >= 0x80)
        if high == len(word) and high >= 2:
            arabic += 1
        elif high < len(word):
            latin += 1
    return "cp1256" if arabic > latin else "cp1252"


def sniff(text: str) -> str:
    """The delimiter that splits most of the first lines into the same number of fields, more than one: titles above
    the header and a decimal comma inside the numbers ("12.450,50" in a semicolon file) don't fool it, and quotes are
    respected."""
    from collections import Counter

    lines = [line for line in text[:65536].splitlines()[:50] if line.strip()]
    best, best_score = ",", (0, 0)
    for delimiter in (",", ";", "\t", "|"):
        try:
            widths = [len(row) for row in csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)]
        except csv.Error:
            continue
        common = Counter(w for w in widths if w > 1).most_common(1)
        if not common:
            continue
        width, lines_of_it = common[0]
        if (lines_of_it, width) > best_score:
            best, best_score = delimiter, (lines_of_it, width)
    return best


def _csv(file_ref: str, filename: str, raw: bytes, header_row: Optional[int]) -> Workbook:
    text, encoding = decode(raw)
    delimiter = sniff(text)
    grid: List[List[Cell]] = []
    rows = cells = 0
    try:
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
            rows += 1
            cells += len(row)
            if cells <= MAX_CELLS and rows <= MAX_ROWS + HEADER_LOOK:
                grid.append([Cell(value if value.strip() != "" else None) for value in row])
    except csv.Error as failed:
        raise Refused(f"{filename}: the CSV breaks at record {rows + 1}: {failed}")
    _limits(filename, "the file", rows, cells)
    while grid and all(c.blank for c in grid[-1]):
        grid.pop()
    sheet = _sheet("Sheet1", grid, header_row, {}, "")
    return Workbook(file_ref, filename, "csv", [sheet], encoding=encoding, delimiter=delimiter)


def _limits(filename: str, what: str, rows: int, cells: int) -> None:
    if cells > MAX_CELLS:
        raise Refused(f"{filename}: {what} has {cells:,} cells; at most {MAX_CELLS:,} are read. Split it, or keep "
                      f"only the columns needed. Nothing was read.", "too_large")
    if rows > MAX_ROWS + HEADER_LOOK:
        raise Refused(f"{filename}: {what} has {rows:,} rows; at most {MAX_ROWS:,} rows of data are read. Split it "
                      f"(by month, by branch) and give the parts. Nothing was cut or read.", "too_large")


def _trimmed(values) -> list:
    values = list(values)
    while values and (values[-1] is None or values[-1] == ""):
        values.pop()
    return values


def _xlsx(file_ref: str, filename: str, raw: bytes, wanted: str, header_row: Optional[int]) -> Workbook:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    try:
        formulas = load_workbook(io.BytesIO(raw), data_only=False, read_only=True)
        values = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as exc:
        return Workbook(file_ref, filename, "xlsx", [], f"The workbook could not be opened: {exc}")
    merges = merged_ranges(raw)
    sheets, cells, first = [], 0, None
    try:
        for ws_f in formulas.worksheets:
            name = ws_f.title
            ws_v = values[name]
            ws_f.reset_dimensions()          # a writer's stated size can be wrong: read every row there is
            ws_v.reset_dimensions()
            grid, rows, last = [], 0, 0
            for cells_f, row_v in zip(ws_f.iter_rows(), ws_v.iter_rows(values_only=True)):
                rows += 1
                # A formula is told by its cell's type, never by its text: a text cell that starts with "=" (an
                # address pasted from an email, say) is text, and stays text.
                kinds = [getattr(c, "data_type", "n") for c in cells_f]
                row_f, row_v = _trimmed(c.value for c in cells_f), _trimmed(row_v)
                if row_f or row_v:
                    last = rows
                cells += len(row_f)
                if cells > MAX_CELLS:
                    break
                if rows > MAX_ROWS + HEADER_LOOK + 1:
                    continue
                made = []
                for column, value_f in enumerate(row_f):
                    value_v = row_v[column] if column < len(row_v) else None
                    if kinds[column] == "f" and value_f is not None:
                        formula = value_f if isinstance(value_f, str) else str(getattr(value_f, "text", value_f))
                        made.append(Cell(None, formula, value_v, f"{name}!{get_column_letter(column + 1)}{rows}"))
                    else:
                        made.append(Cell(value_f))
                grid.append(made)
            _limits(filename, f"sheet '{name}'", last, cells)
            del grid[last:]
            here = header_row if (first is None and not wanted) or (wanted and _same(name, wanted)) else None
            first = first or name
            sheets.append(_sheet(name, grid, here, merges, name))
    finally:
        formulas.close()
        values.close()
    return Workbook(file_ref, filename, "xlsx", sheets)


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _sheet(name: str, grid: List[List[Cell]], header_row: Optional[int], merges: Dict[str, list],
           title: str) -> Sheet:
    ranges = merges.get(title, [])
    for min_col, min_row, max_col, max_row in ranges:
        if min_row > len(grid):
            continue
        top = grid[min_row - 1]
        source = top[min_col - 1] if min_col - 1 < len(top) else Cell(None)
        for r in range(min_row, min(max_row, len(grid)) + 1):
            row = grid[r - 1]
            if len(row) < max_col:
                row.extend(Cell(None) for _ in range(max_col - len(row)))
            for c in range(min_col, max_col + 1):
                if (r, c) != (min_row, min_col):
                    row[c - 1] = Cell(source.shown)
    found = header_row is None
    if header_row is None:
        index = detect_header(grid)
    else:
        if header_row > max(len(grid), 1):
            raise Refused(f"Sheet '{name}' has {len(grid)} rows; there is no row {header_row} to be its header.")
        index = header_row - 1
    head = grid[index] if index < len(grid) else []
    width = max([len(head)] + [len(r) for r in grid[index + 1:]] or [0])
    columns, taken = [], set()
    for position in range(width):
        text = " ".join(head[position].text.split()) if position < len(head) else ""
        text = text or f"Column{position + 1}"
        base, n = text, 2
        while text.casefold() in taken:
            text, n = f"{base} ({n})", n + 1
        taken.add(text.casefold())
        columns.append(text)
    rows = grid[index + 1:]
    if len(rows) > MAX_ROWS:
        raise Refused(f"Sheet '{name}' has {len(rows):,} rows of data under its header; at most {MAX_ROWS:,} are "
                      f"read. Split it and give the parts. Nothing was cut or read.", "too_large")
    return Sheet(name, columns, rows, index + 1, len(ranges), found and index > 0)


def detect_header(grid: List[List[Cell]]) -> int:
    """The first row that looks like a header: at least two different values, filling most of the table's width.
    A title (one value, or one merged across), a "Report date: …" line or a blank row is not it."""
    widths = [sum(1 for c in row if not c.blank) for row in grid[:50]]
    widest = max(widths or [0])
    for index, row in enumerate(grid[:HEADER_LOOK]):
        filled = [c.text.strip() for c in row if not c.blank]
        if len(set(filled)) >= 2 and len(filled) >= max(2, math.ceil(widest * 0.6)):
            return index
    return next((i for i, w in enumerate(widths) if w), 0)


_MERGE = re.compile(rb'<(?:\w+:)?mergeCell\b[^>]*?\bref="([A-Z]{1,3}[0-9]+(?::[A-Z]{1,3}[0-9]+)?)"')


def merged_ranges(raw: bytes) -> Dict[str, List[Tuple[int, int, int, int]]]:
    """Each worksheet's merged ranges, by title, read from its XML a piece at a time (read-only reading doesn't
    give them)."""
    import xml.etree.ElementTree as ET

    from openpyxl.utils.cell import range_boundaries

    found: Dict[str, list] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            book = ET.fromstring(package.read("xl/workbook.xml"))
            links = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
            targets = {link.get("Id"): link.get("Target") for link in links}
            for sheet in book.iter():
                if not sheet.tag.endswith("}sheet"):
                    continue
                rid = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), None)
                target = targets.get(rid) or ""
                path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
                if path not in package.namelist():
                    continue
                ranges, tail = [], b""
                with package.open(path) as stream:
                    while chunk := stream.read(1 << 20):
                        text = tail + chunk
                        for match in _MERGE.finditer(text):
                            ref = match.group(1).decode()
                            ranges.append(range_boundaries(ref if ":" in ref else f"{ref}:{ref}"))
                        tail = text[-300:]
                found[sheet.get("name")] = sorted(set(ranges))
    except (KeyError, zipfile.BadZipFile, ET.ParseError, ValueError):
        return {}
    return found


def row_dict(sheet: Sheet, row: List[Cell], columns: Optional[List[str]] = None,
             formulas: bool = False) -> Dict[str, str]:
    """The row keyed by column name, narrowed to ``columns`` when given (trimmed, without regard to case)."""
    wanted = {" ".join(str(c).split()).casefold() for c in columns} if columns else set()
    out = {}
    for index, name in enumerate(sheet.columns):
        if wanted and " ".join(name.split()).casefold() not in wanted:
            continue
        cell = sheet.cell(row, index)
        out[name] = cell.formula if (formulas and cell.formula) else cell.text
    return out


def key_of(sheet: Sheet, row: List[Cell], columns: List[str], how: str) -> Tuple[str, ...]:
    return tuple(normalize(sheet.cell(row, c).text, how) for c in columns)


def blank_key(key: Tuple[str, ...]) -> bool:
    """A key with any part blank matches nothing: two empty emails are not one person."""
    return any(not part.strip() for part in key)
