"""Reading what only a picture holds, through the model's picture input: a scanned page, a photo, a slide's
pictures, a Word file's pictures.

A PDF page is rendered to an image with pdfium. Every picture goes to the model as a JPEG or PNG of at most
``SIDE`` pixels on its longer side and under 5 MiB, one picture per model call, so what it read is tied to its page.
What came back is kept for this chat's process, by the file's digest, so a page is read once.

What the model read off a picture is never a quote from the file's text: it is marked ``from_picture`` wherever it
is shown, and a refusal or a reading cut short is said, never passed off as the page being blank.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .common import Refused

SIDE = 2000
PICTURE_MOST = 5 * 1024 * 1024 - 64 * 1024
AT_ONCE = 4
NO_TEXT = "[no text]"

TRANSCRIBE = (
    "You copy the text you see in the picture exactly as written: in its own language and script (Arabic stays "
    "Arabic, and digits stay the digits written, Arabic-Indic included), in reading order, one line per line, a "
    "table as rows with ' | ' between cells. Never translate, summarise, correct, complete or add anything. Write "
    "[illegible] for a word you cannot read. If the picture holds no text, answer exactly: [no text]")
DESCRIBE = (
    "You describe what the picture shows, briefly and plainly, in the language of any text it carries. For a chart "
    "or a table give its title, axes or columns, and only the values that are written on it, as written; say a "
    "value is unlabelled rather than estimate it. Never guess at what is not visible.")


@dataclass
class Reading:
    """What the model read off one picture: its text, or why there is none."""

    text: str = ""
    refused: bool = False
    cut: bool = False
    problem: str = ""

    @property
    def empty(self) -> bool:
        return not self.text.strip() or self.text.strip() == NO_TEXT


#: What was read in this chat's process: (file digest, mode, page, picture) -> Reading.
READ: Dict[Tuple[str, str, int, int], Reading] = {}


def prepared(raw: bytes) -> bytes:
    """A picture as the model takes it: upright, at most SIDE pixels, a JPEG (a PNG when it is line art with
    transparency) under 5 MiB."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((SIDE, SIDE))
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white")
                rgba = image.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            for quality in (85, 70, 55):
                out = io.BytesIO()
                image.save(out, "JPEG", quality=quality, optimize=True)
                if out.tell() <= PICTURE_MOST:
                    return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as failed:
        raise Refused(f"The picture could not be opened ({failed}). Save it as JPEG or PNG and attach it again.")
    raise Refused("The picture stays over 5 MB however it is compressed.")


def pdf_page_picture(raw: bytes, number: int) -> bytes:
    """One PDF page rendered to a picture, at about 200 dots per inch but never over SIDE pixels."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(raw)
    try:
        page = document[number - 1]
        width, height = page.get_size()
        scale = min(200 / 72, SIDE / max(width, height, 1))
        image = page.render(scale=scale).to_pil()
        out = io.BytesIO()
        image.convert("RGB").save(out, "PNG", optimize=True)
        return prepared(out.getvalue())
    finally:
        document.close()


def pictures_of(loaded, number: int) -> List[Tuple[str, bytes]]:
    """The pictures one page stands for, as (label, prepared bytes); a picture in a format no model takes (a
    Windows metafile, say) is named with no bytes."""
    if loaded.kind == "pdf":
        return [(f"page {number}", pdf_page_picture(loaded.raw, number))]
    if loaded.kind == "image":
        return [(loaded.filename, prepared(loaded.raw))]
    found: List[Tuple[str, bytes]] = []
    if loaded.kind == "pptx":
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        slide = Presentation(io.BytesIO(loaded.raw)).slides[number - 1]

        def walk(shapes):
            for shape in shapes:
                try:
                    kind = shape.shape_type
                except Exception:
                    kind = None
                if kind == MSO_SHAPE_TYPE.GROUP:
                    walk(shape.shapes)
                    continue
                try:
                    image = shape.image
                except Exception:
                    continue
                label = f"slide {number}, picture {len(found) + 1}"
                found.append((label, _or_empty(image.blob)))
        walk(slide.shapes)
    elif loaded.kind == "docx":
        from docx import Document

        from .reading import docx_pictures

        for index, blob in enumerate(docx_pictures(Document(io.BytesIO(loaded.raw))), 1):
            found.append((f"picture {index}", _or_empty(blob)))
    return found


def _or_empty(blob: bytes) -> bytes:
    try:
        return prepared(blob)
    except Refused:
        return b""


async def read(call, loaded, numbers: List[int], mode: str = "transcribe") -> Dict[int, List[Tuple[str, Reading]]]:
    """Each page's pictures read by the model, at most AT_ONCE at a time: page -> [(label, Reading)]."""
    gate = asyncio.Semaphore(AT_ONCE)
    digest = loaded.digest

    async def one(number: int) -> Tuple[int, List[Tuple[str, Reading]]]:
        try:
            found = await asyncio.to_thread(pictures_of, loaded, number)
        except Refused as refused:
            return number, [(f"{loaded.unit} {number}", Reading(problem=str(refused)))]
        except Exception as failed:
            return number, [(f"{loaded.unit} {number}", Reading(problem=f"It could not be turned into a picture: "
                                                                        f"{failed}"))]
        readings = []
        for index, (label, picture) in enumerate(found, 1):
            key = (digest, mode, number, index)
            reading = READ.get(key)
            if reading is None and not picture:
                reading = Reading(problem="This picture is in a format the model can't take (a Windows metafile, "
                                          "say).")
            elif reading is None:
                async with gate:
                    reading = await _asked(call, picture, mode, label)
                if reading.text and not reading.cut:     # only a whole reading is kept; a failure is asked again
                    READ[key] = reading
            readings.append((label, reading))
        return number, readings

    return dict(await asyncio.gather(*(one(number) for number in numbers)))


async def _asked(call, picture: bytes, mode: str, label: str) -> Reading:
    prompt = ("Copy the text of this picture." if mode == "transcribe" else "Describe this picture.") + f" ({label})"
    try:
        said = await call.model(prompt, system=TRANSCRIBE if mode == "transcribe" else DESCRIBE,
                                max_tokens=8000, pictures=[picture])
    except Exception as failed:
        return Reading(problem=f"The model could not be asked: {failed}")
    if getattr(said, "refused", False):
        return Reading(refused=True, problem="The model refused to read this picture.")
    return Reading(text=str(said).strip(), cut=bool(getattr(said, "cut", False)),
                   problem="The model's reading was cut short: the end of this picture is missing."
                   if getattr(said, "cut", False) else "")


def cached(loaded, number: int, mode: str = "transcribe") -> List[Tuple[str, Reading]]:
    """What was already read off a page's pictures in this chat, if anything."""
    digest, found, index = loaded.digest, [], 1
    while (digest, mode, number, index) in READ:
        found.append((f"{loaded.unit} {number}", READ[(digest, mode, number, index)]))
        index += 1
    return found
