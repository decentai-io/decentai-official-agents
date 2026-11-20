"""Reading a receipt or policy file — as text, or as pictures the chat's model reads — and the small deterministic
checks the model's answers are held to: money in either decimal convention, dates, currencies, merchants."""

from __future__ import annotations

import asyncio
import io
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from standard.http import FILE_MOST, human_size

CATEGORIES = ("meals", "travel", "accommodation", "transport", "office", "other")

#: Currencies whose minor unit is not the cent.
MINOR = {"BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "IQD": 3, "LYD": 3, "TND": 3, "JPY": 0, "KRW": 0}
#: "$" alone is any of these: refused unless the currency is stated.
DOLLARS = ("USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN")
CODES = ("USD", "EUR", "GBP", "AED", "SAR", "QAR", "KWD", "BHD", "OMR", "EGP", "JOD", "CAD", "AUD", "NZD", "SGD",
         "HKD", "INR", "PKR", "CHF", "JPY", "CNY", "TRY", "MAD", "LBP", "IQD", "MXN")
#: Marks printed on receipts that name one currency, longest first so "US$" wins over "$".
SYMBOLS = [("US$", "USD"), ("C$", "CAD"), ("CA$", "CAD"), ("A$", "AUD"), ("AU$", "AUD"), ("S$", "SGD"),
           ("HK$", "HKD"), ("NZ$", "NZD"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("₺", "TRY"),
           ("د.إ", "AED"), ("درهم إماراتي", "AED"), ("Dhs", "AED"), ("DHS", "AED"), ("ر.س", "SAR"),
           ("ريال سعودي", "SAR"), ("ر.ق", "QAR"), ("ريال قطري", "QAR"), ("د.ك", "KWD"), ("دينار كويتي", "KWD"),
           ("د.ب", "BHD"), ("دينار بحريني", "BHD"), ("ر.ع", "OMR"), ("ريال عماني", "OMR"), ("ج.م", "EGP"),
           ("جنيه مصري", "EGP"), ("د.أ", "JOD"), ("دينار أردني", "JOD")]
SYMBOLS.sort(key=lambda pair: -len(pair[0]))

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
for _i, _names in enumerate([("يناير", "كانون الثاني"), ("فبراير", "شباط"), ("مارس", "آذار"),
                             ("أبريل", "ابريل", "إبريل", "نيسان"), ("مايو", "أيار"), ("يونيو", "يونيه", "حزيران"),
                             ("يوليو", "يوليه", "تموز"), ("أغسطس", "اغسطس", "آب"), ("سبتمبر", "أيلول"),
                             ("أكتوبر", "اكتوبر", "تشرين الأول"), ("نوفمبر", "تشرين الثاني"),
                             ("ديسمبر", "كانون الأول")], 1):
    for _name in _names:
        MONTHS[_name] = _i

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_MONTH_WORD = r"([^\W\d_]{3,9}(?: [^\W\d_]{3,6})?)"
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_WORD + r"\.?,?\s+(\d{4})\b")
_MONTH_DAY = re.compile(r"\b([^\W\d_]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_TOTAL_WORDS = re.compile(r"total|amount|due|fare|paid|المجموع|الإجمالي|الاجمالي|المبلغ|الصافي", re.I)

#: The largest picture the model takes, and the side a picture is brought down to.
PICTURE_MOST = 5 * 1024 * 1024
PICTURE_SIDE = 2000


class TooLarge(Exception):
    """A file over what a chat file may be, refused before it is read."""


class Loaded:
    def __init__(self, file_ref: str, filename: str, kind: str, pages: List[str], problem: str = "",
                 pictures: Optional[List[bytes]] = None, page_count: int = 0):
        self.file_ref = file_ref
        self.filename = filename
        self.kind = kind            # pdf | text | image | scan | unsupported
        self.pages = pages          # the text of each page: one for a text file
        self.problem = problem
        self.pictures = pictures or []   # for a photo or a scan: each page as a picture the model can read
        self.page_count = page_count or len(pages)

    @property
    def text(self) -> str:
        return "\n".join(self.pages).strip()


async def load(call, resource_id: str, file_ref: str, pictures_most: int = 4) -> Loaded:
    """A file handed to the call, or one of the resource's, by its ref; its size checked before it is read, and its
    parsing done off the agent's loop."""
    path = await call.file(file_ref, resource_id)
    size = path.stat().st_size
    if size > FILE_MOST:
        raise TooLarge(f"{path.name} is {human_size(size)}; a file here is at most {human_size(FILE_MOST)}. "
                       f"Send a smaller copy (a photo at a lower resolution, or the receipt's own PDF).")
    raw = await asyncio.to_thread(path.read_bytes)
    return await asyncio.to_thread(_loaded, file_ref, path.name, raw, pictures_most)


def _loaded(file_ref: str, filename: str, raw: bytes, pictures_most: int) -> Loaded:
    name = filename.lower()
    if raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n" or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP") \
            or raw[:4] == b"GIF8" or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        try:
            return Loaded(file_ref, filename, "image", [], pictures=[picture(raw)], page_count=1)
        except Exception as exc:
            return Loaded(file_ref, filename, "image", [], f"The picture could not be opened ({exc}); enter its "
                                                            f"details and confirm them.")
    if name.endswith((".heic", ".heif")) or raw[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return Loaded(file_ref, filename, "unsupported", [], "A HEIC photo can't be shown to the model: send it as "
                                                             "JPEG or PNG (on an iPhone, Settings → Camera → Most "
                                                             "Compatible), or enter its details.")
    if raw[:5] == b"%PDF-" or name.endswith(".pdf"):
        return _pdf(file_ref, filename, raw, pictures_most)
    text = decoded(raw)
    if text is None:
        return Loaded(file_ref, filename, "unsupported", [], "Not a PDF, text or picture file.")
    return Loaded(file_ref, filename, "text", [text])


def decoded(raw: bytes) -> Optional[str]:
    """Text in the encodings receipts and policies arrive in: UTF-8 (with or without its mark), UTF-16 with its
    mark, or Windows Arabic (cp1256), which also holds the Latin letters of Western receipts."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if b"\x00" in raw[:4000]:
        return None
    try:
        return raw.decode("cp1256")
    except UnicodeDecodeError:
        return None


def picture(raw: bytes) -> bytes:
    """A photo the way the model takes it: turned upright as the camera said, at most PICTURE_SIDE on a side, JPEG,
    under PICTURE_MOST."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        image.thumbnail((PICTURE_SIDE, PICTURE_SIDE))
        for quality in (85, 70, 55):
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality)
            if buffer.tell() <= PICTURE_MOST:
                return buffer.getvalue()
    raise ValueError("the picture stays over 5 MB however it is compressed")


def _pdf(file_ref: str, filename: str, raw: bytes, pictures_most: int) -> Loaded:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                return Loaded(file_ref, filename, "pdf", [], "The PDF is password-protected; send an unlocked copy.")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        return Loaded(file_ref, filename, "pdf", [], f"The PDF could not be opened: {exc}")
    if len("".join(pages).strip()) >= 20:
        return Loaded(file_ref, filename, "pdf", pages)
    # A scan: its pages as pictures for the model to read.
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(raw)
        count = len(document)
        rendered = []
        for number in range(min(count, pictures_most)):
            image = document[number].render(scale=2).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            rendered.append(picture(buffer.getvalue()))
        document.close()
    except Exception as exc:
        return Loaded(file_ref, filename, "scan", [], f"The PDF has no text layer and its pages could not be "
                                                      f"rendered ({exc}); enter its details and confirm them.")
    problem = (f"Only the first {pictures_most} of its {count} scanned pages were read." if count > pictures_most
               else "")
    return Loaded(file_ref, filename, "scan", [], problem, pictures=rendered, page_count=count)


# -- money -------------------------------------------------------------------------------------------------------

def arabic_digits(text: Any) -> str:
    """Arabic-Indic and Persian digits as 0-9; the Arabic decimal separator (٫) as "." and its thousands
    separator (٬) as ","."""
    return str(text if text is not None else "").translate(_DIGITS)


def minor(currency: str) -> int:
    return MINOR.get(str(currency or "").upper(), 2)


def quantized(value: Decimal, currency: str = "") -> Decimal:
    return value.quantize(Decimal(1).scaleb(-minor(currency)), rounding=ROUND_HALF_UP)


def decimal_mark(text: str) -> str:
    """The decimal mark a receipt's own figures use, where they show it unambiguously ("12.50" or "12,50"), or ""."""
    text = arabic_digits(text)
    dots = len(re.findall(r"\d\.\d{2}(?!\d)", text))
    commas = len(re.findall(r"\d,\d{2}(?!\d)", text))
    return "." if dots and not commas else "," if commas and not dots else ""


def parse_money(value: Any, currency: str = "", mark: str = "") -> Tuple[Optional[Decimal], str]:
    """An amount, exactly, in either convention: 1,234.56 and 1.234,56 are both 1234.56, and 12,50 is 12.50.
    A lone separator before three digits ("1,234", "1.234") is settled by ``mark`` (the convention the document
    uses), else by the currency (three decimals only in a currency that has them); otherwise it is refused as
    ambiguous: (None, why)."""
    if value is None or isinstance(value, bool):
        return None, ""
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value)), ""
        except InvalidOperation:
            return None, f"{value} is not an amount"
    shown = str(value).strip()
    text = arabic_digits(shown)
    for symbol, _ in SYMBOLS:
        text = text.replace(symbol, " ")
    text = re.sub(r"[\s   '’$]", "", text)
    text = re.sub(r"^[^\d\-.,]+|[^\d.,]+$", "", text)        # a currency mark either side
    negative = text.startswith("-")
    text = text.lstrip("-")
    if not text or not re.fullmatch(r"[\d.,]+", text) or not re.search(r"\d", text):
        return None, (f"{shown} is not an amount" if shown else "")
    marks = [c for c in text if c in ".,"]
    whole, fraction = text, ""
    if not marks:
        pass
    elif len(set(marks)) == 2:
        point = text[max(text.rfind("."), text.rfind(","))]
        group = "," if point == "." else "."
        whole, fraction = text.rsplit(point, 1)
        if point in whole or not _grouped(whole, group):
            return None, f"{shown} is not an amount"
        whole = whole.replace(group, "")
    elif len(marks) > 1:
        if not _grouped(text, marks[0]):
            return None, f"{shown} is not an amount"
        whole = text.replace(marks[0], "")
    else:
        sep = marks[0]
        head, tail = text.split(sep)
        if len(tail) != 3 or head in ("", "0"):
            whole, fraction = head or "0", tail
        elif mark:
            whole, fraction = (head, tail) if sep == mark else (head + tail, "")
        elif currency and minor(currency) != 3:
            whole = head + tail                                # no three-decimal amounts in this currency
        else:
            return None, (f"{shown} could be {head}{tail} or {head}.{tail}; say which, or give the currency "
                          f"(three decimals only for BHD, KWD, OMR, JOD)")
    try:
        found = Decimal(f"{whole or '0'}.{fraction}" if fraction else whole)
    except InvalidOperation:
        return None, f"{shown} is not an amount"
    return (-found if negative else found), ""


