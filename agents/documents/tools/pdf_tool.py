"""PDF pages moved, not retyped: merge several files, split one, rotate pages, list and fill a form's fields. The
pages are copied as they are (text, fonts, pictures), and every new file is opened again before it is returned."""

from __future__ import annotations

import asyncio
import io
from pathlib import PurePosixPath
from typing import Any, Dict, List, Tuple

from standard import ToolBase

from .common import Refused, check_made, check_size, named, record_output
from .reading import ranges

MOST_FILES = 20


async def opened(call, ref: str) -> Tuple[str, bytes]:
    path = await call.file(str(ref), "source")
    check_size(path)
    raw = await asyncio.to_thread(path.read_bytes)
    if raw[:5] != b"%PDF-":
        raise Refused(f"{path.name} is not a PDF.")
    return path.name, raw


def reader_of(name: str, raw: bytes):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Refused(f"{name} is password-protected: ask for a copy without a password.")
        len(reader.pages)
        return reader
    except Refused:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError, NotImplementedError) as failed:
        raise Refused(f"{name} could not be opened as a PDF: {failed}")


def written(writer) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    raw = buffer.getvalue()
    from pypdf import PdfReader

    PdfReader(io.BytesIO(raw)).pages[0]       # it opens again, and has a page
    return raw


def stem(name: str) -> str:
    return PurePosixPath(name).stem or "document"


