"""Rows kept by conditions and put in order, into a new file (CSV or Excel). Every comparison is code: a number is
compared as a number read with its column's own decimal mark, a date as a date, anything else as text without
regard to case."""

import asyncio
import re
from datetime import date, datetime
from decimal import Decimal

from standard import ToolBase

from . import numbers
from .common import decimal_of, failed, job, opened
from .tables import Refused
from .workbook import build, check_csv, deliver, named, write_csv

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
ORDERING = ("gt", "gte", "lt", "lte")


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and ISO_DATE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def ordered(op, left, right) -> bool:
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right,
            "eq": left == right, "ne": left != right}[op]


class Condition:
    def __init__(self, sheet, spec, decimal):
        self.column = str(spec["column"])
        self.index = sheet.column(self.column)
        self.op = str(spec["op"])
        self.value = spec.get("value")
        self.sheet = sheet
        self.number = numbers.plain(self.value) if self.op not in ("contains", "not_contains") else None
        self.date = as_date(self.value) if isinstance(self.value, str) else None
        self.read = None
        if self.number is not None and self.date is None:
            self.read = sheet.numbers(self.column, decimal)        # refused when the column can't be read
        if self.op in ORDERING and self.number is None and self.date is None:
            raise Refused(f"'{self.op}' on {self.column} needs a number or a date (YYYY-MM-DD), not {self.value!r}.")
        if self.op not in ("blank", "not_blank") and self.value is None:
            raise Refused(f"The condition on {self.column} ({self.op}) needs a value.")

    def holds(self, row) -> bool:
        cell = self.sheet.cell(row, self.index)
        if self.op == "blank":
            return cell.blank
        if self.op == "not_blank":
            return not cell.blank
        text = cell.text
        if self.op in ("contains", "not_contains"):
            found = " ".join(str(self.value).split()).casefold() in " ".join(text.split()).casefold()
            return found if self.op == "contains" else not found
        if self.date is not None:
            mine = as_date(cell.shown)
            return mine is not None and ordered(self.op, mine, self.date)
        if self.read is not None:
            mine = self.read(row)
            if mine is not None:
                return ordered(self.op, mine, self.number)
            if self.op in ORDERING:
                return False
        same = " ".join(text.split()).casefold() == " ".join(str(self.value).split()).casefold()
        return same if self.op == "eq" else not same if self.op == "ne" else False


class ArrangeTool(ToolBase):
    id = "arrange"

    async def rows(self, call):
        inputs = call.inputs
        decimal = decimal_of(call)
        try:
            book, sheet = await opened(call)
            conditions = [Condition(sheet, spec, decimal) for spec in inputs.get("filters") or []]
            orders = [self._order(sheet, spec, decimal) for spec in inputs.get("sort") or []]
            columns = [sheet.columns[sheet.column(c)] for c in inputs.get("columns") or []] or list(sheet.columns)
        except Exception as exc:
            return failed(exc)
        every = (inputs.get("match") or "all") == "all"
        kept = []
        for index, row in enumerate(sheet.rows):
            if all(c.blank for c in row):
                continue
            verdicts = (c.holds(row) for c in conditions)
            if not conditions or (all(verdicts) if every else any(verdicts)):
                kept.append(index)
        for key, descending in reversed(orders):
            present = [i for i in kept if key(sheet.rows[i]) is not None]
            absent = [i for i in kept if key(sheet.rows[i]) is None]
            present.sort(key=lambda i: key(sheet.rows[i]), reverse=descending)
            kept = present + absent                                     # blanks and the unreadable go last
        picked = [sheet.column(c) for c in columns]
        fmt = str(inputs.get("format") or "xlsx")
        try:
            if fmt == "csv":
                lines = [[sheet.cell(sheet.rows[i], c).text for c in picked] for i in kept]
                delimiter = {"comma": ",", "semicolon": ";", "tab": "\t"}[str(inputs.get("delimiter") or "comma")]
                raw = await asyncio.to_thread(write_csv, columns, lines, delimiter)
                await asyncio.to_thread(check_csv, raw, len(lines), delimiter)
                filename = named(str(inputs["filename"]), ".csv")
                file_ref = call.give(filename, raw)
            else:
                body = [[sheet.cell(sheet.rows[i], c) for c in picked] for i in kept]
                log = [("", "rules", self._said(conditions, every, inputs.get("sort") or []))]
                raw, _ = await asyncio.to_thread(build, [("Rows", columns, body)], log)
                file_ref, check = await asyncio.to_thread(deliver, call, str(inputs["filename"]), raw)
                filename = check["filename"]
        except Exception as exc:
            return {"error": f"The file did not read back: {exc}"}, "error"
        job_ref = await job(call, "arrange", book.file_ref,
                            f"{len(kept)} of {len(sheet.rows)} row(s) kept; "
                            f"{self._said(conditions, every, inputs.get('sort') or [])}", output=filename)
        return {"job_ref": job_ref, "file_ref": file_ref, "filename": filename, "rows": len(kept),
                "rows_read": len(sheet.rows), "first_rows": [sheet.row_number(i) for i in kept[:20]],
                "verified": True}, "success"

    @staticmethod
    def _order(sheet, spec, decimal):
        name, how = str(spec["column"]), str(spec.get("as") or "auto")
        index = sheet.column(name)
        cells = [sheet.cell(r, index) for r in sheet.rows]
        filled = [c for c in cells if not c.blank or c.formula]
        if how == "auto":
            if filled and all(as_date(c.shown) is not None for c in filled):
                how = "date"
            elif filled and all(numbers.native(c.shown) is not None or numbers.shape(c.text) is not None
                                for c in filled):
                how = "number"
            else:
                how = "text"
        if how == "number":
            read = sheet.numbers(name, decimal)
            return (lambda row: read(row)), bool(spec.get("descending"))
        if how == "date":
            return (lambda row: as_date(sheet.cell(row, index).shown)), bool(spec.get("descending"))
        return ((lambda row: " ".join(sheet.cell(row, index).text.split()).casefold() or None),
                bool(spec.get("descending")))

    @staticmethod
    def _said(conditions, every, sort) -> str:
        said = (" and " if every else " or ").join(f"{c.column} {c.op} {c.value!r}" if c.value is not None
                                                   else f"{c.column} {c.op}" for c in conditions) or "every row"
        if sort:
            said += "; ordered by " + ", ".join(f"{s['column']}{' descending' if s.get('descending') else ''}"
                                               for s in sort)
        return said
