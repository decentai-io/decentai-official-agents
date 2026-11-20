"""A data source's schema, and what may be written to it or asked of it.

Notion would take a select option it has never seen and quietly add it, and it refuses an unknown property with a
message that does not say which properties exist. Both are checked here first, from the schema itself, so a refusal
names the valid choices and nothing is sent that the database does not already define.

Since Notion's 2025-09-03 version the schema belongs to a *data source*, one of a database's (most databases have
one). ``DatabaseSchema`` is a data source's schema, with the database it belongs to.

Numbers are exact (``Decimal``) from the input to the request. People are named by name or address and turned into
Notion's user ids here, from the workspace's own list; a name that matches no one, or several, is refused.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .text import PropertyText, Text

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}"
                  r"(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$")

#: Property types Notion computes itself; nothing can write them.
COMPUTED = {"formula", "rollup", "created_time", "created_by", "last_edited_time",
            "last_edited_by", "unique_id", "verification", "button"}
TEXT_TYPES = {"title", "rich_text", "url", "email", "phone_number"}
OPTION_TYPES = ("select", "multi_select", "status")
PEOPLE_TYPES = ("people", "created_by", "last_edited_by")
DATE_TYPES = ("date", "created_time", "last_edited_time")
#: Notion's request limits: one array at most 100 items; an email or phone at most 200 characters, a URL 2,000.
ARRAY_MOST = 100
LENGTH_MOST = {"url": 2000, "email": 200, "phone_number": 200}

#: Which operators each kind of property takes (Notion's filter conditions).
OPERATORS = {
    "text": {"equals", "does_not_equal", "contains", "does_not_contain", "starts_with", "ends_with",
             "is_empty", "is_not_empty"},
    "number": {"equals", "does_not_equal", "greater_than", "less_than", "greater_than_or_equal_to",
               "less_than_or_equal_to", "is_empty", "is_not_empty"},
    "checkbox": {"equals", "does_not_equal"},
    "select": {"equals", "does_not_equal", "is_empty", "is_not_empty"},
    "multi_select": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
    "date": {"equals", "before", "after", "on_or_before", "on_or_after", "is_empty", "is_not_empty"},
    "people": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
    "relation": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
}
#: A compound filter nests at most this deep (Notion: a group may hold groups, which hold conditions).
NESTING_MOST = 2


class SchemaProblem(Exception):
    """A value the schema does not allow, worded for the person."""


def exact(value: Any, name: str) -> Decimal:
    """A number as an exact decimal: 12.50 stays 12.50. A comma is refused rather than guessed at."""
    if isinstance(value, bool):
        raise SchemaProblem(f"'{name}' is a number, not yes/no.")
    if isinstance(value, float):
        value = repr(value)
    text = str(value).strip()
    if "," in text:
        raise SchemaProblem(f"'{name}' is a number; write '{text}' with a point for decimals and no thousands "
                            f"separator (12.50, 1250).")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise SchemaProblem(f"'{name}' is a number; '{value}' is not one.") from None
    if not number.is_finite():
        raise SchemaProblem(f"'{name}' is a number; '{value}' is not one.")
    return number


class People:
    """The workspace's people, to name one by name, address or id."""

    def __init__(self, users: List[Dict[str, Any]]):
        self.users = users

    @staticmethod
    def email(user: Dict[str, Any]) -> str:
        return str((user.get("person") or {}).get("email") or "")

    def find(self, wanted: Any, name: str) -> str:
        text = str(wanted or "").strip()
        lowered = text.lower()
        for test in (lambda u: str(u.get("id") or "") == text,
                     lambda u: self.email(u).lower() == lowered and bool(lowered),
                     lambda u: str(u.get("name") or "").strip().lower() == lowered and bool(lowered)):
            found = [u for u in self.users if test(u)]
            if len(found) == 1:
                return str(found[0]["id"])
            if len(found) > 1:
                raise SchemaProblem(f"Several people in the workspace are called '{text}': "
                                    f"{', '.join(self.email(u) or str(u.get('id')) for u in found[:10])}. "
                                    f"Say which by address.")
        known = ", ".join(str(u.get("name") or self.email(u)) for u in self.users[:20])
        raise SchemaProblem(f"No one in the workspace is '{text}' (for '{name}'). People the integration can "
                            f"see: {known or '(none)'}.")


