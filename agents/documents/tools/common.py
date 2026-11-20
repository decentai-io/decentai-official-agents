"""What every tool of this agent shares: the file size rule, names for produced files, the output record, how text is
compared when a quote is checked, and which way a text runs."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from standard.http import FILE_MOST, human_size

#: Arabic-Indic and Persian digits, read as the digits they are.
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
#: Arabic letters (the main block, its supplement and extensions, and the presentation forms a PDF may carry).
ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
#: Hebrew, Syriac, Thaana and N'Ko also run right to left.
RIGHT_TO_LEFT = re.compile(r"[֐-׿܀-޿߀-߿]|" + ARABIC.pattern)
LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
#: Marks that change nothing a reader checks: tatweel, Arabic diacritics, zero-width joiners and direction marks.
QUIET = re.compile(r"[ـً-ٰٟۖ-ۭ​-‏‪-‮⁦-⁩﻿]")


class Refused(Exception):
    """A rule refused the call: its words, and the kind of refusal (``too_large``, ``invalid``, …)."""

    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind

    def answer(self):
        return {"error": str(self), "kind": self.kind}, "error"


def check_size(path: Path) -> int:
    """A file handed in is read only when it fits a chat file; a larger one is refused before it is read."""
    size = path.stat().st_size
    if size > FILE_MOST:
        raise Refused(f"{path.name} is {human_size(size)}; a file here is at most {human_size(FILE_MOST)}. Split it "
                      f"(or share a link to it) and hand over the part that matters.", "too_large")
    return size


def check_made(name: str, raw: bytes) -> None:
    if len(raw) > FILE_MOST:
        raise Refused(f"{name} came to {human_size(len(raw))}, more than a chat file may be "
                      f"({human_size(FILE_MOST)}). Produce it in parts.", "too_large")


def named(filename: str, suffix: str, fallback: str = "document") -> str:
    """A safe file name with its suffix; letters of every script are kept, Arabic included."""
    name = re.sub(r"[^\w.\- ]", "_", str(filename or ""), flags=re.UNICODE).strip(" ._") or fallback
    return name if name.lower().endswith(suffix) else name + suffix


def folded(text: str) -> str:
    """Text as a quote is compared: compatibility forms unfolded (a PDF's Arabic presentation forms become letters),
    digits of every script made ASCII, quiet marks dropped, quotes and dashes made plain, spacing and case ignored."""
    text = unicodedata.normalize("NFKC", str(text or "")).translate(DIGITS)
    text = QUIET.sub("", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―−]", "-", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch in "\n\t")
    return " ".join(text.split()).casefold()


def bare(text: str) -> str:
    """Folded text with its spacing and punctuation gone too: what a value is checked against inside its quote."""
    return re.sub(r"[\W_]+", "", folded(text), flags=re.UNICODE)


def runs_right_to_left(text: str) -> bool:
    """Whether a paragraph runs right to left: its first strong letter is Arabic (or Hebrew), as the Unicode
    paragraph rule decides it."""
    for ch in str(text or ""):
        if RIGHT_TO_LEFT.match(ch):
            return True
        if unicodedata.bidirectional(ch) == "L":
            return False
    return False


def mostly_arabic(texts) -> bool:
    """Whether a document is Arabic: more of its letters Arabic than Latin."""
    arabic = latin = 0
    for text in texts:
        arabic += len(ARABIC.findall(str(text or "")))
        latin += len(LATIN.findall(str(text or "")))
    return arabic > latin


async def record_output(call, kind: str, filename: str, operation: str, purpose: str = "", pages: int = 0,
                        made_from: str = "") -> str:
    """The trail of what this agent produced: every file, by name, kind, operation and what it was made from."""
    return await call.records.create("output", {
        "kind": kind, "filename": filename[:200], "operation": operation, "purpose": str(purpose or "")[:200],
        **({"pages": pages} if pages else {}), **({"made_from": made_from[:2000]} if made_from else {})})
