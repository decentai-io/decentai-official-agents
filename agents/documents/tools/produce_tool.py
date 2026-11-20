"""Producing files people can open: Word, PDF and PowerPoint from data, and Word or PowerPoint templates filled in.
Each is built off the agent's loop, read back before it is returned, kept to the size a chat file may be, and
recorded in ``output``."""

from __future__ import annotations

import asyncio
import io
import re

from standard import ToolBase

from . import builders, deck
from .common import Refused, check_made, check_size, named, record_output, runs_right_to_left
from .reading import kind_of

PLACEHOLDER = re.compile(r"\{\{\s*([\w.\- ]+?)\s*\}\}", re.UNICODE)


class ProduceTool(ToolBase):
    id = "produce"

    async def docx(self, call):
        try:
            sections = builders.sections_of(call.inputs)
            title = str(call.inputs["title"])
            raw = await asyncio.to_thread(builders.build_docx, title, sections)
            paragraphs = await asyncio.to_thread(builders.verify_docx, raw,
                                                 builders.texts_of(title, sections))
            filename = named(call.inputs["filename"], ".docx")
            check_made(filename, raw)
        except Refused as refused:
            return refused.answer()
        except ValueError as failed:
            return {"error": f"The Word file did not read back: {failed}", "kind": "failed"}, "error"
        output_ref = await record_output(call, "docx", filename, "produce", call.inputs.get("purpose"))
        return {"file_ref": call.give(filename, raw), "filename": filename, "output_ref": output_ref,
                "verified": True, "paragraphs": paragraphs,
                "right_to_left": builders.mostly_arabic(builders.texts_of(title, sections))}, "success"

    async def pdf(self, call):
        try:
            sections = builders.sections_of(call.inputs)
            title = str(call.inputs["title"])
            raw = await asyncio.to_thread(builders.build_pdf, title, sections)
            pages = await asyncio.to_thread(builders.verify_pdf, raw, builders.texts_of(title, sections))
            filename = named(call.inputs["filename"], ".pdf")
            check_made(filename, raw)
        except Refused as refused:
            return refused.answer()
        except Exception as failed:
            return {"error": f"The PDF could not be made or did not read back: {failed}", "kind": "failed"}, "error"
        output_ref = await record_output(call, "pdf", filename, "produce", call.inputs.get("purpose"), pages)
        return {"file_ref": call.give(filename, raw), "filename": filename, "output_ref": output_ref,
                "verified": True, "pages": pages}, "success"

    async def pptx(self, call):
        template, template_name = None, ""
        try:
            outline = deck.outline_of(call.inputs)
            if call.inputs.get("template_ref"):
                path = await call.file(str(call.inputs["template_ref"]), "source")
                check_size(path)
                template, template_name = await asyncio.to_thread(path.read_bytes), path.name
                if kind_of(template_name, template) != "pptx":
                    raise Refused("The template is not a PowerPoint (.pptx) file.")
            raw, continued = await asyncio.to_thread(
                deck.build_pptx, str(call.inputs["title"]), str(call.inputs.get("subtitle") or ""), outline, template)
            slides, problems = await asyncio.to_thread(deck.verify_pptx, raw)
            filename = named(call.inputs["filename"], ".pptx", "presentation")
            check_made(filename, raw)
        except Refused as refused:
            return refused.answer()
        except Exception as failed:
            return {"error": f"The deck could not be made or did not read back: {failed}", "kind": "failed"}, "error"
        output_ref = await record_output(call, "pptx", filename, "produce", call.inputs.get("purpose"), slides,
                                         template_name)
        result = {"file_ref": call.give(filename, raw), "filename": filename, "output_ref": output_ref,
                  "verified": not problems, "slides": slides, "templated": bool(template),
                  "continued_slides": continued}
        if problems:
            result["layout_problems"] = problems[:20]
        return result, "success"

    async def fill_template(self, call):
        values = {str(k): "" if v is None else str(v) for k, v in (call.inputs.get("values") or {}).items()}
        try:
            path = await call.file(str(call.inputs["template_ref"]), "source")
            check_size(path)
            raw = await asyncio.to_thread(path.read_bytes)
            kind = kind_of(path.name, raw)
            if kind == "docx":
                made, filled, unfilled = await asyncio.to_thread(fill_docx, raw, values)
                filename = named(call.inputs["filename"], ".docx")
                count = await asyncio.to_thread(self.paragraphs, made)
            elif kind == "pptx":
                made, filled, unfilled = await asyncio.to_thread(deck.fill_pptx, raw, values)
                filename = named(call.inputs["filename"], ".pptx", "presentation")
                count, _ = await asyncio.to_thread(deck.verify_pptx, made)
            else:
                raise Refused("The template is neither a Word (.docx) nor a PowerPoint (.pptx) file.")
            check_made(filename, made)
        except Refused as refused:
            return refused.answer()
        except Exception as failed:
            return {"error": f"The template could not be filled or did not read back: {failed}",
                    "kind": "failed"}, "error"
        output_ref = await record_output(call, kind, filename, "fill", call.inputs.get("purpose"),
                                         count if kind == "pptx" else 0, path.name)
        return {"file_ref": call.give(filename, made), "filename": filename, "output_ref": output_ref,
                "filled": sorted(filled), "unfilled": sorted(unfilled),
                "unused": sorted(set(values) - filled), "verified": True}, "success"

    @staticmethod
    def paragraphs(raw: bytes) -> int:
        from docx import Document

        return sum(1 for p in Document(io.BytesIO(raw)).paragraphs if p.text.strip())


def fill_docx(raw: bytes, values):
    """A Word template's {{placeholders}} replaced, in the body, its tables, and every header and footer. A
    placeholder Word split across runs is still found: the paragraph's text is filled whole, into its first run."""
    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = Document(io.BytesIO(raw))
    except (PackageNotFoundError, KeyError, ValueError) as failed:
        raise Refused(f"The template is not a Word file: {failed}")
    filled, unfilled = set(), set()

    def fill(paragraph):
        text = paragraph.text
        names = PLACEHOLDER.findall(text)
        if not names:
            return
        for name in names:
            (filled if name in values else unfilled).add(name)
        new = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)
        if new != text and paragraph.runs:
            paragraph.runs[0].text = new
            for run in paragraph.runs[1:]:
                run.text = ""
            if runs_right_to_left(new):
                builders.right_to_left(paragraph)

    def walk(container):
        for paragraph in container.paragraphs:
            fill(paragraph)
        for table in container.tables:
            for row in table.rows:
                for cell in row.cells:
                    walk(cell)

    walk(document)
    for section in document.sections:
        for part in (section.header, section.footer, section.first_page_header, section.first_page_footer):
            try:
                if not part.is_linked_to_previous:
                    walk(part)
            except Exception:
                pass
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), filled, unfilled
