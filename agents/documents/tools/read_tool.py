"""Reading a document: what it is, its text page by page to the end, what its pictures say through the model, and
named fields extracted with a quote behind each, over the whole document in windows.

Extraction is checked in code, twice: the quote must be in the page's text, and the value must be in the quote. A
field nobody found is ``not_stated`` only when every page was read; otherwise it is ``not_read``, with the pages that
were not, so text never read is never reported as silent.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from standard import ToolBase

from . import pictures
from .common import Refused, bare, folded
from .reading import Loaded, load

EXTRACT_SYSTEM = (
    "You read one part of a business document and answer ONLY with a JSON object keyed by the field names asked. "
    "For a field this part states, give {\"found\": true, \"value\": <the value exactly as written in the document, "
    "same digits and script>, \"quote\": <the shortest passage, copied verbatim from the text, that states it>, "
    "\"page\": <the number from the [page N] marker above it>}; otherwise {\"found\": false}. Never guess, infer, "
    "calculate, convert, translate or complete a value. The document may be in Arabic or any language: quote it "
    "in its own script.")
MOST_PICTURE_PAGES = 20


class ReadTool(ToolBase):
    id = "read"

    async def _loaded(self, call, ref: str) -> Loaded:
        loaded = await load(call, "source", ref)
        return loaded

    # -- inspect ---------------------------------------------------------------------------------------------------

    async def inspect(self, call):
        try:
            loaded = await self._loaded(call, str(call.inputs["file_ref"]))
        except Refused as refused:
            return refused.answer()
        result: Dict[str, Any] = {
            "file_ref": loaded.file_ref, "filename": loaded.filename, "kind": loaded.kind, "unit": loaded.unit,
            "pages": len(loaded.pages), "readable_pages": loaded.readable, "unreadable_pages": loaded.unreadable,
            "scanned_pages": loaded.scanned, "characters": loaded.characters, "headings": loaded.headings[:50]}
        if loaded.encoding:
            result["encoding"] = loaded.encoding
        if loaded.pictures:
            result["pictures"] = [{"page": page, "count": count} for page, count in sorted(loaded.pictures.items())]
        if loaded.kind == "pptx":
            result.update({"titles": [{"slide": n, "title": t} for n, t in enumerate(loaded.titles, 1)][:300],
                           "chart_slides": loaded.charts, "has_notes": loaded.notes, "layouts": loaded.layouts[:20]})
        if loaded.problem:
            result["problem"] = loaded.problem
        return result, "success"

    # -- text ------------------------------------------------------------------------------------------------------

    async def text(self, call):
        try:
            loaded = await self._loaded(call, str(call.inputs["file_ref"]))
        except Refused as refused:
            return refused.answer()
        if loaded.problem and not loaded.pages:
            return {"error": loaded.problem, "kind": "invalid"}, "error"
        start = int(call.inputs.get("from_page") or 1)
        offset = int(call.inputs.get("from_char") or 0)
        budget = int(call.inputs.get("max_chars") or 20000)
        if start > len(loaded.pages):
            return {"error": f"The file has {len(loaded.pages)} {loaded.unit}(s); there is no {loaded.unit} "
                             f"{start}.", "kind": "invalid"}, "error"
        out: List[Dict[str, Any]] = []
        used, next_page, next_char = 0, 0, 0
        for number in range(start, len(loaded.pages) + 1):
            text, from_picture = loaded.pages[number - 1], False
            if not text and number in loaded.unreadable:
                read = [r for _, r in pictures.cached(loaded, number) if not r.empty]
                if read:
                    text, from_picture = "\n".join(r.text for r in read), True
            skip = offset if number == start else 0
            text = text[skip:]
            if used and used + len(text) > budget:
                next_page = number
                break
            entry: Dict[str, Any] = {"page": number, "text": text[:budget - used]}
            if from_picture:
                entry["from_picture"] = True
            if skip:
                entry["from_char"] = skip
            if len(text) > budget - used:
                entry["cut"] = True
                next_page, next_char = number, skip + budget - used
                out.append(entry)
                break
            out.append(entry)
            used += len(text)
        result = {"file_ref": loaded.file_ref, "filename": loaded.filename, "unit": loaded.unit,
                  "pages_total": len(loaded.pages), "pages": out, "truncated": bool(next_page),
                  "unreadable_pages": [n for n in loaded.unreadable
                                       if not any(not r.empty for _, r in pictures.cached(loaded, n))],
                  "scanned_pages": loaded.scanned}
        if next_page:
            result["next_page"] = next_page
            if next_char:
                result["next_char"] = next_char
        return result, "success"

    # -- pictures --------------------------------------------------------------------------------------------------

    async def pictures(self, call):
        try:
            loaded = await self._loaded(call, str(call.inputs["file_ref"]))
        except Refused as refused:
            return refused.answer()
        if loaded.problem and not loaded.pages:
            return {"error": loaded.problem, "kind": "invalid"}, "error"
        mode = str(call.inputs.get("mode") or "transcribe")
        count = int(call.inputs.get("count") or 4)
        total = len(loaded.pages)
        if call.inputs.get("pages"):
            wanted = sorted({int(n) for n in call.inputs["pages"]})
            outside = [n for n in wanted if not 1 <= n <= total]
            if outside:
                return {"error": f"The file has {total} {loaded.unit}(s): {outside} are not in it.",
                        "kind": "invalid"}, "error"
        else:
            start = int(call.inputs.get("from_page") or 1)
            if loaded.kind in ("pdf",):
                wanted = [n for n in range(start, total + 1) if n in loaded.unreadable or n in loaded.scanned]
                wanted = wanted or list(range(start, total + 1))
            elif loaded.kind in ("pptx", "docx"):
                wanted = [n for n in sorted(loaded.pictures) if n >= start]
            else:
                wanted = [1]
        if not wanted:
            return {"error": f"{loaded.filename} has no pictures to read.", "kind": "not_found"}, "error"
        now, later = wanted[:count], wanted[count:]
        await call.progress(f"Reading {len(now)} {loaded.unit}(s) of {loaded.filename} as pictures")
        found = await pictures.read(call, loaded, now, mode)
        readings, refused, cut = [], 0, 0
        for number in now:
            for label, reading in found.get(number) or []:
                entry: Dict[str, Any] = {"page": number, "picture": label, "from_picture": True,
                                         "text": "" if reading.empty else reading.text}
                if reading.refused:
                    entry["refused"], refused = True, refused + 1
                if reading.cut:
                    entry["cut"], cut = True, cut + 1
                if reading.problem:
                    entry["problem"] = reading.problem
                elif reading.empty and mode == "transcribe":
                    entry["problem"] = "The model saw no text in this picture."
                readings.append(entry)
        result = {"file_ref": loaded.file_ref, "filename": loaded.filename, "unit": loaded.unit, "mode": mode,
                  "pages_total": total, "readings": readings, "refused": refused, "cut": cut}
        if later:
            result["next_pages"] = later[:50]
        return result, "success"

    # -- extract ---------------------------------------------------------------------------------------------------

    async def extract(self, call):
        try:
            loaded = await self._loaded(call, str(call.inputs["file_ref"]))
        except Refused as refused:
            return refused.answer()
        if loaded.problem and not loaded.pages:
            return {"error": loaded.problem, "kind": "invalid"}, "error"
        wanted = [{"name": str(f["name"]), "hint": str(f.get("hint") or "")} for f in call.inputs["fields"]]
        size = int(call.inputs.get("max_chars") or 24000)
        most = int(call.inputs.get("max_windows") or 10)

        # The pages to read: their text layer, or what the model reads off their pictures when asked to.
        from_picture: set = set()
        texts: Dict[int, str] = {n: loaded.pages[n - 1] for n in loaded.readable}
        blind = [n for n in range(1, len(loaded.pages) + 1) if n not in texts]
        problems: List[str] = []
        if call.inputs.get("read_pictures") and loaded.kind in ("pdf", "image"):
            ask = [n for n in blind + loaded.scanned][:MOST_PICTURE_PAGES]
            if ask:
                await call.progress(f"Reading {len(ask)} scanned page(s) of {loaded.filename} as pictures")
                read = await pictures.read(call, loaded, sorted(set(ask)))
                for number, readings in read.items():
                    words = "\n".join(r.text for _, r in readings if not r.empty and not r.refused)
                    if words and not any(r.cut for _, r in readings):
                        texts[number] = (texts.get(number, "") + "\n" + words).strip()
                        from_picture.add(number)
                    else:
                        problems += [f"page {number}: {r.problem}" for _, r in readings if r.problem]
                blind = [n for n in blind if n not in from_picture]
        if not texts:
            return {"error": "No page of this document has text to read"
                             + ("; pass read_pictures to read its scanned pages through the model."
                                if loaded.kind in ("pdf", "image") else "."), "kind": "invalid",
                    "unreadable_pages": blind}, "error"

        windows = self.windows(texts, from_picture, size)
        remaining = {w["name"] for w in wanted}
        best: Dict[str, Dict[str, Any]] = {}
        told: List[Dict[str, Any]] = []
        unread: set = set(blind)
        for index, (pages, text) in enumerate(windows, 1):
            span = self.span(pages)
            if not remaining:
                told.append({"window": index, "pages": span, "status": "not_needed"})
                continue
            if index > most:
                told.append({"window": index, "pages": span, "status": "not_reached"})
                unread.update(pages)
                continue
            asked = "\n".join(f"- {w['name']}" + (f": {w['hint']}" if w["hint"] else "")
                              for w in wanted if w["name"] in remaining)
            await call.progress(f"Reading {loaded.unit}s {span} of {loaded.filename} (window {index} of "
                                f"{len(windows)})")
            try:
                said = await call.model(f"DOCUMENT {loaded.filename}, {loaded.unit}s {span}:\n{text}\n\nFIELDS:\n"
                                        f"{asked}\n\nAnswer with one JSON object keyed by field name.",
                                        system=EXTRACT_SYSTEM, max_tokens=4000)
            except Exception as failed:
                return {"error": f"The model could not be asked: {failed}", "kind": "failed"}, "error"
            if getattr(said, "refused", False):
                told.append({"window": index, "pages": span, "status": "refused"})
                unread.update(pages)
                continue
            parsed = self.parsed(said)
            if not isinstance(parsed, dict):
                told.append({"window": index, "pages": span, "status": "cut" if getattr(said, "cut", False)
                             else "unanswered"})
                unread.update(pages)
                continue
            told.append({"window": index, "pages": span, "status": "cut" if getattr(said, "cut", False) else "read"})
            if getattr(said, "cut", False):
                unread.update(pages)            # the answer stopped short: a field it never reached was not read
            for name in list(remaining):
                item = parsed.get(name)
                if not (isinstance(item, dict) and item.get("found") is True and item.get("value") not in (None, "")):
                    continue
                entry = self.checked(name, item, texts, pages, from_picture)
                entry.update({"window": index, "window_pages": span})
                if entry["verified"]:
                    best[name] = entry
                    remaining.discard(name)
                elif name not in best:
                    best[name] = entry          # kept until a later window states it verifiably

        whole = not unread
        fields, found, missing = [], 0, 0
        for want in wanted:
            entry = best.get(want["name"])
            if entry:
                found += 1
                fields.append(entry)
                continue
            missing += 1
            fields.append({"name": want["name"], "found": False, "status": "not_stated" if whole else "not_read",
                           **({} if whole else {"unread_pages": sorted(unread)[:200]})})
        verified = sum(1 for f in fields if f.get("verified"))
        extract_ref = await call.records.create("extract", {
            "file_ref": loaded.file_ref, "filename": loaded.filename[:200], "found": found, "missing": missing,
            "verified": verified, "fields": ", ".join(w["name"] for w in wanted)[:2000],
            "result": {"fields": fields, "windows": told}})
        result = {"extract_ref": extract_ref, "file_ref": loaded.file_ref, "filename": loaded.filename,
                  "unit": loaded.unit, "fields": fields, "found": found, "verified": verified, "missing": missing,
                  "windows": told, "whole_document_read": whole, "unread_pages": sorted(unread)[:500],
                  "refused": any(w["status"] == "refused" for w in told),
                  "cut": any(w["status"] == "cut" for w in told), "truncated": not whole}
        if problems:
            result["problems"] = problems[:20]
        return result, "success"

    @staticmethod
    def windows(texts: Dict[int, str], from_picture: set, size: int) -> List[Tuple[List[int], str]]:
        """Pages packed in order into windows of at most ``size`` characters; a longer page is read in pieces,
        each its own window, so nothing is left out."""
        out: List[Tuple[List[int], str]] = []
        pages: List[int] = []
        chunks: List[str] = []
        used = 0
        for number in sorted(texts):
            marker = f"[page {number}{', read from a picture' if number in from_picture else ''}]\n"
            text = texts[number]
            pieces = [text[i:i + size - len(marker)] for i in range(0, len(text), size - len(marker))] or [""]
            for piece in pieces:
                block = marker + piece
                if used + len(block) > size and chunks:
                    out.append((pages, "\n\n".join(chunks)))
                    pages, chunks, used = [], [], 0
                if number not in pages:
                    pages.append(number)
                chunks.append(block)
                used += len(block) + 2
        if chunks:
            out.append((pages, "\n\n".join(chunks)))
        return out

    @staticmethod
    def span(pages: List[int]) -> str:
        return str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"

    @staticmethod
    def checked(name: str, item: Dict[str, Any], texts: Dict[int, str], pages: List[int],
                from_picture: set) -> Dict[str, Any]:
        """A field as the model gave it, checked in code: the quote against the window's pages, word for word
        (spacing, digits' script and Arabic marks aside), and the value against the quote."""
        value, quote = str(item.get("value")).strip(), str(item.get("quote") or "").strip()
        entry: Dict[str, Any] = {"name": name, "found": True, "value": value, "quote": quote}
        needle = folded(quote)
        page = 0
        if len(needle) >= 2:
            claimed = item.get("page") if isinstance(item.get("page"), int) else 0
            order = ([claimed] if claimed in pages else []) + [n for n in pages if n != claimed]
            page = next((n for n in order if needle in folded(texts.get(n, ""))), 0)
        in_quote = bool(bare(value)) and bare(value) in bare(quote)
        entry["quote_in_text"] = bool(page)
        entry["value_in_quote"] = in_quote
        entry["verified"] = bool(page) and in_quote
        entry["status"] = "found" if entry["verified"] else "unverified"
        if page:
            entry["page"] = page
            if page in from_picture:
                entry["from_picture"] = True
        elif isinstance(item.get("page"), int) and item["page"] in pages:
            entry["page"] = int(item["page"])
        if not entry["verified"]:
            entry["check"] = ("the quote is not in the document's text" if not page else
                              "the value is not written in the quote")
        return entry

    @staticmethod
    def parsed(answer: str) -> Optional[dict]:
        text = str(answer or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[4:] if text.startswith("json") else text
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            found = json.loads(text[start:end + 1])
        except ValueError:
            return None
        return found if isinstance(found, dict) else None
