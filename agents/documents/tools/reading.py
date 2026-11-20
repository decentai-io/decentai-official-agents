"""Turning a file into numbered pages of text, and saying which pages could not be read.

A PDF is read page by page. A Word, text, Markdown or CSV file has no pages of its own, so it is cut into parts of
about ``PART`` characters at paragraph boundaries, and every part is reachable: nothing past a first page is lost. A
deck is read slide by slide, its tables, charts' numbers and speaker notes included. A picture (a photo, a scan) is
one page with no text layer.

A page with no text layer is ``unreadable`` here, by number; a page with a little text and a picture on it is
``scanned``, since its picture may hold more. Neither is ever dropped, and both can be read through the model's
picture input (``pictures.py``), which marks what it read as read from a picture.

Everything here is blocking (parsers, zip files), so the tools run it in ``asyncio.to_thread``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .common import Refused, check_size

MAX_PAGES = 2000
PART = 5000
#: Fewer letters than this on a PDF page that also carries a picture: the picture may be the page.
SCANNED_BELOW = 40
PICTURE_KINDS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif", ".tif": "tiff",
                 ".tiff": "tiff", ".bmp": "bmp", ".heic": "heic", ".heif": "heic"}
UNITS = {"pdf": "page", "docx": "part", "text": "part", "markdown": "part", "csv": "part", "pptx": "slide",
         "image": "picture"}


@dataclass
class Loaded:
    """One file, read: its kind, its pages of text (index 0 is page 1), and what could not be read."""

    file_ref: str
    filename: str
    kind: str
    pages: List[str] = field(default_factory=list)
    unreadable: List[int] = field(default_factory=list)
    scanned: List[int] = field(default_factory=list)
    headings: List[str] = field(default_factory=list)
    problem: str = ""
    encoding: str = ""
    raw: bytes = b""
    #: Pictures by page (a slide's pictures, a Word file's pictures as page 1's), counted, not decoded.
    pictures: dict = field(default_factory=dict)
    charts: List[int] = field(default_factory=list)
    notes: bool = False
    layouts: List[str] = field(default_factory=list)
    titles: List[str] = field(default_factory=list)

    @property
    def unit(self) -> str:
        return UNITS.get(self.kind, "page")

    @property
    def readable(self) -> List[int]:
        return [n for n in range(1, len(self.pages) + 1) if n not in self.unreadable]

    @property
    def characters(self) -> int:
        return sum(len(p) for p in self.pages)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def kind_of(filename: str, raw: bytes) -> str:
    name = (filename or "").lower()
    suffix = Path(name).suffix
    if raw[:5] == b"%PDF-" or suffix == ".pdf":
        return "pdf"
    if raw[:2] == b"PK":
        if suffix == ".pptx" or b"ppt/" in raw[:8000]:
            return "pptx"
        if suffix == ".docx" or b"word/" in raw[:8000]:
            return "docx"
        return "unsupported"
    if suffix in PICTURE_KINDS or raw[:4] in (b"\x89PNG", b"GIF8") or raw[:3] == b"\xff\xd8\xff" or (
            raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        return "image"
    if suffix == ".csv":
        return "csv"
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix in (".doc", ".ppt", ".xls", ".xlsx", ".odt", ".rtf", ".pages", ".key"):
        return "unsupported"
    return "text" if decoded(raw) is not None else "unsupported"


def decoded(raw: bytes):
    """(text, encoding) of a text file: UTF-8 with or without its mark, UTF-16 with its mark (or the zero bytes that
    give it away), else Windows Arabic (cp1256) or Western (cp1252), whichever reads as letters. None when it is no
    text."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace"), "utf-16"
    sample = raw[:4000]
    if sample and sample.count(b"\x00") > len(sample) // 4:
        odd = sample[1::2].count(b"\x00") > sample[0::2].count(b"\x00")
        return raw.decode("utf-16-le" if odd else "utf-16-be", "replace"), "utf-16"
    if b"\x00" in sample:
        return None
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    arabic = raw.decode("cp1256", "replace")
    western = raw.decode("cp1252", "replace")
    arabic_letters = len(re.findall(r"[ء-ي]", arabic))
    western_letters = len(re.findall(r"[a-zß-ÿ]", western)) - len(re.findall(r"[a-zA-Z]", western))
    return (arabic, "cp1256") if arabic_letters >= western_letters else (western, "cp1252")


