"""Arithmetic, in code, in exact decimals: groups and their measures, and a pivot of sums. A cell that is not a
number is skipped and counted, never guessed; a column whose decimal mark can't be told is refused until the person
names it; and the number of groups is bounded, refused past it with the count."""

import asyncio
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal

from standard import ToolBase

from . import numbers
from .common import decimal_of, failed, job, opened
from .tables import Cell, Refused
from .workbook import build, deliver

MAX_GROUPS = 500
PIVOT_ROWS = 300
PIVOT_COLUMNS = 40
SHOWN_MOST = 20


def _text(value: Decimal) -> str:
    return numbers.text(value)


def _average(values):
    places = max([-v.as_tuple().exponent for v in values if v.as_tuple().exponent < 0] + [2])
    return (sum(values) / len(values)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


class CalcTool(ToolBase):
    id = "calc"

    async def aggregate(self, call):
        inputs = call.inputs
        try:
            book, sheet = await opened(call)
            group_by = [str(c) for c in inputs.get("group_by") or []]
            measures = [(str(m["column"]), str(m["op"])) for m in inputs["measures"]]
            for name in group_by + [c for c, _ in measures]:
                sheet.column(name)
            readers = {column: sheet.numbers(column, decimal_of(call)) for column, op in measures if op != "count"}
        except Exception as exc:
            return failed(exc)

        groups, skipped, examples = OrderedDict(), 0, []
        for index, row in enumerate(sheet.rows):
            if all(c.blank for c in row):
                continue
            key = tuple(sheet.cell(row, c).text for c in group_by)
            if key not in groups and len(groups) >= MAX_GROUPS:
                return {"error": f"Grouping by {', '.join(group_by)} makes more than {MAX_GROUPS} groups; at most "
                                 f"{MAX_GROUPS} are computed. Group by fewer or broader columns, or filter first "
                                 f"(arrange_rows). Nothing was computed.", "kind": "invalid"}, "error"
            bucket = groups.setdefault(key, {(c, op): [] for c, op in measures})
            for column, op in measures:
                cell = sheet.cell(row, column)
                if op == "count":
                    if not cell.blank:
                        bucket[(column, op)].append(Decimal(1))
                    continue
                number = readers[column](row)
                if number is None:
                    if not cell.blank:
                        skipped += 1
                        if len(examples) < SHOWN_MOST:
                            examples.append({"row": sheet.row_number(index), "column": column, "value": cell.text})
                    continue
                bucket[(column, op)].append(number)

        rows = []
        for key, bucket in groups.items():
            out = dict(zip(group_by, key))
            for (column, op), values in bucket.items():
                label = f"{op}({column})"
                if op == "count":
                    out[label] = len(values)
                elif not values:
                    out[label] = None
                elif op == "sum":
                    out[label] = _text(sum(values))
                elif op == "avg":
                    out[label] = _text(_average(values))
                elif op == "min":
                    out[label] = _text(min(values))
                elif op == "max":
                    out[label] = _text(max(values))
            rows.append(out)
        summary = (f"{len(rows)} group(s) by {', '.join(group_by) or 'nothing'}; "
                   f"{', '.join(f'{op}({c})' for c, op in measures)}; {skipped} non-numeric cell(s) skipped")
        job_ref = await job(call, "aggregate", book.file_ref, summary)
        return {"job_ref": job_ref, "groups": rows, "rows_read": len(sheet.rows), "skipped_cells": skipped,
                "skipped_examples": examples,
                "decimal_marks": {c: r.mark for c, r in readers.items()}}, "success"

    async def pivot(self, call):
        """Rows grouped by ``rows`` (one or two columns) against the values of ``across``, each crossing the sum
        (or count) of ``values``, with a total for every row and column and a grand total."""
        inputs = call.inputs
        try:
            book, sheet = await opened(call)
            down = [str(c) for c in inputs["rows"]]
            across = str(inputs.get("across") or "")
            value = str(inputs["values"])
            op = str(inputs.get("op") or "sum")
            for name in down + ([across] if across else []):
                sheet.column(name)
            read = sheet.numbers(value, decimal_of(call)) if op == "sum" else None
            sheet.column(value)
        except Exception as exc:
            return failed(exc)

        cells, row_keys, col_keys, skipped, examples = {}, {}, {}, 0, []
        for index, row in enumerate(sheet.rows):
            if all(c.blank for c in row):
                continue
            r = tuple(sheet.cell(row, c).text for c in down)
            c = sheet.cell(row, across).text if across else "Total"
            row_keys.setdefault(r, None)
            col_keys.setdefault(c, None)
            if len(row_keys) > PIVOT_ROWS or len(col_keys) > PIVOT_COLUMNS:
                return {"error": f"The pivot would have more than {PIVOT_ROWS} rows or {PIVOT_COLUMNS} columns "
                                 f"(so far {len(row_keys)} × {len(col_keys)}); filter first or pivot on broader "
                                 f"columns. Nothing was computed.", "kind": "invalid"}, "error"
            target = sheet.cell(row, value)
            if op == "count":
                number = Decimal(0) if target.blank else Decimal(1)
            else:
                number = read(row)
                if number is None:
                    if not target.blank:
                        skipped += 1
                        if len(examples) < SHOWN_MOST:
                            examples.append({"row": sheet.row_number(index), "column": value, "value": target.text})
                    continue
            cells[(r, c)] = cells.get((r, c), Decimal(0)) + number

        def order(keys):
            return sorted(keys, key=lambda k: tuple(str(p).casefold() for p in (k if isinstance(k, tuple) else (k,))))

        rows_sorted, cols_sorted = order(row_keys), order(col_keys)
        header = down + [str(c) for c in cols_sorted] + ["Total"]
        table, col_totals, grand = [], {c: Decimal(0) for c in cols_sorted}, Decimal(0)
        for r in rows_sorted:
            line, total = list(r), Decimal(0)
            for c in cols_sorted:
                found = cells.get((r, c))
                line.append(_text(found) if found is not None else "")
                if found is not None:
                    total += found
                    col_totals[c] += found
            grand += total
            table.append(line + [_text(total)])
        totals = ["Total"] + [""] * (len(down) - 1) + [_text(col_totals[c]) for c in cols_sorted] + [_text(grand)]

        result = {"columns": header, "rows": table, "totals": totals, "grand_total": _text(grand),
                  "skipped_cells": skipped, "skipped_examples": examples}
        output = ""
        if inputs.get("filename"):
            body = [[Cell(Decimal(v)) if i >= len(down) and v != "" else Cell(v or None) for i, v in enumerate(line)]
                    for line in table + [totals]]
            try:
                raw, _ = await asyncio.to_thread(build, [("Pivot", header, body)],
                               [("", "rules", f"{op} of {value} by {', '.join(down)}"
                                 + (f" across {across}" if across else "")
                                 + f"; {skipped} non-numeric cell(s) skipped")])
                result["file_ref"], check = await asyncio.to_thread(deliver, call, str(inputs["filename"]), raw)
                output = result["filename"] = check["filename"]
            except Exception as exc:
                return {"error": f"The workbook did not read back: {exc}"}, "error"
        result["job_ref"] = await job(call, "pivot", book.file_ref,
                                      f"{op} of {value}: {len(table)} row(s) × {len(cols_sorted)} column(s); "
                                      f"{skipped} non-numeric cell(s) skipped", output=output)
        return result, "success"
