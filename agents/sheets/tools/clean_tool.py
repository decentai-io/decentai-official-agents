import asyncio
from standard import ToolBase

from . import numbers
from .common import decimal_of, failed, job, opened
from .tables import Cell, blank_key, key_of
from .workbook import build, deliver, text_rows

SHOWN_MOST = 100


class CleanTool(ToolBase):
    id = "clean"

    async def dedupe(self, call):
        inputs = call.inputs
        try:
            book, sheet = await opened(call)
            keys = [str(c) for c in inputs["key_columns"]]
            for name in keys:
                sheet.column(name)
        except Exception as exc:
            return failed(exc)
        how = str(inputs.get("normalize") or "lowercase")

        seen, kept, removed, log, blank = {}, [], [], [], 0
        for index, row in enumerate(sheet.rows):
            key = key_of(sheet, row, keys, how)
            if blank_key(key):
                blank += 1
                kept.append(index)
                continue
            if key in seen:
                removed.append(index)
                log.append((sheet.row_number(index), "removed",
                            f"duplicate of row {sheet.row_number(seen[key])} on {', '.join(keys)}: "
                            f"{' | '.join(key)}"))
            else:
                seen[key] = index
                kept.append(index)
        log.insert(0, ("", "rules", f"first row kept per {', '.join(keys)} ({how}); a row with a blank key is "
                                    f"kept and never counted a duplicate ({blank} such row(s))"))
        cols_k, rows_k = text_rows(sheet.columns, [sheet.rows[i] for i in kept])
        cols_r, rows_r = text_rows(sheet.columns, [sheet.rows[i] for i in removed],
                                   {"Original row": [sheet.row_number(i) for i in removed]})
        try:
            raw, written = await asyncio.to_thread(build, [("Clean", cols_k, rows_k), ("Removed", cols_r, rows_r)], log)
            file_ref, check = await asyncio.to_thread(deliver, call, str(inputs["filename"]), raw)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        job_ref = await job(call, "dedupe", book.file_ref,
                            f"{len(kept)} kept, {len(removed)} removed by {', '.join(keys)}; {blank} blank key(s)",
                            output=check["filename"])
        return {"job_ref": job_ref, "file_ref": file_ref, "filename": check["filename"], "kept": len(kept),
                "removed": len(removed), "blank_keys": blank,
                "removed_rows": [sheet.row_number(i) for i in removed][:500],
                "formulas_as_values": written.formulas, "verified": True}, "success"

    async def convert(self, call):
        """Columns of numbers typed as text, written as numbers, each read the way its column writes them. A cell
        that isn't a number is left as it was and listed, never guessed."""
        inputs = call.inputs
        try:
            book, sheet = await opened(call)
            wanted = [str(c) for c in inputs["columns"]]
            readers = {sheet.column(name): sheet.numbers(name, decimal_of(call)) for name in wanted}
        except Exception as exc:
            return failed(exc)
        rows, refused, converted = [], [], 0
        for index, row in enumerate(sheet.rows):
            cells = list(row) + [Cell(None)] * (len(sheet.columns) - len(row))
            for column, read in readers.items():
                cell = cells[column]
                if cell.blank or cell.formula or not isinstance(cell.value, str):
                    continue
                number = read(row)
                if number is None:
                    why = ("an identifier (a leading zero, a plus, or more than 15 digits); kept as text"
                           if numbers.identifier(cell.value) else "not a number in this column's writing")
                    refused.append({"row": sheet.row_number(index), "column": sheet.columns[column],
                                    "value": cell.value, "why": why})
                    continue
                cells[column] = Cell(number)
                converted += 1
            rows.append(cells)
        log = [("", "rules", "; ".join(f"{sheet.columns[c]}: decimal mark '{r.mark}'" for c, r in readers.items()))]
        log += [(r["row"], "not converted", f"{r['column']}: '{r['value']}' is {r['why']}") for r in refused]
        problems = [[Cell(r["row"]), Cell(r["column"]), Cell(r["value"]), Cell(r["why"])] for r in refused]
        try:
            raw, written = await asyncio.to_thread(build, [("Converted", list(sheet.columns), rows),
                                  ("Not converted", ["Row", "Column", "Value", "Why"], problems)], log)
            file_ref, check = await asyncio.to_thread(deliver, call, str(inputs["filename"]), raw)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        job_ref = await job(call, "convert", book.file_ref,
                            f"{converted} cell(s) made numbers in {', '.join(wanted)}; {len(refused)} left as text",
                            output=check["filename"])
        return {"job_ref": job_ref, "file_ref": file_ref, "filename": check["filename"], "converted": converted,
                "not_converted": len(refused), "not_converted_cells": refused[:SHOWN_MOST],
                "decimal_marks": {sheet.columns[c]: r.mark for c, r in readers.items()},
                "formulas_as_values": written.formulas, "verified": True}, "success"
