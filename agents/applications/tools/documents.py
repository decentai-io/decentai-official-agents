"""A document attached to a step: checked in code for what code can
check — type, size, pages — and, where the form says what to read from
it, read by the model: a PDF by its text, a photo by its pixels. What
the model read is held to the text where there is text."""

from __future__ import annotations

import io
import json
import mimetypes
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TEXT_SHOWN = 12000
FILE_MOST = 8 * 1024 * 1024 - 64 * 1024
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
IMAGE_TYPES = ("image/png", "image/jpeg")


class Inspected:
    def __init__(self, filename: str, mime: str, raw: bytes,
                 text: str = "", pages: Optional[int] = None):
        self.filename = filename
        self.mime = mime
        self.raw = raw
        self.size = len(raw)
        self.text = text
        self.pages = pages

    @property
    def is_image(self) -> bool:
        return self.mime in IMAGE_TYPES


def inspect(path: Path, step: Dict[str, Any]) -> Tuple[Optional[Inspected], List[str]]:
    """The file as this step may take it, or the problems. Its name says
    what it is, or nothing. A PDF's text is extracted here; an image
    carries none."""
    filename = path.name
    mime = mimetypes.guess_type(filename)[0] or ""
    raw = path.read_bytes()
    size = len(raw)
    problems: List[str] = []
    accept = [str(a) for a in step.get("accept") or []]
    if mime not in accept:
        problems.append(f"{filename} is {mime or 'of no known type'}; this step takes "
                        + ", ".join(accept) + ".")
    # A chat file is at most 8 MiB less 64 KiB: no step takes more.
    limit = min(float(step.get("max_mb") or 8), FILE_MOST / (1024 * 1024))
    if size > limit * 1024 * 1024:
        problems.append(f"{filename} is {size:,} bytes; this step takes at most "
                        f"{limit:.2f} MB.")
    if not raw:
        problems.append(f"{filename} is empty.")
    if problems:
        return None, problems
    text, pages = "", None
    if mime == "application/pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            pages = len(reader.pages)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            return None, [f"{filename} could not be opened as a PDF: {exc}"]
        if pages and int(step.get("max_pages") or 0) and pages > int(step["max_pages"]):
            return None, [f"{filename} has {pages} pages; this step takes at most "
                          f"{step['max_pages']}."]
    return Inspected(filename, mime, raw, text, pages), []


async def read_fields(call, step: Dict[str, Any], document: Inspected
                      ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """What the form asked to read from the document, as the model read
    it: ``{field: {value, status}}`` where status is ``verified`` (the
    text carries the value), ``assumed`` (a reading the text could not
    confirm, or a photo), or ``missing``. A photo is shown to the model; a
    scan with no text layer cannot be, since the model is shown pictures
    only, and is refused with what to attach instead."""
    wanted: Dict[str, str] = dict(step.get("read") or {})
    if not wanted:
        return {}, []
    if not document.is_image and not document.text.strip():
        return {}, [f"{document.filename} has no text to read (a scan), and a PDF is not shown "
                    f"to the model as a picture: attach a photo of it, or a PDF with text."]
    keys = json.dumps(wanted, ensure_ascii=False)
    ask = (f"QUESTIONS\nAnswer as JSON with exactly these keys — the value is the "
           f"text as it appears in the document, or \"\" when it is not there. "
           f"Dates as YYYY-MM-DD. {keys}")
    try:
        if document.is_image:
            prompt = f"DOCUMENT ({document.filename}) — a picture.\n{ask}"
            answer = await call.model(prompt, pictures=[document.raw])
            by_text = False
        else:
            prompt = (f"DOCUMENT ({document.filename})\n"
                      f"{document.text[:TEXT_SHOWN]}\n{ask}")
            answer = await call.model(prompt)
            by_text = True
    except Exception as exc:
        return {}, [f"The document could not be read by the model: {exc}"]
    parsed = _json_object(answer)
    read: Dict[str, Dict[str, Any]] = {}
    for field in wanted:
        value = str(parsed.get(field) or "").strip()
        if not value:
            read[field] = {"value": "", "status": "missing"}
        elif by_text and (_in_text(value, document.text) or _date_in_text(value, document.text)):
            read[field] = {"value": value, "status": "verified"}
        else:
            read[field] = {"value": value, "status": "assumed"}
    return read, []


def _json_object(text: str) -> Dict[str, Any]:
    text = str(text or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _norm(text: str) -> str:
    return re.sub(r"[\s\-/.:]", "", str(text or "")).lower()


def _in_text(value: str, text: str) -> bool:
    return bool(_norm(value)) and _norm(value) in _norm(text)


def _date_in_text(value: str, text: str) -> bool:
    """A date the model gave as YYYY-MM-DD, found on the page as the page
    writes it: 2031-05-01, 01/05/2031, 01.05.2031, 1 May 2031, 01 MAY 2031,
    1 May, 2031 or May 1, 2031. A numeric date is read day first, as
    passports and the Gulf write it; one written month first (05/01/2031)
    does not verify, and the person is asked instead."""
    try:
        when = date.fromisoformat(str(value).strip())
    except ValueError:
        return False
    month = MONTHS[when.month - 1]
    forms = {f"{when:%Y%m%d}", f"{when:%d%m%Y}", f"{when.day}{month}{when.year}", f"{when:%d}{month}{when.year}",
             f"{when.day}{month[:3]}{when.year}", f"{when:%d}{month[:3]}{when.year}",
             f"{month}{when.day},{when.year}", f"{month[:3]}{when.day},{when.year}",
             f"{when:%d}{month[:3]}{when:%y}"}
    page = _norm(text)
    return any(re.search(rf"(?<![0-9]){re.escape(form)}(?![0-9])", page) for form in forms)
