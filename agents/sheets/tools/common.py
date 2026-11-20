"""What every tool of the agent does the same way: read the file a call names, refuse out loud, keep the job."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .tables import Refused, load

log = logging.getLogger("sheets")


async def opened(call, ref: Optional[str] = None, sheet: Optional[str] = None, header: Optional[int] = None):
    """(the workbook, the sheet) a call names, by its inputs file_ref, sheet and header_row unless told otherwise."""
    inputs = call.inputs
    ref = str(ref if ref is not None else inputs["file_ref"])
    name = str(sheet if sheet is not None else inputs.get("sheet") or "")
    header = header if header is not None else inputs.get("header_row")
    book = await load(call, "source", ref, name, int(header) if header else None)
    if book.problem and not book.sheets:
        raise Refused(book.problem)
    return book, book.sheet(name)


def failed(exc: Exception):
    """A refusal with its kind; anything else as what it is, in words."""
    if isinstance(exc, Refused):
        return exc.answer()
    if isinstance(exc, ValueError):
        return {"error": str(exc), "kind": "invalid"}, "error"
    if getattr(exc, "code", "") == "not_found":
        return {"error": str(exc), "kind": "not_found"}, "error"
    log.exception("failed")
    return {"error": f"The file could not be read: {exc}"}, "error"


async def job(call, kind: str, source_a: str, summary: str, source_b: str = "", output: str = "",
              result: Optional[Dict[str, Any]] = None) -> str:
    """The trail: what was done, on what, into which file. What it said and found stays in values: a summary can
    name the values a filter was given."""
    fields: Dict[str, Any] = {"kind": kind, "source_a": str(source_a)[:500], "summary": summary[:2000]}
    if source_b:
        fields["source_b"] = str(source_b)[:500]
    if output:
        fields["output_ref"] = output[:200]
    if result:
        fields["result"] = result
    return await call.records.create("job", fields)


def decimal_of(call) -> Optional[str]:
    mark = call.inputs.get("decimal")
    return mark if mark in (".", ",") else None