class PdfTool(ToolBase):
    id = "pdf"

    async def merge(self, call):
        refs = [str(r) for r in call.inputs["file_refs"]]
        try:
            files = [await opened(call, ref) for ref in refs]

            def merged():
                from pypdf import PdfWriter

                writer, counts = PdfWriter(), []
                for name, raw in files:
                    reader = reader_of(name, raw)
                    writer.append(reader)
                    counts.append(len(reader.pages))
                return written(writer), counts

            raw, counts = await asyncio.to_thread(merged)
            filename = named(call.inputs.get("filename") or f"{stem(files[0][0])}-merged", ".pdf")
            check_made(filename, raw)
        except Refused as refused:
            return refused.answer()
        output_ref = await record_output(call, "pdf", filename, "merge", call.inputs.get("purpose"), sum(counts),
                                         ", ".join(name for name, _ in files))
        return {"file_ref": call.give(filename, raw), "filename": filename, "output_ref": output_ref,
                "pages": sum(counts), "parts": [{"filename": name, "pages": count}
                                                for (name, _), count in zip(files, counts)]}, "success"

    async def split(self, call):
        try:
            name, raw = await opened(call, call.inputs["file_ref"])
            reader = await asyncio.to_thread(reader_of, name, raw)
            total = len(reader.pages)
            if call.inputs.get("ranges"):
                groups = [ranges(text, total) for text in call.inputs["ranges"]]
                labels = [str(text).replace(" ", "") for text in call.inputs["ranges"]]
            else:
                every = int(call.inputs.get("every") or 1)
                groups = [list(range(n, min(n + every, total + 1))) for n in range(1, total + 1, every)]
                labels = [f"{g[0]}-{g[-1]}" if len(g) > 1 else str(g[0]) for g in groups]
            if len(groups) > MOST_FILES:
                raise Refused(f"That makes {len(groups)} files; at most {MOST_FILES} come from one split. Give "
                              f"larger parts (every) or fewer ranges.")

            def parts() -> List[bytes]:
                from pypdf import PdfWriter

                made = []
                for group in groups:
                    writer = PdfWriter()
                    for number in group:
                        writer.add_page(reader.pages[number - 1])
                    made.append(written(writer))
                return made

            made = await asyncio.to_thread(parts)
            names = [named(f"{stem(name)}-pages-{label}", ".pdf") for label in labels]
            for filename, data in zip(names, made):
                check_made(filename, data)
        except Refused as refused:
            return refused.answer()
        out = []
        for filename, data, group in zip(names, made, groups):
            output_ref = await record_output(call, "pdf", filename, "split", call.inputs.get("purpose"), len(group),
                                             name)
            out.append({"file_ref": call.give(filename, data), "filename": filename, "output_ref": output_ref,
                        "pages": len(group), "from_pages": f"{group[0]}-{group[-1]}" if len(group) > 1
                        else str(group[0])})
        return {"files": out, "pages_total": total}, "success"

    async def rotate(self, call):
        degrees = int(call.inputs["degrees"]) % 360
        try:
            name, raw = await opened(call, call.inputs["file_ref"])
            reader = await asyncio.to_thread(reader_of, name, raw)
            total = len(reader.pages)
            pages = ranges(call.inputs["pages"], total) if call.inputs.get("pages") else list(range(1, total + 1))

            def turned() -> bytes:
                from pypdf import PdfWriter

                writer = PdfWriter(clone_from=reader)
                for number in pages:
                    writer.pages[number - 1].rotate(degrees)
                return written(writer)

            made = await asyncio.to_thread(turned)
            filename = named(call.inputs.get("filename") or f"{stem(name)}-rotated", ".pdf")
            check_made(filename, made)
        except Refused as refused:
            return refused.answer()
        output_ref = await record_output(call, "pdf", filename, "rotate", call.inputs.get("purpose"), total, name)
        return {"file_ref": call.give(filename, made), "filename": filename, "output_ref": output_ref,
                "rotated_pages": pages, "degrees": degrees, "pages": total}, "success"

    # -- forms -----------------------------------------------------------------------------------------------------

    async def fields(self, call):
        try:
            name, raw = await opened(call, call.inputs["file_ref"])
            reader = await asyncio.to_thread(reader_of, name, raw)
            found, xfa = await asyncio.to_thread(form_fields, reader)
        except Refused as refused:
            return refused.answer()
        result = {"file_ref": str(call.inputs["file_ref"]), "filename": name, "fields": found, "count": len(found)}
        if xfa:
            result["problem"] = ("This form is an XFA form (made in Adobe LiveCycle): its fields can't be filled "
                                 "here. Fill it in Adobe Reader.")
        elif not found:
            result["problem"] = "This PDF has no form fields to fill (a printed form scanned to PDF has none)."
        return result, "success"

    async def fill(self, call):
        values = dict(call.inputs.get("values") or {})
        try:
            name, raw = await opened(call, call.inputs["file_ref"])
            reader = await asyncio.to_thread(reader_of, name, raw)
            found, xfa = await asyncio.to_thread(form_fields, reader)
            if xfa:
                raise Refused("This form is an XFA form (made in Adobe LiveCycle): it can't be filled here.")
            if not found:
                raise Refused("This PDF has no form fields to fill.")
            given = settled(found, values)
            made = await asyncio.to_thread(filled_form, reader, given)
            filename = named(call.inputs.get("filename") or f"{stem(name)}-filled", ".pdf")
            check_made(filename, made)
            back = await asyncio.to_thread(read_values, made)
            wrong = [n for n, v in given.items() if back.get(n) != v]
            if wrong:
                raise Refused(f"The filled form did not read back the values of: {', '.join(wrong[:10])}.", "failed")
        except Refused as refused:
            return refused.answer()
        output_ref = await record_output(call, "pdf", filename, "form", call.inputs.get("purpose"),
                                         len(reader.pages), name)
        by_name = {f["name"]: f for f in found}
        unfilled = [f["name"] for f in found if f["name"] not in given and f["type"] not in ("button", "signature")
                    and not f.get("value")]
        return {"file_ref": call.give(filename, made), "filename": filename, "output_ref": output_ref,
                "filled": sorted(given), "unfilled": unfilled,
                "required_unfilled": [n for n in unfilled if by_name[n].get("required")],
                "verified": True}, "success"


KINDS = {"/Tx": "text", "/Btn": "checkbox", "/Ch": "choice", "/Sig": "signature"}


