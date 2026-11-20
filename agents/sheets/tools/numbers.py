"""Numbers written as text, read the way the column writes them.

A column says which mark is its decimal one: "1.234,56" and "12,50" say the comma (as in much of Europe and in
Arabic-speaking offices that type with a comma), "1,234.56" and "12.50" say the point. A column of nothing but
"1,234" or "2.500" does not say: that could be one thousand two hundred and thirty-four or one point two three
four. Such a column is ambiguous, and its numbers are refused until the person names the mark (``decimal``).
A column that says both is mixed, and refused the same way.

Arabic-Indic digits (٠١٢…٩, and the Persian ۰۱۲…۹) are digits; the Arabic decimal separator (٫) is always a
decimal mark and the Arabic thousands separator (٬) always a thousands one. A currency written beside the amount
("AED 1,250.00", "1.250,00 د.إ", "$12") is set aside and remembered: a column of two currencies is never added up.

An identifier is not a number: "00412" (a leading zero), "+971501234567" (a leading plus), or more than fifteen
digits. Those stay text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
#: Marks that only ever group thousands.
GROUPING = "    '٬"
#: Currencies written beside an amount: ISO codes in capitals, the common symbols, and the Gulf's Arabic forms. A
#: word that is none of these ("Box 5", "INV1043", "صندوق 5") keeps the text from being a number at all.
CODES = ("AED", "SAR", "QAR", "KWD", "BHD", "OMR", "USD", "EUR", "GBP", "EGP", "JOD", "LBP", "IQD", "MAD", "TND",
         "DZD", "LYD", "SDG", "YER", "SYP", "INR", "PKR", "CNY", "JPY", "CHF", "CAD", "AUD", "TRY", "SGD", "HKD")
ARABIC_CURRENCIES = ("د.إ", "د.ا", "ر.س", "ر.ق", "د.ك", "د.ب", "ر.ع", "ج.م", "درهم", "دراهم", "ريال", "دينار",
                     "جنيه", "دولار", "يورو")
_CURRENCY = "(?:" + "|".join([*CODES, r"[$€£¥]", *(re.escape(c) for c in ARABIC_CURRENCIES)]) + ")"
_SHAPE = re.compile(rf"^(?P<pre>{_CURRENCY})?\s*(?P<sign>[-−]?)\s*(?P<pre2>{_CURRENCY})?\s*"
                    rf"(?P<core>[\d.,{GROUPING}٫]*\d[\d.,{GROUPING}٫]*|[.,٫]\d+)\s*"
                    rf"(?P<tail>-?)\s*(?P<post>{_CURRENCY})?$")
_GROUPED = re.compile(r"^\d{1,3}(?:,\d{3})+$")
IDENTIFIER = re.compile(r"^(?:0\d+|\+\d[\d\s-]*)$")
LONGEST = 15


@dataclass
class Shape:
    """What one text says about itself: its decimal mark ("." or ",", None when it has none, "?" when it could be
    either), its value under each mark, and the currency written beside it."""

    evidence: Optional[str]
    as_point: Optional[Decimal]
    as_comma: Optional[Decimal]
    currency: str = ""

    def value(self, mark: str) -> Optional[Decimal]:
        return self.as_comma if mark == "," else self.as_point


def identifier(text: str) -> bool:
    plain = str(text).strip().translate(DIGITS)
    return bool(IDENTIFIER.match(plain)) or len(re.sub(r"\D", "", plain)) > LONGEST


def _decimal(digits: str) -> Optional[Decimal]:
    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def _grouped(whole: str, sep: str) -> Optional[str]:
    """The whole part with its thousands marks taken out, or None when they don't group by three."""
    if sep not in whole:
        return whole
    return whole.replace(sep, "") if _GROUPED.match(whole.replace(sep, ",")) else None


