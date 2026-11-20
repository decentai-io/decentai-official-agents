"""A number as a page writes it — a price, a stock count, a rate — read in
code, never by a model, and compared in exact decimals.

Pages write numbers many ways: ``1,299.00``, ``1.299,00``, ``1 299,00``,
``1'299.00``, ``129,5``, and in Arabic-Indic digits ``١٬٢٩٩٫٠٠``. The
rules:

- Arabic-Indic and Persian digits are read as digits; ``٫`` is the
  decimal separator and ``٬`` a thousands one.
- With both ``.`` and ``,`` the last one is the decimal separator.
- With one of them only, a watch that says the page writes a decimal
  comma (``decimal_comma``) is believed; otherwise a separator followed
  by exactly three digits is a thousands one (``1,299`` is 1299; so is
  ``1.299`` with ``decimal_comma``), and any other is the decimal point.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
GROUPING = "    '’"
#: A number: groups of three after a separator, then a decimal part.
TOKEN = re.compile(
    r"(?<![\d.,])[-−]?\d{1,3}(?:[    '’,.٬]\d{3})+(?:[.,٫]\d+)?(?![\d])"
    r"|(?<![\d.,])[-−]?\d+(?:[.,٫]\d+)?(?![\d])")
#: How far after a phrase its number may be.
NEAR_CHARS = 80


def digits(text: str) -> str:
    return text.translate(DIGITS)


def number_of(token: str, decimal_comma: Optional[bool] = None) -> Optional[Decimal]:
    """The value a written number means, or None."""
    text = digits(str(token)).strip().replace("−", "-")
    for mark in GROUPING:
        text = text.replace(mark, "")
    text = text.replace("٬", "")
    if "٫" in text:
        text = text.replace(",", "").replace(".", "").replace("٫", ".")
    elif "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        other = "." if decimal == "," else ","
        text = text.replace(other, "").replace(decimal, ".")
    elif "," in text or "." in text:
        mark = "," if "," in text else "."
        count, after = text.count(mark), len(text.rsplit(mark, 1)[1])
        lead = text.split(mark, 1)[0].lstrip("-")
        if decimal_comma is True:
            decimal = count == 1 and (mark == "," or after != 3 or lead in ("", "0"))
        elif decimal_comma is False:
            decimal = mark == "." and count == 1
        else:
            # 0.125 is a fraction whatever follows: no thousands start with 0.
            decimal = count == 1 and (after != 3 or lead in ("", "0"))
        text = text.replace(mark, ".") if decimal else text.replace(mark, "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def first_number(text: str, decimal_comma: Optional[bool] = None):
    """(value, as written, match) of the first number in the text, or None."""
    translated = digits(text)
    for match in TOKEN.finditer(translated):
        value = number_of(match.group(0), decimal_comma)
        if value is not None:
            return value, text[match.start():match.end()], match
    return None


def shown(value: Optional[Decimal]) -> str:
    """A value in plain digits: 1299.5, never 1.2995E+3."""
    if value is None:
        return ""
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"
