"""What a Microsoft Forms response table looks like, and one response as the assistant reads it.

Microsoft Forms has no API. A form whose responses sync to Excel (a work or school account's) keeps them in a table
whose first columns Forms fills itself: the response's id, its start time, its completion time, the responder's
email and name, and in newer forms the last modified time; then one column per question. The header words are in
the form owner's language ("ID", "المعرف", "Identificador"...), so a table is recognized by those columns' place
and kind, not only their words:

- position 1 is a whole number that grows from row to row (the id);
- positions 2 and 3 are date-times (Excel serial numbers), the completion never before the start;
- position 4 is an address, "anonymous" in the owner's language, or empty;
- position 5 is a name;

or, for a form nobody has answered yet, by the header words in the languages known here. Times are handed on as
ISO 8601 (``2026-09-01T10:03:00``): the time Excel shows, with no zone, since Forms writes none.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .workbook import Region, Workbook, clip

NAMES = {
    "id": {"id", "المعرف", "معرف", "رقم التعريف", "identificador", "identifiant", "kennung", "id."},
    "start": {"start time", "وقت البدء", "hora de inicio", "heure de début", "startzeit", "ora di inizio",
              "başlangıç saati", "horário de início"},
    "completion": {"completion time", "وقت الإكمال", "وقت الاكتمال", "hora de finalización", "heure de fin",
                   "fertigstellungszeit", "ora di completamento", "tamamlanma saati", "horário de conclusão"},
    "email": {"email", "e-mail", "البريد الإلكتروني", "correo electrónico", "adresse de messagerie", "e-mail-adresse",
              "posta elettronica", "e-posta"},
    "name": {"name", "الاسم", "اسم", "nombre", "nom", "nome", "ad"},
    "modified": {"last modified time", "وقت آخر تعديل", "hora de la última modificación",
                 "heure de dernière modification", "zeitpunkt der letzten änderung", "ora ultima modifica",
                 "son değiştirilme saati", "hora da última modificação"},
}
ANONYMOUS = {"", "anonymous", "مجهول", "مجهول الهوية", "anónimo", "anonyme", "anonym", "anonimo", "anonim", "anônimo"}
FIRST = ("id", "start", "completion", "email", "name")
EXCEL_EPOCH = datetime(1899, 12, 30)


def named(header: Any, kind: str) -> bool:
    return str(header or "").strip().lower() in NAMES[kind]


def serial(value: Any) -> float | None:
    """An Excel date-time serial (days since 1899-12-30), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 20000 <= value <= 80000 else None


def iso(value: Any) -> str:
    """An Excel date-time as ISO 8601, to the second; "" when it is not one."""
    days = serial(value)
    if days is None:
        text = str(value or "").strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "")).strftime("%Y-%m-%dT%H:%M:%S") if text else ""
        except ValueError:
            return ""
    moment = EXCEL_EPOCH + timedelta(days=days)
    return (moment + timedelta(microseconds=500_000)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def whole(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and float(value).is_integer() and value >= 1:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class Layout:
    """Where a response table's own columns are: the five Forms fills, and the last-modified one if present."""

    def __init__(self, modified: bool):
        self.modified = modified

    @property
    def kept(self) -> int:
        return 6 if self.modified else 5


def later(row: list[Any]) -> bool:
    """Whether a row's sixth column is a date-time no earlier than its completion: Forms' last modified time."""
    row = list(row) + [""] * 6
    modified, done = serial(row[5]), serial(row[2])
    return modified is not None and done is not None and modified >= done


def recognize(headers: list[str], rows: list[list[Any]]) -> Layout | None:
    """The table's layout if it is a Forms response table, by its columns' place and kind (``rows`` are a few of
    its rows' values) or, with no rows, by its header words; else None."""
    if len(headers) < 5:
        return None
    by_name = sum(named(headers[i], kind) for i, kind in enumerate(FIRST))
    rows = [r for r in rows if any(str(c if c is not None else "").strip() for c in r)]
    if rows:
        last_id, by_kind = 0, True
        for row in rows:
            row = list(row) + [""] * (6 - len(row))
            number, start, done = whole(row[0]), serial(row[1]), serial(row[2])
            if number is None or number <= last_id or start is None or done is None or done < start:
                by_kind = False
                break
            address = str(row[3] if row[3] is not None else "").strip().lower()
            if "@" not in address and address not in ANONYMOUS:
                by_kind = False
                break
            last_id = number
        if not by_kind and by_name < 4:
            return None
    elif by_name < 4:
        return None
    modified = len(headers) > 5 and (named(headers[5], "modified") or (
        bool(rows) and all(later(r) for r in rows) and not str(headers[5]).strip().endswith("?")))
    return Layout(modified)


def questions(headers: list[str], layout: Layout) -> list[str]:
    return headers[layout.kept:]


def response_row(region: Region, layout: Layout, values: list[Any], text: list[Any], index: int) -> dict | None:
    """One row as a response, or None when its id is not a whole number (a row a person typed, not one Forms
    wrote)."""
    values = list(values) + [""] * (len(region.headers) - len(values))
    text = list(text) + [""] * (len(region.headers) - len(text))
    number = whole(values[0])
    if number is None:
        return None
    row = {"response_id": number, "email": str(text[3] or "").strip(), "name": str(text[4] or "").strip(),
           "answers": {h: clip(text[i]) for i, h in enumerate(region.headers) if i >= layout.kept},
           "row_number": region.row_number(index)}
    for key, i in (("start_time", 1), ("completion_time", 2), *((("modified_time", 5),) if layout.modified else ())):
        moment = iso(values[i])
        if moment:
            row[key] = moment
        elif str(text[i] or "").strip():
            row[f"{key}_shown"] = str(text[i]).strip()
    return row


async def response_table(book: Workbook, table: str = "", most_tables: int = 10):
    """(region, layout) of the workbook's response table, the one named or the first that is one; or None."""
    names = [table] if table else [str(t.get("name") or "") for t in (await book.tables())[:most_tables]]
    for name in names:
        region = await book.table_region(name)
        sample, _ = await book.read_both(region, 0, 3)
        layout = recognize(region.headers, sample)
        if layout is not None:
            return region, layout
    return None


async def responses(book: Workbook, region: Region, layout: Layout, start: int, stop: int) -> list[dict]:
    values, text = await book.read_both(region, start, stop)
    found = [response_row(region, layout, v, t, start + i) for i, (v, t) in enumerate(zip(values, text))]
    return [r for r in found if r is not None]
