"""A1 ranges, tabs and cells, as the Sheets API (v4) takes and gives them.

A tab is named inside a range by its title, quoted, a quote in it doubled, so "Sign-ups 2026" or "Dana's list" still
parses. A read beyond a tab's grid is refused by Google ("exceeds grid limits"), so every range built here is held
to the grid. Values come back trimmed: trailing empty rows and cells are left out, an empty row in the middle comes
back as an empty list.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

CELL_MOST = 500
COLUMNS = re.compile(r"^([A-Za-z]{1,3}):([A-Za-z]{1,3})$")
ONE_CELL = re.compile(r"^([A-Za-z]{1,3})([1-9]\d*)$")
META = "spreadsheetId,spreadsheetUrl,properties(title,locale,timeZone),sheets.properties(sheetId,title,index," \
       "gridProperties(rowCount,columnCount))"


def column_letter(number: int) -> str:
    """1 → A, 26 → Z, 27 → AA."""
    letters, number = "", max(1, int(number))
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + (ord(letter) - 64)
    return number


def a1(tab: str, cells: str = "") -> str:
    quoted = "'" + str(tab).replace("'", "''") + "'"
    return f"{quoted}!{cells}" if cells else quoted


def tab_row(sheet: dict[str, Any]) -> dict[str, Any]:
    properties = sheet.get("properties") or {}
    grid = properties.get("gridProperties") or {}
    return {"tab": str(properties.get("title") or ""), "sheet_id": int(properties.get("sheetId") or 0),
            "index": int(properties.get("index") or 0), "grid_rows": int(grid.get("rowCount") or 0),
            "grid_columns": int(grid.get("columnCount") or 0)}


def find_tab(meta: dict[str, Any], tab: str) -> dict[str, Any] | None:
    """The tab named: its exact title, then the same title in another case, then its sheet id; never a guess."""
    tabs = [tab_row(s) for s in meta.get("sheets") or []]
    tab = str(tab)
    return (next((t for t in tabs if t["tab"] == tab), None)
            or next((t for t in tabs if t["tab"].lower() == tab.lower()), None)
            or next((t for t in tabs if tab.isdigit() and t["sheet_id"] == int(tab)), None))


def no_tab(meta: dict[str, Any], tab: str):
    names = ", ".join(repr(tab_row(s)["tab"]) for s in (meta.get("sheets") or [])[:20])
    return {"error": f"The spreadsheet has no tab named {tab!r}; its tabs are {names}.", "kind": "not_found"}, "error"


def clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > CELL_MOST:
        return value[:CELL_MOST] + "…"
    return value


def cell_value(value: Any) -> Any:
    """A cell to write: a number stays a number, true/false a boolean, None nothing, anything else text."""
    if value is None:
        return ""
    if isinstance(value, (bool, int, float)):
        return value
    return str(value)


def decimal(value: Any) -> Decimal | None:
    """A number a cell holds (Google's unformatted value), exactly; None for text, a blank or a boolean."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        except InvalidOperation:
            return None
    return None


def plain(number: Decimal) -> str:
    """A Decimal as text without an exponent, without trailing zeros."""
    text = format(number.normalize(), "f")
    return text if text != "-0" else "0"


def trimmed(row: list[Any]) -> list[Any]:
    row = list(row)
    while row and (row[-1] is None or str(row[-1]).strip() == ""):
        row.pop()
    return row


def blank(row: list[Any]) -> bool:
    return not trimmed(row)


def header_names(cells: list[Any], width: int) -> list[str]:
    """Column names from the header row: a blank becomes its letter, a repeated name gets its letter beside it."""
    names, seen = [], set()
    for index in range(width):
        letter = column_letter(index + 1)
        name = str(cells[index]).strip() if index < len(cells) else ""
        name = name or letter
        if name in seen:
            name = f"{name} ({letter})"
        seen.add(name)
        names.append(name)
    return names
