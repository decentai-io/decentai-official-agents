"""Reading a file as JSON, and finding things inside it.

A document is JSON or JSON Lines, decided by trying: a file that is not one JSON value but whose first line is,
with more lines after it, is JSON Lines, which is what most exports are. A line of it that doesn't parse is named by
its number, never half-read. ``NaN`` and ``Infinity`` are not JSON, and are refused where they stand.

The path language is small enough to state completely: ``a.b`` for a nested key, ``a[0]`` for one item (``a[-1]``
the last), ``a[*]`` for every item, ``*`` for every value of an object, and ``$`` for the whole document.

Values are compared as JSON compares them: ``1`` is ``1.0``, but ``1`` is not ``true`` and ``"1"`` is not ``1``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .tables import decode

MAX_DEPTH = 24
MAX_KEYS = 300


class NotJson(ValueError):
    pass


class Doc:
    """One file, read: its kind, what parsed, and what failed."""

    def __init__(self, file_ref: str, filename: str, kind: str, data: Any, records: List[Any], size: int,
                 problem: str = "", line: int = 0, column: int = 0, encoding: str = ""):
        self.file_ref, self.filename, self.kind = file_ref, filename, kind   # json | jsonl | csv | unsupported
        self.data, self.records, self.size = data, records, size
        self.problem, self.line, self.column, self.encoding = problem, line, column, encoding

    @property
    def valid(self) -> bool:
        return not self.problem and self.kind in ("json", "jsonl")

    @property
    def top_level(self) -> str:
        return type_of(self.data) if self.valid else "none"


def type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _constant(name: str):
    raise NotJson(f"{name} is not a JSON value")


def loads(text: str) -> Any:
    return json.loads(text, parse_constant=_constant)


def _where(text: str, token: str) -> Tuple[int, int]:
    found = re.search(rf"(?<![\w\"]){re.escape(token)}(?![\w\"])", text)
    if not found:
        return 0, 0
    before = text[:found.start()]
    return before.count("\n") + 1, found.start() - before.rfind("\n")


def parse(file_ref: str, filename: str, raw: bytes) -> Doc:
    text, encoding = decode(raw)
    size = len(raw)
    if not text.strip():
        return Doc(file_ref, filename, "unsupported", None, [], size, "The file is empty.", encoding=encoding)
    try:
        data = loads(text)
    except NotJson as exc:
        token = str(exc).split(" ", 1)[0]
        line, column = _where(text, token)
        return Doc(file_ref, filename, "unsupported", None, [], size,
                   f"The file is not valid JSON: {exc} (line {line}).", line, column, encoding)
    except ValueError as exc:
        lines = [(n, line) for n, line in enumerate(text.splitlines(), 1) if line.strip()]
        first_parses = False
        if len(lines) >= 2:
            try:
                loads(lines[0][1])
                first_parses = True
            except ValueError:
                pass
        if first_parses:
            values = []
            for number, line in lines:
                try:
                    values.append(loads(line))
                except ValueError as bad:
                    return Doc(file_ref, filename, "jsonl", None, [], size,
                               f"Line {number} of the JSON Lines is not valid JSON: {bad}. Nothing was read; "
                               f"{len(values)} line(s) before it were fine.", number,
                               getattr(bad, "colno", 0) or 0, encoding)
            return Doc(file_ref, filename, "jsonl", values, values, size, encoding=encoding)
        if _looks_csv(filename, text):
            return Doc(file_ref, filename, "csv", None, [], size,
                       "This is a CSV file, not JSON. json_from_table turns it into JSON.", encoding=encoding)
        return Doc(file_ref, filename, "unsupported", None, [], size, f"The file is not valid JSON: {exc}",
                   getattr(exc, "lineno", 0) or 0, getattr(exc, "colno", 0) or 0, encoding)
    return Doc(file_ref, filename, "json", data, data if isinstance(data, list) else [data], size, encoding=encoding)


def _looks_csv(filename: str, text: str) -> bool:
    if (filename or "").lower().endswith((".csv", ".tsv")):
        return True
    head = text.strip().splitlines()[:3]
    return len(head) >= 2 and all(any(d in line for d in ",;\t") for line in head)


# -- the path language ---------------------------------------------------------------------------------------------

def parse_path(path: str) -> List[Tuple[str, Any]]:
    """``a.b[0].c[*]`` → steps. Raises ValueError on nonsense."""
    text = str(path or "").strip()
    if text in ("", "$"):
        return []
    text = text[2:] if text.startswith("$.") else text[1:] if text.startswith("$") else text
    steps: List[Tuple[str, Any]] = []
    token, index = "", 0
    while index < len(text):
        char = text[index]
        if char == ".":
            if token:
                steps.append(_key(token))
                token = ""
            index += 1
            continue
        if char == "[":
            if token:
                steps.append(_key(token))
                token = ""
            close = text.find("]", index)
            if close == -1:
                raise ValueError(f"unclosed '[' in path '{path}'")
            inside = text[index + 1:close].strip()
            if inside == "*":
                steps.append(("wild", None))
            else:
                try:
                    steps.append(("index", int(inside)))
                except ValueError:
                    raise ValueError(f"'[{inside}]' is not an index or '*' in path '{path}'")
            index = close + 1
            continue
        token += char
        index += 1
    if token:
        steps.append(_key(token))
    return steps


def _key(token: str) -> Tuple[str, Any]:
    return ("wild", None) if token == "*" else ("key", token)


def select(data: Any, steps: List[Tuple[str, Any]], limit: int = 1000) -> List[Tuple[str, Any]]:
    """(path, value) for everything the steps reach, in document order."""
    found: List[Tuple[str, Any]] = [("$", data)]
    for kind, argument in steps:
        nxt: List[Tuple[str, Any]] = []
        for path, value in found:
            if kind == "key":
                if isinstance(value, dict) and argument in value:
                    nxt.append((_join(path, argument), value[argument]))
            elif kind == "index":
                if isinstance(value, list):
                    position = argument if argument >= 0 else len(value) + argument
                    if 0 <= position < len(value):
                        nxt.append((f"{path}[{position}]", value[position]))
            elif isinstance(value, list):
                nxt.extend((f"{path}[{i}]", item) for i, item in enumerate(value))
            elif isinstance(value, dict):
                nxt.extend((_join(path, key), item) for key, item in value.items())
            if len(nxt) >= limit:
                break
        found = nxt[:limit]
        if not found:
            break
    return found


def _join(path: str, key: str) -> str:
    return str(key) if path == "$" else f"{path}.{key}"


# -- what is in a file ---------------------------------------------------------------------------------------------

def key_map(records: Iterable[Any], max_records: int):
    """Every path the records use, how many records use it, the types seen there and one example. Array indexes
    collapse to ``[]``."""
    present: Dict[str, int] = {}
    types: Dict[str, List[str]] = {}
    example: Dict[str, str] = {}
    scanned = depth_seen = 0
    truncated = False
    for record in records:
        if scanned >= max_records:
            truncated = True
            break
        scanned += 1
        seen: Dict[str, None] = {}
        depth_seen = max(depth_seen, _walk(record, "", seen, types, example, 1))
        for path in seen:
            present[path] = present.get(path, 0) + 1
    rows = [{"path": path, "present": count, "types": types.get(path, []), "example": example.get(path, "")}
            for path, count in present.items()]
    rows.sort(key=lambda row: (-row["present"], row["path"]))
    return rows[:MAX_KEYS], len(rows) > MAX_KEYS or truncated, scanned, depth_seen


def _walk(value, path, seen, types, example, depth) -> int:
    if depth > MAX_DEPTH:
        return depth
    deepest = depth
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _note(child, item, seen, types, example)
            deepest = max(deepest, _walk(item, child, seen, types, example, depth + 1))
    elif isinstance(value, list):
        child = f"{path}[]" if path else "[]"
        for item in value[:200]:
            if isinstance(item, (dict, list)):
                deepest = max(deepest, _walk(item, child, seen, types, example, depth + 1))
            else:
                _note(child, item, seen, types, example)
    return deepest


def _note(path, value, seen, types, example) -> None:
    seen[path] = None
    kind = type_of(value)
    known = types.setdefault(path, [])
    if kind not in known and len(known) < 5:
        known.append(kind)
    if path not in example and not isinstance(value, (dict, list)):
        example[path] = preview(value, 80)


def preview(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def as_json(value: Any, limit: int = 200) -> str:
    """A value as JSON text, so "1" and 1 and true read apart."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


