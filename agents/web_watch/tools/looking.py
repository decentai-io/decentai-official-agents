"""One look at a page, in the forms a watch compares, and what it finds.

Everything here blocks (parsing, a person's regular expression, a
diff), so the watch tool calls it in a thread. A person's regular
expression runs on the ``regex`` module with a time limit, and one too
slow for a page is refused, never left to hang the agent.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import zlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

import regex

from .numbers import NEAR_CHARS, first_number, number_of, shown
from .page_text import phrase_pattern

#: How long a person's pattern may run on one page.
REGEX_SECONDS = 2.0
#: The text a pattern runs over at most.
SEARCH_CHARS = 1_000_000
EXCERPT_CHARS = 300
QUOTE_CONTEXT = 100
#: The snapshot a diff is made from: compressed, over up to four fields of a record.
SNAPSHOT_FIELDS = 4
FIELD_CHARS = 19_000
DIFF_LINES = 60
DIFF_CHARS = 6000
SELECTED_MOST = 200


class TooSlow(Exception):
    """A person's pattern took longer than REGEX_SECONDS."""


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- what a person asks for, checked before anything is fetched ---------------

def pattern_refusal(pattern: str) -> str:
    try:
        regex.compile(pattern)
    except regex.error as exc:
        return f"The pattern is not a valid regular expression: {exc.msg} (at position {exc.pos})."
    except (ValueError, OverflowError, RecursionError) as exc:
        return f"The pattern is not a valid regular expression: {exc}."
    return ""


def selector_refusal(selector: str) -> str:
    import soupsieve

    try:
        soupsieve.compile(selector)
    except soupsieve.SelectorSyntaxError as exc:
        return f"The CSS selector is not valid: {str(exc).splitlines()[0]}"
    except Exception as exc:
        return f"The CSS selector is not valid: {exc}"
    return ""


