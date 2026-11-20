from standard import ToolBase

from .common import failed, opened
from .tables import load, row_dict


class ReadTool(ToolBase):
    id = "read"

    async def inspect(self, call):
        try:
            header = call.inputs.get("header_row")
            book = await load(call, "source", str(call.inputs["file_ref"]), str(call.inputs.get("sheet") or ""),
                              int(header) if header else None)
        except Exception as exc:
            return failed(exc)
        result = {"file_ref": book.file_ref, "filename": book.filename, "kind": book.kind,
                  "sheets": [{"name": s.name, "rows": len(s.rows), "header_row": s.header_row,
                              "merged_ranges": s.merged} for s in book.sheets],
                  "columns": [{"sheet": s.name, **column} for s in book.sheets for column in s.profile()]}
        if book.encoding:
            result["encoding"] = book.encoding
        if book.delimiter:
            result["delimiter"] = book.delimiter
        found = [s for s in book.sheets if s.found_header]
        notes = [f"Sheet '{s.name}': the header is row {s.header_row}; the rows above it are titles, not data."
                 for s in found]
        unsure = [c for s in book.sheets for c in s.profile() if c.get("decimal") in ("ambiguous", "mixed")]
        if unsure:
            notes.append("Columns whose decimal mark can't be told: " + ", ".join(c["name"] for c in unsure)
                         + ". Ask the person whether 1,234 means one thousand two hundred and thirty-four or 1.234, "
                           "and pass decimal.")
        if notes:
            result["notes"] = notes
        if book.problem:
            result["problem"] = book.problem
        return result, "success"

    async def rows(self, call):
        try:
            book, sheet = await opened(call)
        except Exception as exc:
            return failed(exc)
        wanted = [str(c) for c in call.inputs.get("columns") or []]
        have = {" ".join(x.split()).casefold() for x in sheet.columns}
        unknown = [c for c in wanted if " ".join(c.split()).casefold() not in have]
        if unknown:
            return {"error": f"No such column(s): {', '.join(unknown)}. Columns: {', '.join(sheet.columns)}",
                    "kind": "invalid"}, "error"
        start = int(call.inputs.get("from_row") or 1) - 1
        limit = int(call.inputs.get("max_rows") or 100)
        formulas = bool(call.inputs.get("formulas"))
        chosen = sheet.rows[start: start + limit]
        asked = {" ".join(c.split()).casefold() for c in wanted}
        result = {"sheet": sheet.name, "header_row": sheet.header_row,
                  "columns": [c for c in sheet.columns if not asked or " ".join(c.split()).casefold() in asked],
                  "rows": [row_dict(sheet, r, wanted or None, formulas) for r in chosen],
                  "first_sheet_row": sheet.row_number(start),
                  "total_rows": len(sheet.rows), "truncated": start + limit < len(sheet.rows)}
        if result["truncated"]:
            result["next_row"] = start + limit + 1
        if not chosen and sheet.rows:
            result["note"] = f"No rows at from_row {start + 1}: this sheet has {len(sheet.rows)}."
        return result, "success"
