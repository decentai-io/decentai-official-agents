import asyncio
import re
from decimal import Decimal

from standard import ToolBase

from .common import decimal_of, failed, job
from .tables import Cell, Refused, load
from .workbook import build, deliver

_NOT_IN_A_SHEET_NAME = re.compile(r"[\[\]:*?/\\]")


class CombineTool(ToolBase):
    id = "combine"

    async def files(self, call):
        inputs = call.inputs
        total_column = str(inputs.get("total_column") or "").strip()
        decimal = decimal_of(call)

        parts, summary, log, taken = [], [], [], set()
        for entry in inputs["files"]:
            ref = str(entry["file_ref"])
            try:
                book = await load(call, "source", ref)
                sheet = book.sheet()
                problem = book.problem
            except Exception as exc:
                book, sheet = None, None
                problem = str(exc) if isinstance(exc, Refused) else f"could not be read: {exc}"
            filename = book.filename if book is not None else ref
            name = self._sheet_name(str(entry.get("sheet_name") or "") or filename, taken)
            if sheet is None or not sheet.columns:
                summary.append({"source": name, "filename": filename, "rows": 0, "not_a_number": 0,
                                "problem": problem or "the file is empty"})
                log.append(("", "left out", f"{filename}: {problem or 'the file is empty'}"))
                continue
            parts.append((name, filename, sheet))

        if not parts:
            return {"error": "None of the files could be read.", "files": summary, "kind": "invalid"}, "error"

        # The columns of the first file are what the others are held to: a branch that renamed or dropped one is
        # said, not smoothed over.
        expected = [c.strip().casefold() for c in parts[0][2].columns]
        union, seen = [], set()
        for _, _, sheet in parts:
            for column in sheet.columns:
                if column.strip().casefold() not in seen:
                    seen.add(column.strip().casefold())
                    union.append(column)

        sheets, all_rows, grand, untotalled = [], [], Decimal("0"), 0
        for name, filename, sheet in parts:
            readers = {}
            for index, column in enumerate(sheet.columns):
                try:
                    readers[index] = sheet.numbers(column, decimal)
                except Refused:
                    pass                       # ambiguous or mixed: its cells stay text in the workbook
            rows = [[self._typed(c, row, readers.get(i)) for i, c in enumerate(row)] for row in sheet.rows]
            sheets.append((name, list(sheet.columns), rows))
            problems = self._column_problems(sheet, expected, parts[0][2].columns)

            total, skipped = None, 0
            if total_column:
                try:
                    read = sheet.numbers(total_column, decimal)
                    index = sheet.column(total_column)
                    total = Decimal("0")
                    for row in sheet.rows:
                        number = read(row)
                        if number is None:
                            skipped += 0 if sheet.cell(row, index).blank else 1
                        else:
                            total += number
                    grand += total
                    if skipped:
                        problems.append(f"{skipped} value(s) in '{total_column}' are not numbers")
                except Refused as refused:
                    untotalled += 1
                    problems.append(f"no total: {refused}")

            for row in rows:
                by_name = {c.strip().casefold(): row[i] if i < len(row) else Cell(None)
                           for i, c in enumerate(sheet.columns)}
                all_rows.append([Cell(name)] + [by_name.get(u.strip().casefold(), Cell(None)) for u in union])
            summary.append({"source": name, "filename": filename, "rows": len(rows),
                            **({"total": self._money(total)} if total is not None else {}),
                            "not_a_number": skipped, "problem": "; ".join(problems)})
            log.append(("", "combined", f"{filename} → sheet '{name}', {len(rows)} row(s)"))

        grand_total = self._money(grand) if total_column and not untotalled else None
        total_label = f"Total {total_column}" if total_column else "Total"
        summary_rows = [[Cell(s["source"]), Cell(s["filename"]), Cell(s["rows"]),
                         Cell(Decimal(s["total"]) if "total" in s else None),
                         Cell(s["not_a_number"]), Cell(s["problem"] or None)] for s in summary]
        summary_rows.append([Cell("All files"), Cell(None), Cell(len(all_rows)),
                             Cell(Decimal(grand_total) if grand_total else None),
                             Cell(sum(s["not_a_number"] for s in summary)),
                             Cell(f"{untotalled} file(s) have no total, so there is no grand total"
                                  if untotalled else None)])
        log.insert(0, ("", "rules", "one sheet per file, as it arrived; sheet All holds every row with its source; "
                                    "totals are added in code, each file's numbers read with its own decimal mark"))
        try:
            raw, written = await asyncio.to_thread(build, [("Summary", ["Source", "File", "Rows", total_label, "Not a number", "Problem"],
                                   summary_rows),
                                  ("All", ["Source"] + union, all_rows)] + sheets, log)
            file_ref, check = await asyncio.to_thread(deliver, call, str(inputs["filename"]), raw)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        job_ref = await job(call, "combine", str(inputs["files"][0]["file_ref"]),
                            f"{len(parts)} of {len(inputs['files'])} file(s) combined, {len(all_rows)} row(s)",
                            output=check["filename"])
        return {"job_ref": job_ref, "file_ref": file_ref, "filename": check["filename"],
                "combined": len(parts), "left_out": len(inputs["files"]) - len(parts),
                "rows": len(all_rows), "sheets": check["sheets"],
                **({"grand_total": grand_total} if grand_total is not None else {}),
                "files": summary, "formulas_as_values": written.formulas, "verified": True}, "success"

    @staticmethod
    def _sheet_name(wanted: str, taken: set) -> str:
        """What Excel accepts: 31 characters, none of []:*?/\\, no two alike."""
        stem = wanted.rsplit(".", 1)[0] if wanted.lower().endswith((".csv", ".xlsx", ".tsv")) else wanted
        base = _NOT_IN_A_SHEET_NAME.sub(" ", stem).strip()[:31] or "Sheet"
        name, n = base, 2
        while name.casefold() in taken or name.casefold() in ("summary", "all", "change log"):
            suffix = f" ({n})"
            name, n = base[:31 - len(suffix)] + suffix, n + 1
        taken.add(name.casefold())
        return name

    @staticmethod
    def _column_problems(sheet, expected, first_columns):
        have = [c.strip().casefold() for c in sheet.columns]
        missing = [c for c in first_columns if c.strip().casefold() not in have]
        extra = [c for c in sheet.columns if c.strip().casefold() not in expected]
        problems = []
        if missing:
            problems.append("missing column(s): " + ", ".join(missing))
        if extra:
            problems.append("extra column(s): " + ", ".join(extra))
        return problems

    @staticmethod
    def _typed(cell: Cell, row, read) -> Cell:
        """A CSV holds text only. A number goes into the workbook as a number, read with its column's own decimal
        mark, so Excel can add it up; an identifier stays text, and so does a column whose mark can't be told."""
        if read is None or cell.formula or not isinstance(cell.value, str):
            return cell
        number = read(row)
        return cell if number is None else Cell(number)

    @staticmethod
    def _money(value):
        return None if value is None else format(value, "f")