def selected_lines(markup: str, selector: str) -> tuple:
    """(lines, found): the text of the elements a CSS selector picks."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    picked = soup.select(selector, limit=SELECTED_MOST)
    lines = []
    for element in picked:
        for line in element.get_text("\n").split("\n"):
            line = " ".join(line.split())
            if line:
                lines.append(line)
    return lines, bool(picked)


# -- a reading ---------------------------------------------------------------

class Reading:
    """One look at a page, or at the part of it a selector picks."""

    def __init__(self, fetched, page, selector: str = ""):
        self.final_url = fetched.final_url
        self.at = now_utc()
        if selector:
            self.lines, self.part_found = selected_lines(fetched.text(), selector)
        else:
            self.lines, self.part_found = page.lines(), True
        self.text = "\n".join(self.lines)[:SEARCH_CHARS]
        flat = " ".join(self.text.split())
        self.hash = hashlib.sha256(flat.encode("utf-8")).hexdigest()
        self.opening = flat[:EXCERPT_CHARS]

    def look(self, watch: dict) -> dict:
        """{found, value, excerpt, number, written} for this watch."""
        kind = str(watch.get("kind") or "page")
        target = str(watch.get("target") or "")
        if kind in ("page", "selector"):
            return {"found": self.part_found, "value": "", "excerpt": self.opening}
        if kind == "contains":
            match = phrase_pattern(target).search(self.text)
            return self._found(match, " ".join(match.group(0).split()) if match else "")
        if kind == "pattern":
            match = self._search(target)
            if match is None:
                return self._found(None, "")
            value = (match.group(1) or "") if match.groups() else match.group(0)
            return self._found(match, " ".join(value.split())[:200])
        if kind == "number":
            return self._number(watch)
        raise ValueError(f"No watch kind {kind}.")

    def _number(self, watch: dict) -> dict:
        locate = str(watch.get("locate") or "part")
        target = str(watch.get("target") or "")
        comma = decimal_comma(watch)
        if locate == "pattern":
            match = self._search(target)
            if match is None:
                return self._found(None, "")
            written = (match.group(1) or "") if match.groups() else match.group(0)
            found = first_number(written, comma)
            anchor = match
        elif locate == "phrase":
            anchor = phrase_pattern(target).search(self.text)
            if anchor is None:
                return self._found(None, "")
            found = first_number(self.text[anchor.end():anchor.end() + NEAR_CHARS], comma)
        else:
            found = first_number(self.text, comma)
            anchor = found[2] if found else None
        if found is None:
            return self._found(None, "")
        value, written, _ = found
        result = self._found(anchor, shown(value))
        result["number"], result["written"] = value, " ".join(written.split())
        return result

    def _search(self, pattern: str):
        try:
            return regex.compile(pattern).search(self.text, timeout=REGEX_SECONDS, concurrent=True)
        except TimeoutError:
            raise TooSlow(f"The pattern ran for more than {REGEX_SECONDS:g} seconds on this page "
                          f"and was stopped. Make it simpler (avoid nested repeats such as (a|aa)+).")

    def _found(self, match, value: str) -> dict:
        if match is None:
            return {"found": False, "value": "", "excerpt": "", "number": None, "written": ""}
        return {"found": True, "value": value, "excerpt": self._quote(match), "number": None,
                "written": ""}

    def _quote(self, match) -> str:
        start = max(0, match.start() - QUOTE_CONTEXT)
        end = min(len(self.text), match.end() + QUOTE_CONTEXT)
        quote = " ".join(self.text[start:end].split())
        return ("…" if start else "") + quote + ("…" if end < len(self.text) else "")


def decimal_comma(watch: dict) -> Optional[bool]:
    said = watch.get("decimal_comma")
    if said in (None, ""):
        return None
    return bool(int(said))


def decimal(value) -> Optional[Decimal]:
    text = str(value or "").strip()
    return number_of(text) if text else None


# -- snapshots and diffs -----------------------------------------------------

def pack(lines: List[str]) -> tuple:
    """(fields, kept): the lines compressed into the snapshot fields, as
    many from the top as fit; ``kept`` is how many lines that is."""
    room = SNAPSHOT_FIELDS * FIELD_CHARS

    def packed(count: int) -> str:
        raw = "\n".join(lines[:count]).encode("utf-8")
        return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")

    kept, blob = len(lines), packed(len(lines))
    if len(blob) > room:
        low, high = 0, len(lines)
        while low < high:                       # the most lines that fit
            middle = (low + high + 1) // 2
            if len(packed(middle)) <= room:
                low = middle
            else:
                high = middle - 1
        kept, blob = low, packed(low)
    fields = {f"snapshot_{n + 1}": blob[n * FIELD_CHARS:(n + 1) * FIELD_CHARS]
              for n in range(SNAPSHOT_FIELDS)}
    return fields, kept


def unpack(fields: dict) -> List[str]:
    blob = "".join(str(fields.get(f"snapshot_{n + 1}") or "") for n in range(SNAPSHOT_FIELDS))
    if not blob:
        return []
    try:
        text = zlib.decompress(base64.b64decode(blob)).decode("utf-8")
    except (ValueError, zlib.error):
        return []
    return text.split("\n") if text else []


def diff(before: List[str], after: List[str]) -> dict:
    """Removed lines as '- …' and added ones as '+ …', capped, with the
    whole counts so a cap is never silent."""
    lines, added, removed = [], 0, 0
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        lines.extend(f"- {line}" for line in before[i1:i2])
        lines.extend(f"+ {line}" for line in after[j1:j2])
    kept, size, truncated = [], 0, False
    for line in lines:
        line = line if len(line) <= 300 else line[:300] + "…"
        if len(kept) >= DIFF_LINES or size + len(line) > DIFF_CHARS:
            truncated = True
            break
        kept.append(line)
        size += len(line)
    return {"diff": kept, "diff_truncated": truncated, "lines_added": added,
            "lines_removed": removed}