# -- flattening, comparing -----------------------------------------------------------------------------------------

def flatten(value: Any, path: str = "") -> Dict[str, Any]:
    """A record as dotted columns. A list becomes its JSON text: a cell holds one value."""
    out: Dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(item, dict) and item:
                out.update(flatten(item, child))
            elif isinstance(item, (list, dict)):
                out[child] = json.dumps(item, ensure_ascii=False)
            else:
                out[child] = item
    else:
        out[path or "value"] = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
    return out


def leaves(value: Any, path: str = "$") -> Dict[str, Any]:
    """Every scalar in the document by full path, in document order. Arrays are compared by position."""
    out: Dict[str, Any] = {}
    if isinstance(value, dict):
        if not value:
            out[path] = {}
        for key, item in value.items():
            out.update(leaves(item, _join(path, key)))
    elif isinstance(value, list):
        if not value:
            out[path] = []
        for index, item in enumerate(value):
            out.update(leaves(item, f"{path}[{index}]"))
    else:
        out[path] = value
    return out


def same(a: Any, b: Any) -> bool:
    """JSON's equality: 1 == 1.0, but 1 is not true, and "1" is not 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def cell(value: Any) -> str:
    """One value as a CSV cell: text as itself, the rest as JSON, so null and the word "null" read apart."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return json.dumps(value, ensure_ascii=False)


def number(value: Any) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
