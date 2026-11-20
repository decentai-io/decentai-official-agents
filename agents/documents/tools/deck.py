"""PowerPoint decks built from an outline, 16:9 unless a template says otherwise, laid out in code so nothing
overlaps: every slide's title, bullets and table get boxes measured from their text, a table too long for its slide
goes on to the next one with its header row repeated, and bullets that don't fit at a readable size do the same.

With a template, the deck is built on that file: its size, theme, fonts and layouts; its own slides are removed
first, since a template is a design, not content. Arabic bullets and cells run right to left, and an Arabic table's
columns from the right.

Text size is estimated (half an em a character, 1.2 lines' height), not rendered: the read-back checks the boxes,
not the glyphs.
"""

from __future__ import annotations

import io
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .common import Refused, mostly_arabic, runs_right_to_left

EMU = 914400                      # one inch
PT = 12700                        # one point
TITLE_LAYOUTS = ("title slide", "title")
CONTENT_LAYOUTS = ("title and content", "title, content", "content with caption", "title and body")
BARE_LAYOUTS = ("title only", "blank")
PLACEHOLDER = re.compile(r"\{\{\s*([\w.\- ]+?)\s*\}\}", re.UNICODE)


def outline_of(inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for number, slide in enumerate(inputs["slides"], 1):
        columns = [str(c) for c in slide.get("table_columns") or []]
        rows = [[str(c) for c in row] if isinstance(row, (list, tuple)) else [str(row)]
                for row in slide.get("table_rows") or []]
        if rows and not columns:
            raise Refused(f"Slide {number}: a table needs table_columns as well as table_rows.")
        if any(len(r) != len(columns) for r in rows):
            raise Refused(f"Slide {number}: every table row needs one cell per column ({len(columns)}).")
        out.append({"title": str(slide.get("title") or ""), "bullets": [str(b) for b in slide.get("bullets") or []],
                    "notes": str(slide.get("notes") or ""), "columns": columns, "rows": rows})
    return out


def lines(text: str, width: int, size: float) -> int:
    """How many lines a text takes in a box ``width`` EMU wide at ``size`` points: half an em a character."""
    per_line = max(1, int(width / (size * PT * 0.52)))
    return sum(max(1, math.ceil(len(part) / per_line)) for part in str(text).split("\n"))


def text_height(texts: List[str], width: int, size: float, gap: float = 0.35) -> int:
    inner = width - int(0.2 * EMU)
    total = sum(lines(t, inner, size) for t in texts) * size * 1.2 + len(texts) * size * gap
    return int(total * PT + 0.15 * EMU)


class Builder:
    def __init__(self, template: Optional[bytes], arabic: bool):
        from pptx import Presentation

        self.presentation = Presentation(io.BytesIO(template)) if template else Presentation()
        self.arabic = arabic
        if template:
            self.clear()
        else:
            self.widen(13.333)
        self.width = int(self.presentation.slide_width)
        self.height = int(self.presentation.slide_height)
        self.margin = int(0.5 * EMU)
        self.continued: List[int] = []

    # -- the deck's frame ----------------------------------------------------------------------------------------

    def widen(self, inches: float) -> None:
        """The default 4:3 deck made 16:9: the slide widened, and every placed shape of its masters and layouts
        stretched to match, so the placeholders span the new width."""
        presentation = self.presentation
        factor = inches * EMU / presentation.slide_width
        presentation.slide_width = int(inches * EMU)
        for master in presentation.slide_masters:
            for shapes in [master.shapes] + [layout.shapes for layout in master.slide_layouts]:
                for shape in shapes:
                    xfrm = shape._element.xpath("./p:spPr/a:xfrm")
                    if xfrm and shape.left is not None and shape.width is not None:
                        shape.left, shape.width = int(shape.left * factor), int(shape.width * factor)

    def clear(self) -> None:
        slides = self.presentation.slides._sldIdLst
        for element in list(slides):
            try:
                self.presentation.part.drop_rel(element.rId)
            except Exception:
                pass
            slides.remove(element)

    def layout(self, names, fallback: int):
        layouts = self.presentation.slide_layouts
        for wanted in names:
            for layout in layouts:
                if str(layout.name or "").strip().lower() == wanted:
                    return layout
        for wanted in names:
            for layout in layouts:
                if wanted in str(layout.name or "").strip().lower():
                    return layout
        return layouts[fallback] if fallback < len(layouts) else layouts[0]

    # -- text ----------------------------------------------------------------------------------------------------

    def directed(self, paragraph, text: str) -> None:
        from pptx.enum.text import PP_ALIGN

        if runs_right_to_left(text) or (self.arabic and not any(c.isalpha() for c in text)):
            paragraph._p.get_or_add_pPr().set("rtl", "1")
            paragraph.alignment = PP_ALIGN.RIGHT

    def fill(self, frame, texts: List[str], size: float, bold: bool = False) -> None:
        from pptx.util import Pt

        frame.clear()
        frame.word_wrap = True
        for index, text in enumerate(texts):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = text
            for run in paragraph.runs:
                run.font.size = Pt(size)
                run.font.bold = bold or None
            self.directed(paragraph, text)

    # -- slides --------------------------------------------------------------------------------------------------

    def title_slide(self, title: str, subtitle: str) -> None:
        slide = self.presentation.slides.add_slide(self.layout(TITLE_LAYOUTS, 0))
        used = []
        if slide.shapes.title is not None:
            self.fill(slide.shapes.title.text_frame, [title], 40 if len(title) < 40 else 32)
            used.append(slide.shapes.title)
        body = self.body(slide)
        if body is not None and subtitle:
            self.fill(body.text_frame, [subtitle], 20)
            used.append(body)
        if slide.shapes.title is None:
            self.box(slide, [title], self.margin, int(self.height * 0.35), self.width - 2 * self.margin,
                     int(1.4 * EMU), 36)
        self.tidy(slide, used)

    def body(self, slide):
        title = slide.shapes.title
        for placeholder in slide.placeholders:
            if title is not None and placeholder.shape_id == title.shape_id:
                continue
            if placeholder.has_text_frame and placeholder.placeholder_format.type not in (13, 15, 16):
                return placeholder      # not a date, footer or slide number
        return None

    def tidy(self, slide, used) -> None:
        """Placeholders the slide didn't use are removed, so none shows its prompt text or covers another shape."""
        kept = {shape.shape_id for shape in used}
        for placeholder in list(slide.placeholders):
            if placeholder.shape_id not in kept:
                placeholder._element.getparent().remove(placeholder._element)

    def box(self, slide, texts, left, top, width, height, size):
        shape = slide.shapes.add_textbox(left, top, width, height)
        self.fill(shape.text_frame, texts, size)
        return shape

    def frame(self, slide) -> Tuple[int, int, int, int, Any]:
        """Where the title stands, and the region under it for content: (left, top, width, bottom, title)."""
        title = slide.shapes.title
        if title is None:
            title = slide.shapes.add_textbox(self.margin, int(0.3 * EMU), self.width - 2 * self.margin,
                                             int(1.0 * EMU))
        if title.left is None or title.top is None or title.width is None or title.height is None or (
                title.top + title.height > self.height * 0.4):
            title.left, title.top = self.margin, int(0.3 * EMU)
            title.width, title.height = self.width - 2 * self.margin, int(1.0 * EMU)
        left = max(int(title.left), self.margin // 2)
        width = min(int(title.width), self.width - 2 * left)
        top = int(title.top + title.height + 0.15 * EMU)
        bottom = int(self.height - 0.45 * EMU)
        return left, top, width, bottom, title

    def content_slide(self, entry: Dict[str, Any], title: str, bullets: List[str],
                      rows: Optional[List[List[str]]]) -> Tuple[List[str], List[List[str]]]:
        """One slide of the entry: as many of its bullets and table rows as fit; what didn't fit is returned, for
        the next slide."""
        slide = self.presentation.slides.add_slide(
            self.layout(CONTENT_LAYOUTS, 1) if bullets else self.layout(BARE_LAYOUTS, 5))
        left, top, width, bottom, title_shape = self.frame(slide)
        used = [title_shape] if title_shape.is_placeholder else []
        if title:
            size = 30 if lines(title, title_shape.width, 30) == 1 else 24
            self.fill(title_shape.text_frame, [title], size)
        room = bottom - top
        left_over: List[str] = []
        if bullets:
            share = room if not rows else int(room * 0.5)
            size, fitted = self.fit(bullets, width, share)
            left_over, bullets = bullets[fitted:], bullets[:fitted]
            height = min(text_height(bullets, width, size), share)
            body = self.body(slide)
            if body is not None:
                body.left, body.top, body.width, body.height = left, top, width, height
                self.fill(body.text_frame, bullets, size)
                used.append(body)
            else:
                self.box(slide, bullets, left, top, width, height, size)
            top += height + int(0.2 * EMU)
            room = bottom - top
        rest_rows: List[List[str]] = []
        if rows is not None and entry["columns"]:
            widths = self.widths(entry["columns"], rows, width)
            header = self.row_height(entry["columns"], widths)
            heights = [self.row_height(row, widths) for row in rows]
            fit, used_height = 0, header
            while fit < len(rows) and used_height + heights[fit] <= room:
                used_height += heights[fit]
                fit += 1
            if fit == 0 and not bullets and not left_over:
                fit, used_height = 1, header + heights[0]     # one row taller than a slide: it still goes somewhere
            if (fit < min(3, len(rows)) and bullets) or left_over:
                fit = 0                                         # too little room under the bullets: next slide
            if fit:
                self.table(slide, entry["columns"], rows[:fit], widths, [header] + heights[:fit], left, top)
            rest_rows = rows[fit:]
        if entry["notes"]:
            slide.notes_slide.notes_text_frame.text = entry["notes"]
        self.tidy(slide, used)
        return left_over, rest_rows

    def fit(self, bullets: List[str], width: int, room: int) -> Tuple[float, int]:
        """The largest size from 24 to 14 points at which the bullets fit; at 14, as many as fit (at least one)."""
        for size in (24, 22, 20, 18, 16, 14):
            if text_height(bullets, width, size) <= room:
                return size, len(bullets)
        fitted = 1
        while fitted < len(bullets) and text_height(bullets[:fitted + 1], width, 14) <= room:
            fitted += 1
        return 14, fitted

    def widths(self, columns: List[str], rows: List[List[str]], width: int) -> List[int]:
        longest = [max([len(columns[i])] + [len(r[i]) for r in rows]) for i in range(len(columns))]
        weights = [min(max(n, 4), 40) for n in longest]
        total = sum(weights)
        widths = [max(int(width * w / total), int(0.7 * EMU)) for w in weights]
        scale = width / sum(widths)
        return [int(w * scale) for w in widths]

    @staticmethod
    def row_height(cells: List[str], widths: List[int], size: float = 12) -> int:
        tallest = max(lines(cell, w - int(0.2 * EMU), size) for cell, w in zip(cells, widths))
        return int(tallest * size * 1.2 * PT + 0.12 * EMU)

    def table(self, slide, columns, rows, widths, heights, left, top) -> None:
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Pt

        shape = slide.shapes.add_table(len(rows) + 1, len(columns), left, top, sum(widths), sum(heights))
        table = shape.table
        leftward = mostly_arabic(columns + [cell for row in rows for cell in row])
        if leftward:
            table._tbl.tblPr.set("rtl", "1")
        for index, width in enumerate(widths):
            table.columns[index].width = width
        for index, height in enumerate(heights):
            table.rows[index].height = height
        for r, values in enumerate([columns, *rows]):
            for c, value in enumerate(values):
                cell = table.cell(r, c)
                cell.text = value
                for paragraph in cell.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(12)
                        run.font.bold = True if r == 0 else None
                    if runs_right_to_left(value) or (leftward and not any(c.isalpha() for c in value)):
                        paragraph._p.get_or_add_pPr().set("rtl", "1")
                        paragraph.alignment = PP_ALIGN.RIGHT

    def add(self, entry: Dict[str, Any]) -> None:
        bullets, rows = entry["bullets"], entry["rows"] if entry["columns"] else None
        title, part = entry["title"], 1
        while True:
            bullets, rest = self.content_slide(entry, title, bullets, rows)
            if not bullets and not rest:
                break
            rows = rest if entry["columns"] else None
            part += 1
            title = f"{entry['title']} ({'تابع' if self.arabic else 'continued'})" if entry["title"] else ""
            self.continued.append(len(self.presentation.slides) + 1)
            entry = {**entry, "notes": ""}
            if part > 40:
                raise Refused(f"'{entry['title']}' would take more than 40 slides: split it.")

    def saved(self) -> bytes:
        buffer = io.BytesIO()
        self.presentation.save(buffer)
        return buffer.getvalue()


def build_pptx(title: str, subtitle: str, outline: List[Dict[str, Any]], template: Optional[bytes]) -> Tuple[bytes,
                                                                                                           List[int]]:
    texts = [title, subtitle] + [t for e in outline for t in [e["title"], *e["bullets"], *e["columns"],
                                                               *[c for r in e["rows"] for c in r]]]
    builder = Builder(template, mostly_arabic(texts))
    builder.title_slide(title, subtitle)
    for entry in outline:
        builder.add(entry)
    return builder.saved(), builder.continued


def verify_pptx(raw: bytes) -> Tuple[int, List[str]]:
    """The deck opened again: slides with words on them, and on every slide no two shapes overlapping and none off
    the slide. (slides, problems)."""
    from pptx import Presentation

    from .reading import shape_lines

    presentation = Presentation(io.BytesIO(raw))
    width, height = presentation.slide_width, presentation.slide_height
    count, words, problems = len(presentation.slides), 0, []
    if not count:
        raise ValueError("the deck came back with no slides")
    slack = int(0.02 * EMU)
    for number, slide in enumerate(presentation.slides, 1):
        boxes = []
        for shape in slide.shapes:
            words += sum(len(line) for line in shape_lines(shape))
            if None in (shape.left, shape.top, shape.width, shape.height):
                continue
            box = (shape.left, shape.top, shape.left + shape.width, shape.top + shape.height)
            if box[0] < -slack or box[1] < -slack or box[2] > width + slack or box[3] > height + slack:
                problems.append(f"slide {number}: '{shape.name}' runs off the slide")
            for other_name, other in boxes:
                if box[0] < other[2] - slack and other[0] < box[2] - slack and box[1] < other[3] - slack and \
                        other[1] < box[3] - slack:
                    problems.append(f"slide {number}: '{shape.name}' overlaps '{other_name}'")
            boxes.append((shape.name, box))
    if not words:
        raise ValueError("no text came back from the deck")
    return count, problems


# -- templates with {{placeholders}} ---------------------------------------------------------------------------

def fill_pptx(raw: bytes, values: Dict[str, str]) -> Tuple[bytes, set, set]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    presentation = Presentation(io.BytesIO(raw))
    filled, unfilled = set(), set()

    def frame(text_frame):
        for paragraph in text_frame.paragraphs:
            runs = paragraph.runs
            text = "".join(run.text for run in runs)
            names = PLACEHOLDER.findall(text)
            if not names:
                continue
            for name in names:
                (filled if name in values else unfilled).add(name)
            new = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)
            if new != text and runs:
                runs[0].text = new
                for run in runs[1:]:
                    run.text = ""
                if runs_right_to_left(new):
                    paragraph._p.get_or_add_pPr().set("rtl", "1")

    def shape(item):
        try:
            if item.shape_type == MSO_SHAPE_TYPE.GROUP:
                for inner in item.shapes:
                    shape(inner)
                return
        except Exception:
            pass
        if getattr(item, "has_table", False):
            for row in item.table.rows:
                for cell in row.cells:
                    frame(cell.text_frame)
        elif getattr(item, "has_text_frame", False):
            frame(item.text_frame)

    for slide in presentation.slides:
        for item in slide.shapes:
            shape(item)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            frame(slide.notes_slide.notes_text_frame)
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue(), filled, unfilled
