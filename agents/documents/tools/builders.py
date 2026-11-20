"""Word and PDF files built from a title and sections, in any language this agent's fonts cover, Arabic included.

A PDF is written with fpdf2 and the fonts bundled in ``fonts/``: Noto Sans for Latin, Greek and Cyrillic, and Noto
Naskh Arabic beside it, shaped by HarfBuzz so Arabic letters join and run right to left. A paragraph whose first
strong letter is Arabic is set right to left and aligned right; a table in a mostly Arabic document runs its columns
from the right. A character neither font has is refused by name before anything is written, never turned into "?".

A Word file marks an Arabic paragraph right to left (``w:bidi``, ``w:rtl``) and an Arabic table's columns from the
right (``w:bidiVisual``), so Word lays it out as an Arabic writer would.

Every file is read back before it is returned: the PDF's text must carry every letter that was written.
"""

from __future__ import annotations

import io
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from .common import Refused, folded, mostly_arabic, runs_right_to_left

FONTS = Path(__file__).resolve().parent.parent / "fonts"
FACES = {("sans", ""): "NotoSans-Regular.ttf", ("sans", "B"): "NotoSans-Bold.ttf",
         ("naskh", ""): "NotoNaskhArabic-Regular.ttf", ("naskh", "B"): "NotoNaskhArabic-Bold.ttf"}


def sections_of(inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for section in inputs["sections"]:
        columns = [str(c) for c in section.get("table_columns") or []]
        rows = [[str(cell) for cell in row] if isinstance(row, (list, tuple)) else [str(row)]
                for row in section.get("table_rows") or []]
        if rows and not columns:
            raise Refused("A table needs table_columns as well as table_rows.")
        if rows and any(len(r) != len(columns) for r in rows):
            wrong = next(n for n, r in enumerate(rows, 1) if len(r) != len(columns))
            raise Refused(f"Every table row needs one cell per column: row {wrong} has "
                          f"{len(rows[wrong - 1])}, the table {len(columns)} columns.")
        out.append({"heading": str(section.get("heading") or ""),
                    "paragraphs": [str(p) for p in section.get("paragraphs") or []],
                    "columns": columns, "rows": rows})
    return out


def texts_of(title: str, sections: List[Dict[str, Any]]) -> List[str]:
    found = [title]
    for section in sections:
        found += [section["heading"], *section["paragraphs"], *section["columns"]]
        found += [cell for row in section["rows"] for cell in row]
    return [t for t in found if t]


# -- PDF -----------------------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def covered() -> frozenset:
    """Every character the bundled fonts draw."""
    from fontTools.ttLib import TTFont

    found = set()
    for name in FACES.values():
        with TTFont(str(FONTS / name), lazy=True) as font:
            found.update(font.getBestCmap() or {})
    return frozenset(found)


def uncovered(texts: List[str]) -> List[str]:
    have = covered()
    missing = []
    for text in texts:
        for ch in text:
            if ord(ch) in have or ch in "\n\t\r" or unicodedata.category(ch) in ("Cc", "Cf", "Zs"):
                continue
            if ch not in missing:
                missing.append(ch)
    return missing


def build_pdf(title: str, sections: List[Dict[str, Any]]) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    texts = texts_of(title, sections)
    missing = uncovered(texts)
    if missing:
        named = ", ".join(f"'{ch}' ({unicodedata.name(ch, 'U+%04X' % ord(ch))})" for ch in missing[:12])
        raise Refused(f"{len(missing)} character(s) are in no font this agent carries, so they would print as "
                      f"boxes: {named}. Latin, Greek, Cyrillic and Arabic script are covered; take these out, or "
                      f"produce a Word file instead.")
    arabic = mostly_arabic(texts)
    pdf = FPDF(format="A4")
    pdf.set_title(title)
    pdf.set_creator("DecentAI Documents")
    for (family, style), file in FACES.items():
        pdf.add_font(family, style, str(FONTS / file))
    pdf.set_fallback_fonts(["naskh"], exact_match=False)
    pdf.set_text_shaping(use_shaping_engine=True)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    flow = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}

    def write(text: str, size: float, height: float, bold: bool = False) -> None:
        pdf.set_font("sans", "B" if bold else "", size)
        right = runs_right_to_left(text) or (arabic and not text.strip()[:1].isalpha())
        pdf.multi_cell(0, height, text, align="R" if right else "L", **flow)

    write(title, 17, 9, bold=True)
    pdf.ln(2)
    for section in sections:
        if section["heading"]:
            write(section["heading"], 13, 7.5, bold=True)
        for text in section["paragraphs"]:
            write(text, 10.5, 6)
            pdf.ln(1)
        if section["columns"]:
            columns, rows = section["columns"], section["rows"]
            leftward = mostly_arabic(columns + [cell for row in rows for cell in row])
            if leftward:                    # an Arabic table reads from the right: its first column stands there
                columns, rows = columns[::-1], [row[::-1] for row in rows]
            pdf.set_font("sans", "", 9.5)
            with pdf.table(first_row_as_headings=True, line_height=5.5) as table:
                for cells in [columns, *rows]:
                    row = table.row()
                    for cell in cells:
                        right = runs_right_to_left(cell) or (leftward and not any(c.isalpha() for c in cell))
                        row.cell(cell, align="RIGHT" if right else "LEFT")
            pdf.ln(3)
    return bytes(pdf.output())