class DatabaseSchema:
    def __init__(self, source: Dict[str, Any], database: Optional[Dict[str, Any]] = None):
        database = database or {}
        parent = source.get("parent") or {}
        self.source_id = str(source.get("id") or "")
        self.database_id = str(parent.get("database_id") or database.get("id") or "")
        self.title = Text.rich(source.get("title")) or Text.rich(database.get("title"))
        self.url = str(source.get("url") or database.get("url") or "")
        self.in_trash = bool(source.get("in_trash") or database.get("in_trash"))
        self.properties: Dict[str, Dict[str, Any]] = {
            str(name): prop for name, prop in (source.get("properties") or {}).items()
            if isinstance(prop, dict)}

    # -- reading -------------------------------------------------------
    def type_of(self, name: str) -> str:
        return str(self.properties[name].get("type") or "")

    def options(self, name: str) -> List[str]:
        prop = self.properties.get(name) or {}
        kind = str(prop.get("type") or "")
        if kind not in OPTION_TYPES:
            return []
        return [str(o.get("name") or "") for o in (prop.get(kind) or {}).get("options") or []]

    def title_property(self) -> str:
        return next((n for n in self.properties if self.type_of(n) == "title"), "")

    def rows(self) -> List[Dict[str, Any]]:
        out = []
        for name in self.properties:
            row: Dict[str, Any] = {"name": name, "type": self.type_of(name)}
            if self.type_of(name) in OPTION_TYPES:
                row["options"] = self.options(name)
            out.append(row)
        return out

    def _known(self, name: str) -> str:
        if name in self.properties:
            return name
        # A case slip is forgiven only when it names exactly one property.
        folded = [n for n in self.properties if n.lower() == str(name).lower()]
        if len(folded) == 1:
            return folded[0]
        raise SchemaProblem(
            f"The database '{self.title}' has no property '{name}'. Its properties "
            f"are: {', '.join(self.properties)}.")

    def _option(self, name: str, value: Any) -> str:
        wanted = str(value).strip()
        options = self.options(name)
        match = next((o for o in options if o == wanted), None) or next(
            (o for o in options if o.lower() == wanted.lower()), None)
        if match is None:
            raise SchemaProblem(
                f"'{wanted}' is not an option of '{name}'. The options are: "
                f"{', '.join(options) or '(none defined)'}.")
        return match

    def needs_people(self, values: Dict[str, Any] | None = None, filter_: Any = None) -> bool:
        """Whether writing these values, or asking this filter, names a person."""
        def kind(name: Any) -> str:
            try:
                return self.type_of(self._known(str(name)))
            except SchemaProblem:
                return ""
        if any(kind(n) == "people" and v not in (None, [], "") for n, v in (values or {}).items()):
            return True
        stack = [filter_] if filter_ else []
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "conditions" in item:
                    stack.extend(item.get("conditions") or [])
                elif kind(item.get("property")) in PEOPLE_TYPES and item.get("operator") in (
                        "contains", "does_not_contain"):
                    return True
        return False

    # -- writing -------------------------------------------------------
    def build(self, values: Dict[str, Any], people: Optional[People] = None
              ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Values by property name as Notion's property payload, and the
        same values as text for the record. Raises SchemaProblem."""
        payload, shown = {}, {}
        for raw_name, value in values.items():
            name = self._known(raw_name)
            payload[name] = self._value(name, value, people)
            shown[name] = Text.clip(self._shown(value), 500)
        return payload, shown

    @staticmethod
    def _shown(value: Any) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return "" if value is None else str(value)

    @staticmethod
    def _many(value: Any, name: str) -> List[Any]:
        items = value if isinstance(value, list) else [p for p in str(value).split(",") if p.strip()]
        if len(items) > ARRAY_MOST:
            raise SchemaProblem(f"'{name}' takes at most {ARRAY_MOST} values at once; {len(items)} were given.")
        return items

    def _value(self, name: str, value: Any, people: Optional[People] = None) -> Dict[str, Any]:
        kind = self.type_of(name)
        if kind in COMPUTED:
            raise SchemaProblem(f"'{name}' is a {kind} property that Notion fills in "
                                f"itself; it cannot be written.")
        if value is None or value == []:
            return self._clear(name, kind)
        if kind in ("title", "rich_text"):
            return {kind: Text.write(str(value))}
        if kind in ("url", "email", "phone_number"):
            text = str(value).strip()
            if len(text) > LENGTH_MOST[kind]:
                raise SchemaProblem(f"'{name}' takes at most {LENGTH_MOST[kind]} characters; this is {len(text)}.")
            return {kind: text}
        if kind == "number":
            return {"number": exact(value, name)}
        if kind == "checkbox":
            return {"checkbox": self._yes(name, value)}
        if kind in ("select", "status"):
            return {kind: {"name": self._option(name, value)}}
        if kind == "multi_select":
            return {"multi_select": [{"name": self._option(name, v)} for v in self._many(value, name)]}
        if kind == "date":
            start = str(value).strip()
            if not DATE.match(start):
                raise SchemaProblem(f"'{name}' is a date; give YYYY-MM-DD or an ISO "
                                    f"8601 date-time, not '{start}'.")
            return {"date": {"start": start}}
        if kind == "relation":
            ids = self._many(value, name)
            return {"relation": [{"id": str(i).strip()} for i in ids if str(i).strip()]}
        if kind == "people":
            if people is None:
                raise SchemaProblem(f"'{name}' names people, and the workspace's people could not be read.")
            return {"people": [{"id": people.find(v, name)} for v in self._many(value, name)]}
        raise SchemaProblem(f"'{name}' is a {kind} property, which this agent does "
                            f"not write. Ask the person to set it in Notion.")

    @staticmethod
    def _yes(name: str, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("true", "yes"):
            return True
        if str(value).strip().lower() in ("false", "no"):
            return False
        raise SchemaProblem(f"'{name}' is a checkbox; give true or false.")

    def _clear(self, name: str, kind: str) -> Dict[str, Any]:
        if kind in ("title", "rich_text", "multi_select", "relation", "people"):
            return {kind: []}
        if kind in ("number", "select", "date", "url", "email", "phone_number"):
            return {kind: None}
        raise SchemaProblem(f"'{name}' cannot be emptied here.")

    # -- asking ----------------------------------------------------------
    def compound(self, given: Dict[str, Any], people: Optional[People] = None, depth: int = 1) -> Dict[str, Any]:
        """``{match: all | any, conditions: [condition or group]}`` as Notion's ``and`` / ``or`` filter."""
        if depth > NESTING_MOST:
            raise SchemaProblem(f"Notion takes filter groups at most {NESTING_MOST} deep: a group may hold "
                                f"groups of conditions, and no deeper.")
        conditions = list(given.get("conditions") or [])
        if not conditions:
            raise SchemaProblem("A filter group needs at least one condition.")
        if len(conditions) > ARRAY_MOST:
            raise SchemaProblem(f"A filter group holds at most {ARRAY_MOST} conditions.")
        built = [self.compound(c, people, depth + 1) if "conditions" in c else
                 self.filter(str(c.get("property") or ""), str(c.get("operator") or ""), c.get("value"), people)
                 for c in conditions]
        return {"or" if given.get("match") == "any" else "and": built}

    @staticmethod
    def _family(kind: str) -> str:
        if kind in TEXT_TYPES:
            return "text"
        if kind == "status":
            return "select"
        if kind in DATE_TYPES:
            return "date"
        if kind in PEOPLE_TYPES:
            return "people"
        return kind

    def filter(self, name: str, operator: str, value: Any, people: Optional[People] = None) -> Dict[str, Any]:
        """One condition as a Notion filter. Raises SchemaProblem."""
        if not name:
            raise SchemaProblem("A filter condition names a property.")
        name = self._known(name)
        kind = self.type_of(name)
        family = self._family(kind)
        text = "" if value is None else str(value).strip()
        # The older single-filter words, kept: checkbox X is equals X; equals on a multi-select is contains.
        if operator == "checkbox":
            if kind != "checkbox":
                raise SchemaProblem(f"'{name}' is a {kind} property, not a checkbox.")
            operator = "equals"
        if operator == "equals" and family == "multi_select":
            operator = "contains"
        allowed = OPERATORS.get(family)
        if not allowed:
            raise SchemaProblem(f"'{name}' is a {kind} property, which this agent does not filter on.")
        if operator not in allowed:
            raise SchemaProblem(f"'{name}' is a {kind} property; it takes {', '.join(sorted(allowed))}, "
                                f"not {operator}.")
        if operator in ("is_empty", "is_not_empty"):
            return {"property": name, kind: {operator: True}}
        if family == "text":
            return {"property": name, kind: {operator: text}}
        if family == "number":
            return {"property": name, "number": {operator: exact(value, name)}}
        if family == "checkbox":
            return {"property": name, "checkbox": {operator: self._yes(name, value)}}
        if family in ("select", "multi_select"):
            return {"property": name, kind: {operator: self._option(name, text)}}
        if family == "date":
            if not DATE.match(text):
                raise SchemaProblem(f"'{name}' is a date; {operator} takes YYYY-MM-DD or an ISO 8601 "
                                    f"date-time, not '{text}'.")
            return {"property": name, kind: {operator: text}}
        if family == "people":
            if people is None:
                raise SchemaProblem(f"'{name}' names people, and the workspace's people could not be read.")
            return {"property": name, kind: {operator: people.find(value, name)}}
        return {"property": name, kind: {operator: text}}

    def sort(self, name: str, direction: str) -> Dict[str, Any]:
        direction = "ascending" if direction == "ascending" else "descending"
        if name in ("created_time", "last_edited_time") and name not in self.properties:
            return {"timestamp": name, "direction": direction}
        return {"property": self._known(name), "direction": direction}


class PageRow:
    """A page, or a database row, as one result row."""

    PARENTS = ("page_id", "database_id", "data_source_id", "block_id")

    @classmethod
    def parent(cls, obj: Dict[str, Any]) -> str:
        parent = obj.get("parent") or {}
        kind = str(parent.get("type") or "")
        if kind == "workspace":
            return "workspace"
        if kind == "data_source_id":
            return f"database {parent.get('database_id') or parent.get('data_source_id')}"
        if kind in cls.PARENTS:
            return f"{kind[:-3]} {parent.get(kind)}"
        return kind

    @classmethod
    def parent_id(cls, obj: Dict[str, Any]) -> str:
        parent = obj.get("parent") or {}
        kind = str(parent.get("type") or "")
        return str(parent.get(kind) or "") if kind in cls.PARENTS else ""

    @staticmethod
    def source_of(obj: Dict[str, Any]) -> Tuple[str, str]:
        """(data source id, database id) of a row; ("", "") for a page that is no row."""
        parent = obj.get("parent") or {}
        kind = str(parent.get("type") or "")
        if kind == "data_source_id":
            return str(parent.get("data_source_id") or ""), str(parent.get("database_id") or "")
        if kind == "database_id":
            return "", str(parent.get("database_id") or "")
        return "", ""

    @classmethod
    def row(cls, page: Dict[str, Any], with_properties: bool = True) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "page_id": str(page.get("id") or ""),
            "title": Text.clip(PropertyText.title(page), 300),
            "url": str(page.get("url") or ""),
            "created": str(page.get("created_time") or ""),
            "last_edited": str(page.get("last_edited_time") or ""),
        }
        if with_properties:
            row["properties"] = PropertyText.all(page)
        return row