async def load(call, resource_id: str, file_ref: str) -> Loaded:
    """A file handed to the call, or one of the resource's by its ref: its size checked before it is read, then
    parsed off the agent's loop."""
    import asyncio

    path = await call.file(file_ref, resource_id)
    check_size(path)
    raw = await asyncio.to_thread(path.read_bytes)
    return await asyncio.to_thread(parse, file_ref, path.name, raw)


def parse(file_ref: str, filename: str, raw: bytes) -> Loaded:
    kind = kind_of(filename, raw)
    loaded = Loaded(file_ref, filename, kind, raw=raw)
    if kind == "pdf":
        _pdf(loaded)
    elif kind == "docx":
        _docx(loaded)
    elif kind == "pptx":
        _pptx(loaded)
    elif kind == "image":
        loaded.pages, loaded.unreadable = [""], [1]
        loaded.pictures = {1: 1}
        if Path(filename.lower()).suffix in (".heic", ".heif"):
            loaded.problem = ("A HEIC photo can't be opened here: save it as JPEG or PNG (on an iPhone, Settings > "
                              "Camera > Formats > Most Compatible) and attach it again.")
    elif kind == "unsupported":
        loaded.problem = ("Not a PDF, Word (.docx), PowerPoint (.pptx), picture, text, Markdown or CSV file. Legacy "
                          ".doc and .ppt files are not read: save them as .docx or .pptx.")
    else:
        found = decoded(raw)
        if found is None:
            loaded.kind, loaded.problem = "unsupported", "The file is not text in any encoding read here."
        else:
            text, loaded.encoding = found
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if kind == "csv":
                text = _csv_text(text)
            loaded.pages = parts(text.split("\n"))
            loaded.unreadable = [] if text.strip() else [1]
    return loaded