def verify_pdf(raw: bytes, texts: List[str]) -> int:
    """The PDF opened again and its text read out: every letter and digit that was written must come back (a font
    that lacked one would have dropped it). The page count."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    found = folded("".join((page.extract_text() or "") for page in reader.pages))
    if not found.strip():
        raise ValueError("no text came back from the PDF")
    wanted = {ch for ch in folded(" ".join(texts)) if ch.isalnum()}
    lost = sorted(ch for ch in wanted if ch not in found)
    if lost:
        raise ValueError(f"{len(lost)} character(s) did not come back: {''.join(lost[:20])}")
    return len(reader.pages)


# -- Word ----------------------------------------------------------------------------------------------------------

def right_to_left(paragraph, cs_font: str = "Arial") -> None:
    """A paragraph laid out right to left, as Word does for Arabic: the paragraph's direction, its alignment, and
    each run's direction and complex-script font."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = paragraph._p.get_or_add_pPr()
    if properties.find(qn("w:bidi")) is None:
        properties.insert(0, OxmlElement("w:bidi"))
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in paragraph.runs:
        run_properties = run._r.get_or_add_rPr()
        if run_properties.find(qn("w:rtl")) is None:
            run_properties.append(OxmlElement("w:rtl"))
        fonts = run_properties.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            run_properties.insert(0, fonts)
        fonts.set(qn("w:cs"), cs_font)
        language = run_properties.find(qn("w:lang"))
        if language is None:
            language = OxmlElement("w:lang")
            run_properties.append(language)
        language.set(qn("w:bidi"), "ar-AE")


def table_right_to_left(table) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = table._tbl.tblPr
    if properties.find(qn("w:bidiVisual")) is None:
        properties.append(OxmlElement("w:bidiVisual"))


def build_docx(title: str, sections: List[Dict[str, Any]]) -> bytes:
    from docx import Document

    arabic = mostly_arabic(texts_of(title, sections))
    document = Document()

    def directed(paragraph, text):
        if runs_right_to_left(text) or (arabic and not any(c.isalpha() for c in text)):
            right_to_left(paragraph)
        return paragraph

    directed(document.add_heading(title, level=0), title)
    for section in sections:
        if section["heading"]:
            directed(document.add_heading(section["heading"], level=1), section["heading"])
        for text in section["paragraphs"]:
            directed(document.add_paragraph(text), text)
        if section["columns"]:
            table = document.add_table(rows=1, cols=len(section["columns"]))
            table.style = "Table Grid"
            leftward = mostly_arabic(section["columns"] + [cell for row in section["rows"] for cell in row])
            if leftward:
                table_right_to_left(table)
            for cells, values in [(table.rows[0].cells, section["columns"])] + [
                    (table.add_row().cells, row) for row in section["rows"]]:
                for cell, value in zip(cells, values):
                    cell.text = value
                    if runs_right_to_left(value) or (leftward and not any(c.isalpha() for c in value)):
                        for paragraph in cell.paragraphs:
                            right_to_left(paragraph)
            for cell in table.rows[0].cells:
                for run in cell.paragraphs[0].runs:
                    run.bold = True
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def verify_docx(raw: bytes, texts: List[str]) -> int:
    """The Word file opened again: every text written is in it. The paragraphs with words, tables' cells included."""
    from docx import Document

    document = Document(io.BytesIO(raw))
    found = [p.text for p in document.paragraphs if p.text.strip()]
    cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    body = folded("\n".join(found + cells))
    lost = [t for t in texts if folded(t) not in body]
    if lost:
        raise ValueError(f"{len(lost)} passage(s) did not come back, the first: {lost[0][:80]}")
    return len(found)