def form_fields(reader) -> Tuple[List[Dict[str, Any]], bool]:
    """Every field of the form: its full name, kind, value, the values it takes, and whether it is required or
    read-only. Whether the form is XFA."""
    root = reader.trailer["/Root"]
    form = root.get("/AcroForm")
    form = form.get_object() if form is not None else None
    xfa = bool(form is not None and form.get("/XFA") is not None and not (form.get("/Fields") or []))
    fields = reader.get_fields() or {}
    out = []
    for name, field in fields.items():
        kind = KINDS.get(field.get("/FT"), "")
        if not kind:
            continue
        flags = int(field.get("/Ff") or 0)
        entry: Dict[str, Any] = {"name": name, "type": kind, "required": bool(flags & 2), "read_only": bool(flags & 1)}
        if kind == "checkbox":
            if flags & (1 << 16):
                entry["type"] = "button"
            elif flags & (1 << 15):
                entry["type"] = "radio"
            states = [str(s) for s in (field.get("/_States_") or []) if str(s) != "/Off"]
            entry["options"] = [s.lstrip("/") for s in states]
        if kind == "choice":
            entry["options"] = [str(o[-1] if isinstance(o, list) else o) for o in (field.get("/Opt") or [])]
        if kind == "text" and field.get("/MaxLen") is not None:
            entry["max_length"] = int(field["/MaxLen"])
        value = field.get("/V")
        if value is not None and str(value) not in ("", "/Off"):
            entry["value"] = str(value).lstrip("/") if kind == "checkbox" else str(value)
        out.append(entry)
    return out, xfa


def settled(found: List[Dict[str, Any]], values: Dict[str, Any]) -> Dict[str, str]:
    """The values as the form stores them, every one checked first; nothing is written when one is wrong."""
    by_name = {f["name"]: f for f in found}
    unknown = [n for n in values if n not in by_name]
    if unknown:
        raise Refused(f"The form has no field named {', '.join(unknown[:10])}. Its fields: "
                      f"{', '.join(list(by_name)[:30])}.")
    out, wrong = {}, []
    for name, value in values.items():
        field = by_name[name]
        kind, text = field["type"], "" if value is None else str(value)
        if field.get("read_only") or kind in ("signature", "button"):
            wrong.append(f"{name} can't be filled here ({'read-only' if field.get('read_only') else kind})")
        elif kind == "checkbox":
            on = (field.get("options") or ["Yes"])[0]
            if value is True or text.lower() in ("true", "yes", "on", "1", "x", "نعم", on.lower()):
                out[name] = f"/{on}"
            elif value is False or text.lower() in ("false", "no", "off", "0", "", "لا"):
                out[name] = "/Off"
            else:
                wrong.append(f"{name} is a checkbox: give true or false")
        elif kind == "radio":
            if text not in (field.get("options") or []):
                wrong.append(f"{name} takes one of {', '.join(field.get('options') or [])}")
            else:
                out[name] = f"/{text}"
        elif kind == "choice":
            if field.get("options") and text not in field["options"]:
                wrong.append(f"{name} takes one of {', '.join(field['options'][:20])}")
            else:
                out[name] = text
        else:
            if field.get("max_length") and len(text) > field["max_length"]:
                wrong.append(f"{name} takes at most {field['max_length']} characters, not {len(text)}")
            else:
                out[name] = text
    if wrong:
        raise Refused("Nothing was filled: " + "; ".join(wrong[:10]) + ".")
    return out


def filled_form(reader, values: Dict[str, str]) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=reader)
    writer.set_need_appearances_writer(True)      # a viewer redraws the fields, so Arabic shows in its own font
    for page in writer.pages:
        if page.get("/Annots"):
            writer.update_page_form_field_values(page, values, auto_regenerate=False)
    return written(writer)


def read_values(raw: bytes) -> Dict[str, str]:
    from pypdf import PdfReader

    out = {}
    for name, field in (PdfReader(io.BytesIO(raw)).get_fields() or {}).items():
        value = field.get("/V")
        if value is not None:
            out[name] = str(value)
    return out