def parts(lines: List[str], most: int = PART) -> List[str]:
    """Lines packed into parts of at most ``most`` characters, cut only between lines; a longer line is cut at a
    space near the limit. Every line lands in a part: there is always a next part until the end."""
    out, current, size = [], [], 0
    for line in lines:
        while len(line) > most:
            cut = line.rfind(" ", most // 2, most)
            cut = cut if cut > 0 else most
            if current:
                out.append("\n".join(current))
                current, size = [], 0
            out.append(line[:cut])
            line = line[cut:].lstrip()
        if size + len(line) + 1 > most and current:
            out.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current and any(c.strip() for c in current):
        out.append("\n".join(current))
    out = [part.strip("\n") for part in out]
    return out or [""]


# -- PDF -----------------------------------------------------------------------------------------------------------

def _pdf(loaded: Loaded) -> None:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(loaded.raw))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception as failed:
                loaded.problem = f"The PDF is encrypted in a way this reader can't open ({failed})."
                return
            if not opened:
                loaded.problem = ("The PDF is password-protected. Ask for a copy without a password (or print it to "
                                  "a new PDF) and attach that.")
                return
        count = len(reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as failed:
        loaded.problem = f"The PDF could not be opened: {failed}"
        return
    if count > MAX_PAGES:
        loaded.problem = (f"The PDF has {count} pages; at most {MAX_PAGES} are read here. Split it (pdf_split) and "
                          f"read the parts.")
        return
    for number, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        letters = len(re.findall(r"\w", text))
        if not letters:
            loaded.unreadable.append(number)
            text = ""
        elif letters < SCANNED_BELOW and _has_picture(page):
            loaded.scanned.append(number)
        loaded.pages.append(text)


def _has_picture(page) -> bool:
    try:
        resources = page.get("/Resources")
        resources = resources.get_object() if resources is not None else {}
        objects = resources.get("/XObject")
        objects = objects.get_object() if objects is not None else {}
        return any(objects[name].get_object().get("/Subtype") in ("/Image", "/Form") for name in objects)
    except Exception:
        return False


# -- Word ----------------------------------------------------------------------------------------------------------

def _docx(loaded: Loaded) -> None:
    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(io.BytesIO(loaded.raw))
    except (PackageNotFoundError, KeyError, ValueError, OSError) as failed:
        loaded.problem = f"The Word file could not be opened: {failed}"
        return
    lines: List[str] = []

    def table_lines(table) -> List[str]:
        found = []
        for row in table.rows:
            cells, seen = [], set()
            for cell in row.cells:            # a merged cell repeats across the row: once is enough
                if id(cell._tc) in seen:
                    continue
                seen.add(id(cell._tc))
                cells.append(" ".join(cell.text.split()))
            if any(cells):
                found.append(" | ".join(cells))
        return found

    # The body in its own order: a table stands where it stands, not after every paragraph.
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = str(paragraph.style.name if paragraph.style is not None else "")
            if style.startswith(("Heading", "Title")):
                loaded.headings.append(text)
            lines.append(text)
        elif tag == "tbl":
            lines += table_lines(Table(child, document))

    margins = {"header": [], "footer": []}
    for section in document.sections:
        for side in ("header", "footer"):
            try:
                part = getattr(section, side)
                if part.is_linked_to_previous and margins[side]:
                    continue
                found = [p.text.strip() for p in part.paragraphs if p.text.strip()]
                for table in part.tables:
                    found += table_lines(table)
            except Exception:
                found = []
            margins[side] += [line for line in found if line not in margins[side]]
    lines = [f"[header] {line}" for line in margins["header"]] + lines + [
        f"[footer] {line}" for line in margins["footer"]]
    loaded.pages = parts(lines)
    loaded.unreadable = [] if any(p.strip() for p in loaded.pages) else [1]
    count = len(docx_pictures(document))
    if count:
        loaded.pictures = {1: count}


def docx_pictures(document) -> List[bytes]:
    """The pictures a Word file shows, in the order they stand."""
    found = []
    for blip in document.element.body.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
        rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        part = document.part.related_parts.get(rid) if rid else None
        if part is not None and str(getattr(part, "content_type", "")).startswith("image/"):
            found.append(part.blob)
    return found


# -- CSV -----------------------------------------------------------------------------------------------------------

def _csv_text(text: str) -> str:
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(io.StringIO(text), dialect)
    return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows if any(c.strip() for c in row))


# -- PowerPoint ----------------------------------------------------------------------------------------------------

def _pptx(loaded: Loaded) -> None:
    from pptx import Presentation

    try:
        presentation = Presentation(io.BytesIO(loaded.raw))
    except Exception as failed:
        loaded.problem = f"The deck could not be opened: {failed}"
        return
    count = len(presentation.slides)
    if count > MAX_PAGES:
        loaded.problem = f"The deck has {count} slides; at most {MAX_PAGES} are read here."
        return
    for number, slide in enumerate(presentation.slides, 1):
        title_shape = None
        try:
            title_shape = slide.shapes.title
        except Exception:
            pass
        title = clean(title_shape.text) if title_shape is not None else ""
        lines, pictures, charts = [], [0], [0]
        for shape in slide.shapes:
            if title_shape is not None and shape.shape_id == title_shape.shape_id:
                continue
            lines += shape_lines(shape, pictures, charts)
        notes = ""
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = clean(slide.notes_slide.notes_text_frame.text)
        except Exception:
            pass
        try:
            layout = str(slide.slide_layout.name or "")
        except Exception:
            layout = ""
        if layout and layout not in loaded.layouts:
            loaded.layouts.append(layout)
        text = "\n".join(([f"# {title}"] if title else []) + lines + ([f"[notes] {notes}"] if notes else []))
        loaded.pages.append(text)
        loaded.titles.append(title)
        loaded.notes = loaded.notes or bool(notes)
        if not text.strip():
            loaded.unreadable.append(number)
        if pictures[0]:
            loaded.pictures[number] = pictures[0]
        if charts[0]:
            loaded.charts.append(number)