def _grouped(text: str, sep: str) -> bool:
    parts = text.split(sep)
    return 1 <= len(parts[0]) <= 3 and all(len(p) == 3 and p.isdigit() for p in parts[1:]) and parts[0].isdigit()


def money(value: Any, currency: str = "") -> Optional[Decimal]:
    """An amount kept or given, or None."""
    return parse_money(value, currency)[0]


def parse_rate(value: Any) -> Tuple[Optional[Decimal], str]:
    """An exchange rate: one separator is always the decimal mark (3,6725 and 3.6725 are the same rate)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        found = Decimal(str(value))
    else:
        text = re.sub(r"\s", "", arabic_digits(value))
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", text):
            return None, f"{value} is not a rate"
        found = Decimal(text.replace(",", "."))
    return (found, "") if found > 0 else (None, f"{value} is not a rate above zero")


def out(value: Decimal) -> float:
    return float(value)


def amounts_in(text: str, currency: str = "", mark: str = "") -> List[Decimal]:
    """Every money figure on the receipt: those with decimals, and whole numbers on a line naming a currency or a
    total. A figure two conventions could read is kept under both readings, since it is checked against, not used."""
    found: List[Decimal] = []
    for line in arabic_digits(text).splitlines():
        named = bool(currencies_in(line)[0]) or "$" in line
        tokens = [m.group(0) for m in re.finditer(r"(?<![\d.,])(?:(?<!\w)-)?\d(?:[\d.,]*\d)?(?!\d)", line)]
        for index, token in enumerate(tokens):
            whole_number = not re.search(r"[.,]", token)
            last_total = bool(_TOTAL_WORDS.search(line)) and index == len(tokens) - 1
            if whole_number and not (named or last_total):
                continue
            value, problem = parse_money(token, currency, mark)
            if value is not None:
                found.append(value)
            elif problem and "could be" in problem:
                head, tail = re.split(r"[.,]", token.lstrip("-"))
                found += [Decimal(head + tail), Decimal(f"{head}.{tail}")]
    return found


# -- currencies, dates, merchants ----------------------------------------------------------------------------------

def currencies_in(text: str) -> Tuple[List[str], bool]:
    """The currencies a receipt names by code or mark, and whether it shows a bare "$" (any of several dollars)."""
    codes, rest = set(), str(text or "")
    for symbol, code in SYMBOLS:
        if symbol in rest:
            codes.add(code)
            rest = rest.replace(symbol, " ")
    for match in re.finditer(r"(?<![A-Za-z])(" + "|".join(CODES) + r")(?![A-Za-z])", rest.upper()):
        codes.add(match.group(1))
    return sorted(codes), "$" in rest


def code_of(text: Any) -> str:
    """A currency the person or the model named: an ISO code, or a mark that names one; "" if none or "$"."""
    text = str(text or "").strip()
    if text.upper() in CODES:
        return text.upper()
    codes, _ = currencies_in(text)
    return codes[0] if len(codes) == 1 else ""


def iso_date(text: Any) -> Optional[date]:
    text = arabic_digits(text).strip()
    if len(text) != 10:
        return None
    try:
        found = date.fromisoformat(text)
    except ValueError:
        return None
    return found if found.isoformat() == text else None


def today(zone: str = "") -> date:
    """Today where the person is, when their zone is given; else UTC."""
    if zone:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(zone)).date()
    return datetime.now(timezone.utc).date()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_date(text: str) -> Optional[date]:
    """A date written the ways receipts write them, in Latin or Arabic digits and English or Arabic month names;
    an ambiguous day/month order (12/09/2026) is read only when the first number cannot be a month."""
    text = arabic_digits(text)
    found = iso_date(text.strip()) or (iso_date(_ISO.search(text).group(0)) if _ISO.search(text) else None)
    if found:
        return found
    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        for match in pattern.finditer(text):
            day, month, year = ((match.group(1), match.group(2), match.group(3)) if order == "dm"
                                else (match.group(2), match.group(1), match.group(3)))
            number = MONTHS.get(month.lower()) or MONTHS.get(month.lower().split(" ")[0])
            if number:
                try:
                    return date(int(year), number, int(day))
                except ValueError:
                    return None
    match = _DMY.search(text)
    if match:
        a, b, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            day, month = b, a
        elif a <= 12 and b <= 12 and a == b:
            day, month = a, b
        else:
            return None                      # 03/04/2026: nobody can tell
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def folded(text: str) -> str:
    """Text as compared: case, spacing, Arabic diacritics, tatweel and letter variants set aside."""
    text = unicodedata.normalize("NFKC", arabic_digits(text)).lower()
    text = re.sub(r"[ً-ْٰـ]", "", text)
    text = text.translate(str.maketrans("أإآٱىة", "اااايه"))
    return " ".join(text.split())


def normalize_merchant(name: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", folded(name)).split())


def parse_json(answer: str):
    text = str(answer or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


# -- records -------------------------------------------------------------------------------------------------------

def keys_of(record: Dict[str, Any]) -> Dict[str, Any]:
    return record.get("fields") or {}


def receipt_row(record: Dict[str, Any]) -> Dict[str, Any]:
    """A receipt as read whole (record.read: its keys and values together)."""
    keys = keys_of(record)
    row = {"receipt_ref": str(record.get("ref") or ""),
           **{name: str(keys.get(name) or "") for name in (
               "claim_ref", "file_ref", "filename", "merchant", "date", "currency", "verified", "assumed",
               "duplicate_of", "not_duplicate_of", "note", "added", "read_status")},
           "category": str(keys.get("category") or "unknown"),
           "status": str(keys.get("status") or "extracted"),
           "read_from": str(keys.get("read_from") or "text")}
    amount = money(keys.get("amount"))
    if amount is not None:
        row["amount"] = amount
    return row


def shown(row: Dict[str, Any]) -> Dict[str, Any]:
    """A receipt row as a function returns it: the amount a number."""
    return {k: (out(v) if isinstance(v, Decimal) else v) for k, v in row.items()
            if k not in ("not_duplicate_of", "added", "read_status")}


async def read_all(call, resource: str, refs: List[str]) -> List[Dict[str, Any]]:
    """Each record read whole, in the order given, a few at a time."""
    found: List[Dict[str, Any]] = []
    for start in range(0, len(refs), 10):
        found += await asyncio.gather(*(call.records.read(resource, ref) for ref in refs[start:start + 10]))
    return found


async def receipts(call, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The receipts a filter finds, read whole, in the order they were added (record.list is oldest first)."""
    listed = await call.records.list("receipt", filters)
    return [receipt_row(r) for r in await read_all(call, "receipt", [r["ref"] for r in listed])]


def rules_of(records: List[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """name -> (value, quote), the latest of each name."""
    rules: Dict[str, Tuple[str, str]] = {}
    for record in records:
        keys = keys_of(record)
        rules[str(keys.get("name") or "")] = (str(keys.get("value") or ""), str(keys.get("quote") or ""))
    return rules
