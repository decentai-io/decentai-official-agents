"""A Google Doc as numbered paragraphs, across every tab, with its headers, footers and footnotes.

Read with ``includeTabsContent=true`` the document's content is in ``tabs`` (each with child tabs), and each tab holds
its body, headers, footers and footnotes. Without it Google gives the first tab only, which is why that flag is
always sent.

Paragraphs are numbered from 1 in reading order: for each tab, its body (table cells included), then its headers,
footers and footnotes. The ranges are Google's own indexes, never counted from the text, because Google counts
UTF-16 units and objects too. A header, footer or footnote is its own segment, with indexes from 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass
class Paragraph:
    number: int
    tab_id: str
    tab: str
    part: str
    segment_id: str
    start: int
    end: int
    text: str
    style: str
    in_table: bool
    #: Text only: no image, chip, footnote mark or other object a range rewrite would delete.
    plain: bool

    def row(self) -> dict[str, Any]:
        row = {"index": self.number, "tab": self.tab, "part": self.part, "style": self.style, "text": self.text}
        if self.in_table:
            row["in_table"] = True
        if not self.plain:
            row["has_objects"] = True
        return row


def tabs(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(tab id, title, the tab's content) for every tab, child tabs after their parent. A document read without
    tabs (an old answer) is one tab."""
    found: list[tuple[str, str, dict[str, Any]]] = []

    def walk(items: list[dict[str, Any]]) -> None:
        for tab in items or []:
            properties = tab.get("tabProperties") or {}
            found.append((str(properties.get("tabId") or ""), str(properties.get("title") or ""),
                          tab.get("documentTab") or {}))
            walk(tab.get("childTabs") or [])

    walk(document.get("tabs") or [])
    return found or [("", "", document)]


def _element_text(element: dict[str, Any]) -> tuple[str, bool]:
    if "textRun" in element:
        return str((element["textRun"] or {}).get("content") or ""), True
    if "footnoteReference" in element:
        return f"[^{(element['footnoteReference'] or {}).get('footnoteNumber') or ''}]", False
    if "person" in element:
        props = (element["person"] or {}).get("personProperties") or {}
        return str(props.get("name") or props.get("email") or "[person]"), False
    if "richLink" in element:
        props = (element["richLink"] or {}).get("richLinkProperties") or {}
        return str(props.get("title") or props.get("uri") or "[link]"), False
    if "inlineObjectElement" in element:
        return "[image]", False
    if "dateElement" in element:
        props = (element["dateElement"] or {}).get("dateElementProperties") or {}
        return str(props.get("displayText") or "[date]"), False
    return "", False


def _walk(content: list[dict[str, Any]], in_table: bool = False) -> Iterator[tuple[dict[str, Any], bool]]:
    for element in content or []:
        if "paragraph" in element:
            yield element, in_table
        elif "table" in element:
            for row in (element["table"] or {}).get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    yield from _walk(cell.get("content") or [], True)


def paragraphs(document: dict[str, Any]) -> list[Paragraph]:
    found: list[Paragraph] = []
    for tab_id, title, content in tabs(document):
        parts = [("body", "", (content.get("body") or {}).get("content") or [])]
        for kind, key, id_key in (("header", "headers", "headerId"), ("footer", "footers", "footerId"),
                                  ("footnote", "footnotes", "footnoteId")):
            for segment_id, segment in (content.get(key) or {}).items():
                parts.append((kind, str(segment.get(id_key) or segment_id), segment.get("content") or []))
        for part, segment_id, elements in parts:
            for element, in_table in _walk(elements):
                paragraph = element.get("paragraph") or {}
                pieces = [_element_text(e) for e in paragraph.get("elements") or []]
                text = "".join(t for t, _ in pieces)
                found.append(Paragraph(
                    number=len(found) + 1, tab_id=tab_id, tab=title, part=part, segment_id=segment_id,
                    start=int(element.get("startIndex") or 0), end=int(element.get("endIndex") or 0),
                    text=text[:-1] if text.endswith("\n") else text,
                    style=str((paragraph.get("paragraphStyle") or {}).get("namedStyleType") or "NORMAL_TEXT"),
                    in_table=in_table, plain=all(ok for _, ok in pieces)))
    return found


def doc_link(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def stale(error: Any) -> bool:
    """Whether Google refused a batch because its required revision is no longer the latest."""
    body = error.body if isinstance(getattr(error, "body", None), dict) else {}
    status = str((body.get("error") or {}).get("status") or "") if isinstance(body.get("error"), dict) else ""
    return error.status == 400 and ("revision" in error.message.lower() or status == "FAILED_PRECONDITION")