def shape_lines(shape, pictures=None, charts=None) -> List[str]:
    """Every line a shape carries: its paragraphs, its table's rows, its chart's numbers, the shapes inside a group;
    pictures are counted, for the model to read."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    pictures = pictures if pictures is not None else [0]
    charts = charts if charts is not None else [0]
    lines: List[str] = []
    try:
        kind = shape.shape_type
    except Exception:
        kind = None
    if kind == MSO_SHAPE_TYPE.GROUP:
        for inner in shape.shapes:
            lines += shape_lines(inner, pictures, charts)
        return lines
    if kind == MSO_SHAPE_TYPE.PICTURE or (getattr(shape, "is_placeholder", False) and hasattr(shape, "image")):
        pictures[0] += 1
    if getattr(shape, "has_chart", False):
        charts[0] += 1
        lines += chart_lines(shape.chart)
        return lines
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            cells = [clean(cell.text).replace("\n", " ") for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
        return lines
    if getattr(shape, "has_text_frame", False):
        for paragraph in shape.text_frame.paragraphs:
            text = clean("".join(run.text for run in paragraph.runs))
            if text:
                lines.append(text)
    return lines


def chart_lines(chart) -> List[str]:
    """A chart's own numbers, as the file stores them: its title, its categories, and each series' values. Read
    from the file, not from the picture, so they are exact."""
    lines = []
    try:
        title = clean(chart.chart_title.text_frame.text) if chart.has_title else ""
    except Exception:
        title = ""
    try:
        kind = str(chart.chart_type).split(".")[-1].split(" ")[0].lower()
    except Exception:
        kind = "chart"
    lines.append(f"[chart{': ' + title if title else ''}] ({kind})")
    for plot in chart.plots:
        try:
            categories = [clean(str(c)) for c in plot.categories]
        except Exception:
            categories = []
        if categories:
            lines.append("categories: " + " | ".join(categories))
        for series in plot.series:
            try:
                values = ["" if v is None else format(v, "g") for v in series.values]
            except Exception:
                values = []
            lines.append(f"{clean(series.name) or 'series'}: " + " | ".join(values))
    return lines


def clean(text) -> str:
    """A vertical tab is PowerPoint's soft line break."""
    return str(text or "").replace("\v", "\n").strip()


def paragraphs_of(loaded: Loaded):
    """(page, line) pairs, the unit versions are compared by, spacing normalised."""
    out = []
    for number, page in enumerate(loaded.pages, 1):
        for line in page.splitlines():
            line = " ".join(line.split())
            if line:
                out.append((number, line))
    return out


def ranges(text: str, count: int) -> List[int]:
    """Page numbers from "1-3, 5, 8-": each within 1..count, or refused with the numbers."""
    found: List[int] = []
    for piece in re.split(r"[,\s]+", str(text or "").strip()):
        if not piece:
            continue
        match = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?|(\d+)", piece)
        if not match:
            raise Refused(f"'{piece}' is not a page or a range of pages (write 1-3, 5).")
        if match.group(3):
            first = last = int(match.group(3))
        else:
            first, last = int(match.group(1) or 1), int(match.group(2) or count)
        if not 1 <= first <= last <= count:
            raise Refused(f"Pages {piece} are outside the file, which has {count} page(s).")
        found += [n for n in range(first, last + 1) if n not in found]
    if not found:
        raise Refused("No pages were named.")
    return found