def shape(text: Any) -> Optional[Shape]:
    """What a text says as a number, or None when it is no number at all (words, identifiers, percentages)."""
    raw = str(text).strip()
    if not raw or identifier(raw):
        return None
    found = _SHAPE.match(raw.translate(DIGITS))
    negative = False
    if found is None:
        inner = raw.translate(DIGITS)
        if inner.startswith("(") and inner.endswith(")"):
            found, negative = _SHAPE.match(inner[1:-1].strip()), True
        if found is None:
            return None
    currencies = [c for c in (found["pre"], found["pre2"], found["post"]) if c]
    if len(currencies) > 1:
        return None
    negative = negative or bool(found["sign"]) or bool(found["tail"])
    core = found["core"].strip("    '")    # a space before the currency is no grouping
    for mark in GROUPING:
        if mark in core and mark != "٬":
            parts = core.split(".")[0].split(",")[0].split("٫")[0]
            if not re.fullmatch(rf"\d{{1,3}}(?:[{re.escape(mark)}]\d{{3}})+", parts):
                return None
            core = core.replace(mark, "")
    if "٬" in core:
        core = core.replace("٬", "")
    sign = "-" if negative else ""

    def made(evidence, point, comma):
        return Shape(evidence, _decimal(sign + point) if point is not None else None,
                     _decimal(sign + comma) if comma is not None else None, (currencies or [""])[0].strip())

    if "٫" in core:                                   # the Arabic decimal separator says itself
        if core.count("٫") > 1 or "." in core or "," in core:
            return None
        plain = core.replace("٫", ".")
        return made("٫", plain, plain)
    commas, points = core.count(","), core.count(".")
    if commas and points:
        last = "," if core.rfind(",") > core.rfind(".") else "."
        other = "." if last == "," else ","
        whole, _, fraction = core.rpartition(last)
        if other in fraction or core.count(last) > 1 or not fraction.isdigit():
            return None
        whole = _grouped(whole, other)
        if whole is None or not whole.isdigit():
            return None
        plain = f"{whole}.{fraction}"
        return made(last, plain if last == "." else None, plain if last == "," else None)
    for mark, other in ((",", "."), (".", ",")):
        count = commas if mark == "," else points
        if not count:
            continue
        if count > 1:                                       # 1,234,567: the mark groups, the other is decimal
            whole = _grouped(core, mark)
            if whole is None:
                return None
            return made(other, whole if other == "." else None, whole if other == "," else None)
        whole, _, fraction = core.partition(mark)
        if not fraction.isdigit() or (whole and not whole.isdigit()):
            return None
        as_decimal = f"{whole or '0'}.{fraction}"
        if len(fraction) == 3 and whole and len(whole) <= 3 and whole[0] != "0":
            grouped = whole + fraction                      # 1,234 or 2.500: either reading is honest
            point = grouped if mark == "," else as_decimal
            comma = as_decimal if mark == "," else grouped
            return made("?", point, comma)
        return made(mark, as_decimal if mark == "." else None, as_decimal if mark == "," else None)
    return made(None, core, core) if core.isdigit() else None


def native(value: Any) -> Optional[Decimal]:
    """A number the file holds as a number (never a bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


@dataclass
class Column:
    """What a column's texts say, together: ``state`` is "." or "," (the mark it writes), "none" (whole numbers
    only, either mark reads them alike), "ambiguous" (only values like 1,234), "mixed" (both marks), or "text"
    (no numbers written as text)."""

    state: str = "text"
    currencies: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def settled(self) -> bool:
        return self.state in (".", ",", "none", "text")


def detect(texts: Iterable[str]) -> Column:
    seen, currencies, unsure = set(), [], []
    for text in texts:
        found = shape(text)
        if found is None:
            continue
        if found.currency and found.currency not in currencies:
            currencies.append(found.currency)
        if found.evidence == "?":
            if len(unsure) < 3 and str(text).strip() not in unsure:
                unsure.append(str(text).strip())
            seen.add("?")
        elif found.evidence == "٫":
            seen.add(".")
        elif found.evidence:
            seen.add(found.evidence)
        else:
            seen.add("")
    marks = seen & {".", ","}
    if len(marks) == 2:
        state = "mixed"
    elif marks:
        state = marks.pop()
    elif "?" in seen:
        state = "ambiguous"
    elif "" in seen:
        state = "none"
    else:
        state = "text"
    return Column(state, currencies, unsure)


class Unsettled(Exception):
    """A column whose numbers can't be read without a guess."""


def reader(column_name: str, found: Column, decimal: Optional[str] = None):
    """How to read one column's cells as numbers: (value) -> Decimal | None. Refused (Unsettled) when the column is
    ambiguous or mixed and no decimal mark was named, or when it holds more than one currency."""
    if len(found.currencies) > 1:
        raise Unsettled(f"Column '{column_name}' holds amounts in {len(found.currencies)} currencies "
                        f"({', '.join(found.currencies)}); they are not added together. Filter one currency first.")
    if found.state in ("ambiguous", "mixed") and decimal not in (".", ","):
        why = (f"values such as {', '.join(found.examples)} could be thousands or decimals"
               if found.state == "ambiguous" else "some values use a decimal point and others a decimal comma")
        raise Unsettled(f"Column '{column_name}' doesn't say its decimal mark: {why}. Say decimal \".\" "
                        f"(1,234 is one thousand two hundred and thirty-four) or \",\" (1,234 is 1.234); "
                        f"nothing is guessed.")
    mark = found.state if found.state in (".", ",") else (decimal if decimal in (".", ",") else ".")

    def read(value: Any) -> Optional[Decimal]:
        number = native(value)
        if number is not None or value is None or not isinstance(value, str):
            return number
        found_shape = shape(value)
        if found_shape is None:
            return None
        if found_shape.evidence in (".", ",") and found_shape.evidence != mark:
            return None                                       # it says the other mark: not read as this one
        return found_shape.value(mark)

    read.mark = mark
    return read


def plain(value: Any) -> Optional[Decimal]:
    """A number a person or the model gave in a call: 12, "12.5", "1,234.50" (a point is the decimal mark)."""
    number = native(value)
    if number is not None:
        return number
    if not isinstance(value, str):
        return None
    found = shape(value)
    if found is None:
        return None
    return found.value(",") if found.evidence == "," else found.value(".")


def text(number: Decimal) -> str:
    """A Decimal as plain text, never in exponent form."""
    shown = format(number, "f")
    return "0" if shown in ("-0", "-0.0") else shown
