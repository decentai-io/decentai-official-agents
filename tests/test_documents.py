"""The Documents agent (2.0.0, the Presentations agent merged in), its program started and spoken to in the
standard's messages. The fixtures are files the agent produces itself (Word, PDF, PowerPoint), files built here with
the same libraries (a deck with a chart and a picture, an encrypted PDF), and hand-written PDFs: a page with no text
layer (a scan, as far as a reader can tell) and a form with a text field and a checkbox.

Arabic runs through every path: PDFs and Word files written right to left, cp1256 and UTF-16 text, Arabic-Indic
digits in quotes, Arabic placeholders, an Arabic page read off its picture.
"""

import io
import json
import re
import zipfile

import pytest

from tests.harness import Chat, ChatSinks, Records, probe, run

ARABIC_TITLE = "ملخص عقد الإيجار — الوحدة 4B"
ARABIC_SECTIONS = [
    {"heading": "مدة الإشعار", "paragraphs": [
        "يجب أن يصل إشعار عدم التجديد قبل تاريخ التجديد بتسعين (٩٠) يوماً على الأقل.",
        "تاريخ التجديد: ١ مارس ٢٠٢٧. The rent is 4,000.00 USD a month."]},
    {"heading": "المبالغ", "table_columns": ["البند", "المبلغ"],
     "table_rows": [["الإيجار الشهري", "4,000.00"], ["التأمين", "8.000,00"]]},
]
QUOTE_A = {
    "filename": "northlight-q2041.pdf", "title": "Quotation Q-2041 — Northlight Seating",
    "sections": [
        {"heading": "Offer", "paragraphs": [
            "20 x Aria task chair, black mesh, adjustable arms.",
            "Unit price USD 185.00, total USD 3,700.00 excluding shipping.",
            "Delivery within 10 working days of order. Shipping USD 120.00.",
            "Warranty 5 years on mechanism, 2 years on fabric."]},
        {"heading": "Terms", "paragraphs": ["Payment 30 days from invoice."]},
    ],
}
QUOTE_B = {
    "filename": "northlight-q2041-rev2.pdf", "title": "Quotation Q-2041 rev 2 — Northlight Seating",
    "sections": [
        {"heading": "Offer", "paragraphs": [
            "20 x Aria task chair, black mesh, adjustable arms.",
            "Unit price USD 179.00, total USD 3,580.00 excluding shipping.",
            "Delivery within 15 working days of order. Shipping USD 120.00.",
            "Warranty 5 years on mechanism, 2 years on fabric."]},
        {"heading": "Terms", "paragraphs": ["Payment 30 days from invoice.",
                                            "Prices valid until 30 September 2026."]},
    ],
}


def hand_pdf(objects, root_extra=b"") -> bytes:
    """A minimal PDF written by hand, cross-reference table included, so a strict reader accepts it."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


SCANNED_PDF = hand_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"])


def form_pdf() -> bytes:
    """A one-page AcroForm: a text field, a checkbox with its two appearances, and a choice."""
    stream = b"q 0 g 1 1 10 10 re f Q"
    return hand_pdf([
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R 5 0 R 8 0 R] /DA (/Helv 0 Tf 0 g) "
        b"/DR << /Font << /Helv 9 0 R >> >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Annots [4 0 R 5 0 R 8 0 R] >>",
        b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (company) /Rect [50 700 300 720] /DA (/Helv 10 Tf 0 g) "
        b"/Ff 2 /P 3 0 R >>",
        b"<< /Type /Annot /Subtype /Widget /FT /Btn /T (agree) /Rect [50 650 62 662] /V /Off /AS /Off "
        b"/AP << /N << /Yes 6 0 R /Off 7 0 R >> >> /P 3 0 R >>",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 12 12] /Length %d >>\nstream\n" % len(stream) + stream
        + b"\nendstream",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 12 12] /Length 0 >>\nstream\n\nendstream",
        b"<< /Type /Annot /Subtype /Widget /FT /Ch /T (emirate) /Rect [50 600 300 620] /Opt [(Dubai) (Sharjah)] "
        b"/DA (/Helv 10 Tf 0 g) /P 3 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def make(llm=None, ask=None):
    provider = Records()
    return Chat(provider, sinks=ChatSinks(llm=llm, ask=ask)), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["documents"], name, inputs, chat_level=chat_level))


def produced(agents, ex, name, inputs):
    result, status = invoke(agents, ex, name, inputs)
    assert status == "success", result
    return result, ex.files[result["file_ref"]]["content"]


def all_text(agents, ex, ref, max_chars=20000):
    """Every page of a file, read by following next_page and next_char to the end."""
    pages, inputs = [], {"file_ref": ref, "max_chars": max_chars}
    for _ in range(200):
        result, status = invoke(agents, ex, "read_text", inputs)
        assert status == "success", result
        pages += result["pages"]
        if not result["truncated"]:
            return pages, result
        inputs = {"file_ref": ref, "max_chars": max_chars, "from_page": result["next_page"],
                  **({"from_char": result["next_char"]} if result.get("next_char") else {})}
    raise AssertionError("never reached the end")


def fields_asked(prompt: str):
    return re.findall(r"^- ([^:\n]+)", prompt.split("FIELDS:", 1)[1], flags=re.M)


@pytest.fixture
def quotes(agents):
    ex, provider = make()
    a, _ = produced(agents, ex, "produce_pdf", QUOTE_A)
    b, _ = produced(agents, ex, "produce_pdf", QUOTE_B)
    return ex, provider, a["file_ref"], b["file_ref"]


class TestThePackage:
    def test_it_starts_and_says_ready_with_every_function(self, agents):
        assert probe(agents["documents"]) == []

    def test_producing_a_file_is_level_one_and_reading_level_zero(self, agents):
        levels = {f["id"]: f["level"] for f in agents["documents"].manifest["functions"]}
        assert {levels[f] for f in ("produce_docx", "produce_pdf", "produce_pptx", "produce_fill_template",
                                    "pdf_merge", "pdf_split", "pdf_rotate", "pdf_fill", "read_extract")} == {1}
        assert {levels[f] for f in ("read_inspect", "read_text", "read_pictures", "compare_versions",
                                    "pdf_fields")} == {0}

    def test_the_fonts_ship_with_their_licence(self, agents):
        fonts = agents["documents"].folder / "fonts"
        assert {p.name for p in fonts.iterdir()} >= {"NotoSans-Regular.ttf", "NotoNaskhArabic-Regular.ttf",
                                                     "NotoSans-Bold.ttf", "NotoNaskhArabic-Bold.ttf", "OFL.txt"}


class TestProducing:
    def test_a_latin_pdf_is_produced_read_back_and_recorded(self, agents):
        ex, provider = make()
        result, raw = produced(agents, ex, "produce_pdf", QUOTE_A)
        assert result["verified"] is True and result["pages"] == 1 and raw[:5] == b"%PDF-"
        record = provider.data["output"][result["output_ref"]]
        assert record["keys"] == {"kind": "pdf", "filename": "northlight-q2041.pdf", "operation": "produce",
                                  "pages": 1}

    def test_an_arabic_pdf_is_shaped_right_to_left_and_reads_back(self, agents):
        """1.0 wrote Latin-1 only: every Arabic letter came out as "?"."""
        ex, _ = make()
        result, raw = produced(agents, ex, "produce_pdf", {"filename": "ملخص-العقد", "title": ARABIC_TITLE,
                                                          "sections": ARABIC_SECTIONS})
        assert result["filename"] == "ملخص-العقد.pdf" and result["verified"] is True
        assert b"NotoNaskhArabic" in raw and b"NotoSans" in raw
        ref = ex.file("back.pdf", raw)
        pages, _ = all_text(agents, ex, ref)
        text = "".join(p["text"] for p in pages)
        assert "?" not in text
        for word in ("الإشعار", "التجديد", "الإيجار"):
            assert all(letter in text for letter in word)
        assert "4,000.00" in text

    def test_a_character_no_font_covers_is_refused_by_name(self, agents):
        ex, provider = make()
        result, status = invoke(agents, ex, "produce_pdf", {"filename": "x", "title": "Menu 菜单",
                                                            "sections": [{"paragraphs": ["hello"]}]})
        assert status == "error" and result["kind"] == "invalid" and "菜" in result["error"]
        assert not provider.data.get("output")

    def test_an_arabic_word_file_runs_right_to_left(self, agents):
        ex, _ = make()
        result, raw = produced(agents, ex, "produce_docx", {"filename": "ملخص", "title": ARABIC_TITLE,
                                                           "sections": ARABIC_SECTIONS})
        assert result["right_to_left"] is True and result["verified"] is True
        body = zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml").decode()
        assert "<w:bidi/>" in body and "<w:rtl/>" in body and "<w:bidiVisual/>" in body
        pages, _ = all_text(agents, ex, ex.file("back.docx", raw))
        assert "الإيجار الشهري | 4,000.00" in pages[0]["text"]

    def test_a_ragged_table_is_refused_with_its_numbers(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "produce_docx", {"filename": "x", "title": "t", "sections": [
            {"table_columns": ["a", "b"], "table_rows": [["1", "2"], ["1"]]}]})
        assert status == "error" and "row 2 has 1" in result["error"]

    def test_producing_needs_level_one(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "produce_pdf", QUOTE_A, chat_level=0)
        assert status == "error" and result.get("denied") is True

    def test_word_to_pdf_is_not_offered(self, agents):
        instructions = agents["documents"].manifest["agent"]["instructions"]
        assert "Converting a Word file to PDF with its layout is not offered" in instructions
        assert not agents["documents"].function("convert_docx")


class TestReading:
    def test_a_long_word_file_is_read_to_its_last_paragraph(self, agents):
        """1.0 returned one page cut at 60,000 characters, and nothing after it could be reached."""
        ex, _ = make()
        paragraphs = [f"البند {n}: " + ("نص العقد " * 60) + f"نهاية البند {n}." for n in range(1, 161)]
        _, raw = produced(agents, ex, "produce_docx", {"filename": "long", "title": "عقد طويل",
                                                      "sections": [{"paragraphs": paragraphs[:80]},
                                                                   {"paragraphs": paragraphs[80:]}]})
        ref = ex.file("long.docx", raw)
        info, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert info["unit"] == "part" and info["pages"] > 15 and info["characters"] > 90000
        pages, last = all_text(agents, ex, ref, max_chars=30000)
        text = "\n".join(p["text"] for p in pages)
        assert "نهاية البند 160." in text and text.count("نهاية البند") == 160
        assert last["pages_total"] == info["pages"]

    def test_a_word_table_stands_in_place_and_headers_are_read(self, agents):
        from docx import Document

        document = Document()
        document.sections[0].header.paragraphs[0].text = "سري — للاستخدام الداخلي"
        document.add_paragraph("Before the table.")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Item", "Price"
        document.add_paragraph("After the table.")
        buffer = io.BytesIO()
        document.save(buffer)
        ex, _ = make()
        pages, _ = all_text(agents, ex, ex.file("t.docx", buffer.getvalue()))
        assert pages[0]["text"].split("\n") == ["[header] سري — للاستخدام الداخلي", "Before the table.",
                                                "Item | Price", "After the table."]

    def test_windows_text_in_cp1256_and_utf16_is_read_as_arabic(self, agents):
        ex, _ = make()
        narrow = "فاتورة رقم 1043 - المبلغ 4.800,00 درهم"         # cp1256 has no Arabic-Indic digits
        wide = "فاتورة رقم ١٠٤٣ — المبلغ ٤٬٨٠٠٫٠٠ درهم"
        for name, raw, encoding, line in (("old.txt", narrow.encode("cp1256"), "cp1256", narrow),
                                          ("unicode.txt", wide.encode("utf-16"), "utf-16", wide),
                                          ("bom.txt", b"\xef\xbb\xbf" + wide.encode(), "utf-8-sig", wide)):
            ref = ex.file(name, raw)
            info, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
            assert info["encoding"] == encoding, name
            text, _ = invoke(agents, ex, "read_text", {"file_ref": ref})
            assert text["pages"][0]["text"] == line, name

    def test_a_page_longer_than_the_budget_goes_on_from_its_character(self, agents):
        ex, _ = make()
        line = " ".join(f"كلمة{n}" for n in range(600))
        ref = ex.file("one.txt", line)
        pages, _ = all_text(agents, ex, ref, max_chars=500)
        assert len(pages) > 5 and {p["page"] for p in pages} == {1}
        assert "".join(p["text"] for p in pages) == line

    def test_a_scanned_page_is_named_and_points_to_the_pictures(self, agents):
        ex, _ = make()
        ref = ex.file("scan.pdf", SCANNED_PDF)
        info, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert info["unreadable_pages"] == [1] and info["readable_pages"] == []
        result, status = invoke(agents, ex, "read_extract", {"file_ref": ref, "fields": [{"name": "total"}]})
        assert status == "error" and "read_pictures" in result["error"]

    def test_a_password_protected_pdf_is_said_to_be_one_not_a_scan(self, agents):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(SCANNED_PDF)))
        writer.encrypt("secret")
        buffer = io.BytesIO()
        writer.write(buffer)
        ex, _ = make()
        info, _ = invoke(agents, ex, "read_inspect", {"file_ref": ex.file("locked.pdf", buffer.getvalue())})
        assert "password-protected" in info["problem"] and info["unreadable_pages"] == []

    def test_a_file_over_the_chat_limit_is_refused_before_reading(self, agents):
        ex, _ = make()
        ref = ex.file("huge.txt", b"a" * (8 * 1024 * 1024))
        result, status = invoke(agents, ex, "read_text", {"file_ref": ref})
        assert status == "error" and result["kind"] == "too_large" and "8.0 MB" in result["error"]

    def test_legacy_word_is_named_unsupported(self, agents):
        ex, _ = make()
        info, _ = invoke(agents, ex, "read_inspect", {"file_ref": ex.file("old.doc", b"\xd0\xcf\x11\xe0 binary")})
        assert info["kind"] == "unsupported" and ".docx" in info["problem"]


class TestPictures:
    def test_a_scanned_pdf_page_goes_to_the_model_as_a_picture_and_comes_back_marked(self, agents):
        sent = []

        def llm(messages, max_tokens, pictures):
            sent.extend(pictures)
            assert "exactly as written" in messages[0]["text"]
            return "فاتورة ضريبية\nرقم الفاتورة: INV-1043\nالمبلغ المستحق: ٤٬٨٠٠٫٠٠ دولار"

        ex, _ = make(llm)
        ref = ex.file("scan.pdf", SCANNED_PDF)
        result, status = invoke(agents, ex, "read_pictures", {"file_ref": ref})
        assert status == "success", result
        assert len(sent) == 1 and sent[0][:3] == b"\xff\xd8\xff" and len(sent[0]) < 5 * 1024 * 1024
        reading = result["readings"][0]
        assert reading["page"] == 1 and reading["from_picture"] is True and "INV-1043" in reading["text"]
        # read_text now shows the page, marked as read from its picture; it was read once.
        text, _ = invoke(agents, ex, "read_text", {"file_ref": ref})
        assert text["pages"][0]["from_picture"] is True and text["unreadable_pages"] == []
        invoke(agents, ex, "read_pictures", {"file_ref": ref})
        assert len(sent) == 1

    def test_a_refusal_or_a_cut_reading_is_said(self, agents):
        replies = iter([("", "refusal"), ("partial text", "length")])
        ex, _ = make(lambda m, t, p: next(replies))
        ref = ex.file("scan.pdf", SCANNED_PDF)
        result, _ = invoke(agents, ex, "read_pictures", {"file_ref": ref})
        assert result["refused"] == 1 and result["readings"][0]["refused"] is True
        result, _ = invoke(agents, ex, "read_pictures", {"file_ref": ref})
        assert result["cut"] == 1 and "cut short" in result["readings"][0]["problem"]

    def test_a_photo_is_read_and_large_ones_are_made_to_fit(self, agents):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (4000, 3000), "white").save(buffer, "PNG")
        sizes = []

        def llm(messages, max_tokens, pictures):
            from PIL import Image as Img
            sizes.append(Img.open(io.BytesIO(pictures[0])).size)
            return "[no text]"

        ex, _ = make(llm)
        result, _ = invoke(agents, ex, "read_pictures", {"file_ref": ex.file("receipt.png", buffer.getvalue())})
        assert max(sizes[0]) <= 2000 and result["readings"][0]["problem"] == "The model saw no text in this picture."

    def test_extraction_reads_scanned_pages_through_pictures_and_checks_them(self, agents):
        def llm(messages, max_tokens, pictures):
            if pictures:
                return "فاتورة ضريبية\nرقم الفاتورة: INV-1043\nالمبلغ المستحق: ٤٬٨٠٠٫٠٠ دولار"
            assert "[page 1, read from a picture]" in messages[-1]["text"]
            return json.dumps({"invoice_number": {"found": True, "value": "INV-1043", "page": 1,
                                                  "quote": "رقم الفاتورة: INV-1043"},
                               "total": {"found": True, "value": "4,800.00", "page": 1,
                                         "quote": "المبلغ المستحق: ٤٬٨٠٠٫٠٠ دولار"}})

        ex, provider = make(llm)
        ref = ex.file("scan.pdf", SCANNED_PDF)
        result, status = invoke(agents, ex, "read_extract", {"file_ref": ref, "read_pictures": True, "fields": [
            {"name": "invoice_number"}, {"name": "total"}]})
        assert status == "success", result
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["invoice_number"]["verified"] is True and by_name["invoice_number"]["from_picture"] is True
        # Arabic-Indic digits with ٬ and ٫ match the value as the model wrote it.
        assert by_name["total"]["verified"] is True
        assert result["whole_document_read"] is True

    def test_a_decks_picture_and_chart_are_read(self, agents):
        from PIL import Image
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches

        picture = io.BytesIO()
        Image.new("RGB", (300, 200), "white").save(picture, "PNG")
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "المبيعات حسب المنطقة"
        data = CategoryChartData()
        data.categories = ["الشمال", "الجنوب"]
        data.add_series("2026", (128, 96.5))
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(6), Inches(4), data)
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(picture.getvalue()), Inches(1), Inches(1))
        buffer = io.BytesIO()
        deck.save(buffer)
        sent = []
        ex, _ = make(lambda m, t, p: sent.extend(p) or "مخطط بلا أرقام")
        ref = ex.file("sales.pptx", buffer.getvalue())
        info, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert info["unit"] == "slide" and info["chart_slides"] == [1] and info["pictures"] == [{"page": 2,
                                                                                               "count": 1}]
        assert info["unreadable_pages"] == [2]                   # a slide of nothing but a picture
        text, _ = invoke(agents, ex, "read_text", {"file_ref": ref})
        first = text["pages"][0]["text"]
        assert "categories: الشمال | الجنوب" in first and "2026: 128 | 96.5" in first
        result, _ = invoke(agents, ex, "read_pictures", {"file_ref": ref, "mode": "describe"})
        assert result["readings"][0]["page"] == 2 and len(sent) == 1


class TestExtracting:
    def test_fields_come_with_verified_quotes_and_values(self, agents, quotes):
        ex, provider, a, _ = quotes

        def llm(messages, max_tokens, pictures):
            return json.dumps({
                "unit_price": {"found": True, "value": "USD 185.00", "page": 1,
                               "quote": "Unit price USD 185.00, total USD 3,700.00"},
                "delivery": {"found": True, "value": "15 working days",            # not what the quote says
                             "quote": "Delivery within 10 working days of order."},
                "installation": {"found": False},
                "warranty": {"found": True, "value": "10 years", "quote": "Warranty 10 years on everything."}})

        ex.sinks.llm = llm
        result, status = invoke(agents, ex, "read_extract", {"file_ref": a, "fields": [
            {"name": "unit_price"}, {"name": "delivery"}, {"name": "installation"}, {"name": "warranty"}]})
        assert status == "success", result
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["unit_price"]["verified"] is True and by_name["unit_price"]["page"] == 1
        assert by_name["delivery"]["verified"] is False and by_name["delivery"]["value_in_quote"] is False
        assert by_name["warranty"]["quote_in_text"] is False
        assert by_name["installation"] == {"name": "installation", "found": False, "status": "not_stated"}
        record = provider.data["extract"][result["extract_ref"]]
        assert record["keys"]["verified"] == 1 and "fields" in record["values"]

    def test_a_long_document_is_read_in_windows_to_its_end(self, agents):
        """1.0 read the first 30,000 characters and called the rest "not stated"."""
        ex, _ = make()
        parts = [f"المادة {n}. " + ("شروط عامة " * 70) for n in range(1, 60)]
        parts.append("المادة 60. مدة الإشعار تسعون (٩٠) يوماً قبل تاريخ التجديد.")
        ref = ex.file("lease.txt", "\n".join(parts))
        windows = []

        def llm(messages, max_tokens, pictures):
            prompt = messages[-1]["text"]
            windows.append(prompt)
            if "مدة الإشعار تسعون" in prompt:
                return json.dumps({"notice": {"found": True, "value": "تسعون (٩٠) يوماً",
                                              "quote": "مدة الإشعار تسعون (90) يوماً"}})
            return json.dumps({name: {"found": False} for name in fields_asked(prompt)})

        ex.sinks.llm = llm
        result, status = invoke(agents, ex, "read_extract", {"file_ref": ref, "fields": [
            {"name": "notice"}, {"name": "governing_law"}]})
        assert status == "success", result
        assert len(windows) > 1 and result["whole_document_read"] is True
        notice = next(f for f in result["fields"] if f["name"] == "notice")
        assert notice["verified"] is True and notice["window"] == len(windows)
        assert notice["page"] == int(notice["window_pages"].split("-")[-1])
        law = next(f for f in result["fields"] if f["name"] == "governing_law")
        assert law["status"] == "not_stated"

    def test_text_never_read_is_not_read_never_not_stated(self, agents):
        ex, _ = make(lambda m, t, p: json.dumps({"notice": {"found": False}}))
        ref = ex.file("lease.txt", "\n".join(f"Clause {n}. " + "general terms " * 80 for n in range(80)))
        result, _ = invoke(agents, ex, "read_extract", {"file_ref": ref, "max_chars": 4000, "max_windows": 1,
                                                        "fields": [{"name": "notice"}]})
        field = result["fields"][0]
        assert field["status"] == "not_read" and field["unread_pages"] and result["whole_document_read"] is False
        assert [w["status"] for w in result["windows"]][:2] == ["read", "not_reached"]

    def test_a_refused_or_cut_window_is_said_and_its_pages_unread(self, agents):
        replies = iter([("", "refusal"), ('{"notice": {"found": fal', "length")])
        ex, _ = make(lambda m, t, p: next(replies))
        ref = ex.file("lease.txt", "\n".join(f"Clause {n}. " + "general terms " * 80 for n in range(8)))
        result, _ = invoke(agents, ex, "read_extract", {"file_ref": ref, "max_chars": 4000, "max_windows": 2,
                                                        "fields": [{"name": "notice"}]})
        assert result["refused"] is True and result["cut"] is True
        assert [w["status"] for w in result["windows"][:2]] == ["refused", "cut"]
        assert result["fields"][0]["status"] == "not_read"

    def test_a_scanned_page_left_unread_makes_absent_fields_not_read(self, agents):
        from pypdf import PdfReader, PdfWriter

        ex, _ = make(lambda m, t, p: json.dumps({"total": {"found": False}}))
        a, raw = produced(agents, ex, "produce_pdf", QUOTE_A)
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(raw)))
        writer.append(PdfReader(io.BytesIO(SCANNED_PDF)))
        buffer = io.BytesIO()
        writer.write(buffer)
        result, _ = invoke(agents, ex, "read_extract", {"file_ref": ex.file("mixed.pdf", buffer.getvalue()),
                                                        "fields": [{"name": "total"}]})
        assert result["fields"][0] == {"name": "total", "found": False, "status": "not_read", "unread_pages": [2]}

    def test_extraction_needs_the_chats_model(self, agents, quotes):
        ex, _, a, _ = quotes
        result, status = invoke(agents, ex, "read_extract", {"file_ref": a, "fields": [{"name": "total"}]})
        assert status == "error" and "model" in result["error"].lower()


class TestComparing:
    def test_changes_carry_pages_and_focus_words_come_first(self, agents, quotes):
        ex, _, a, b = quotes
        result, status = invoke(agents, ex, "compare_versions", {"file_ref_a": a, "file_ref_b": b,
                                                                 "focus": ["price", "delivery"]})
        assert status == "success", result
        assert result["changed"] >= 2 and result["added"] >= 1 and result["changes"][0]["focus"] is True
        texts = [(c["kind"], c["before"], c["after"]) for c in result["changes"]]
        assert any(k == "changed" and "185.00" in bf and "179.00" in af for k, bf, af in texts)
        assert any(k == "added" and "valid until" in af for k, bf, af in texts)

    def test_arabic_versions_are_compared_with_arabic_focus(self, agents):
        ex, _ = make()
        a = ex.file("v1.md", "# العرض\nالسعر 185 درهم للكرسي.\nالتسليم خلال 10 أيام.")
        b = ex.file("v2.md", "# العرض\nالسعر 179 درهم للكرسي.\nالتسليم خلال 10 أيام.")
        result, _ = invoke(agents, ex, "compare_versions", {"file_ref_a": a, "file_ref_b": b, "focus": ["السعر"]})
        assert result["changed"] == 1 and result["focus_hits"] == 1 and "179" in result["changes"][0]["after"]


class TestTemplates:
    def test_a_word_template_is_filled_in_body_tables_and_header_with_arabic_names(self, agents):
        from docx import Document

        document = Document()
        document.sections[0].header.paragraphs[0].text = "عرض سعر رقم {{رقم_العرض}}"
        document.add_paragraph("مقدم إلى {{العميل}} بتاريخ {{date}}.")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "{{item}}", "{{qty}}"
        document.add_paragraph("Valid {{validity}} days.")
        buffer = io.BytesIO()
        document.save(buffer)
        ex, _ = make()
        template = ex.file("template.docx", buffer.getvalue())
        result, raw = produced(agents, ex, "produce_fill_template", {
            "template_ref": template, "filename": "عرض-هاربورلاين",
            "values": {"العميل": "هاربورلاين للخدمات اللوجستية", "رقم_العرض": "SQ-2026-118",
                       "date": "٧ سبتمبر ٢٠٢٦", "item": "كرسي آريا", "qty": 20, "unused": "x"}})
        assert result["filled"] == sorted(["العميل", "رقم_العرض", "date", "item", "qty"])
        assert result["unfilled"] == ["validity"] and result["unused"] == ["unused"]
        pages, _ = all_text(agents, ex, ex.file("out.docx", raw))
        text = pages[0]["text"]
        assert "[header] عرض سعر رقم SQ-2026-118" in text and "مقدم إلى هاربورلاين للخدمات اللوجستية" in text
        assert "كرسي آريا | 20" in text and "Valid {{validity}} days." in text
        body = zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml").decode()
        assert "<w:bidi/>" in body

    def test_a_deck_template_is_filled_notes_included(self, agents):
        from pptx import Presentation

        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = "Review for {{customer}}"
        slide.placeholders[1].text = "Total {{total}}"
        slide.notes_slide.notes_text_frame.text = "Mention {{contact}}"
        buffer = io.BytesIO()
        deck.save(buffer)
        ex, _ = make()
        result, raw = produced(agents, ex, "produce_fill_template", {
            "template_ref": ex.file("t.pptx", buffer.getvalue()), "filename": "review",
            "values": {"customer": "سدرة", "total": "4,800.00", "contact": "Dana"}})
        assert result["filename"] == "review.pptx" and result["unfilled"] == []
        text, _ = invoke(agents, ex, "read_text", {"file_ref": ex.file("r.pptx", raw)})
        assert "# Review for سدرة" in text["pages"][0]["text"] and "[notes] Mention Dana" in text["pages"][0]["text"]


class TestDecks:
    def test_the_sample_deck_is_read_with_its_table_and_notes(self, agents):
        ex, _ = make()
        raw = (agents["documents"].folder / "samples" / "sidra-q3-review.pptx").read_bytes()
        ref = ex.file("sidra-q3-review.pptx", raw)
        info, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert info["kind"] == "pptx" and info["pages"] == 5 and info["has_notes"] is True
        text, _ = invoke(agents, ex, "read_text", {"file_ref": ref, "from_page": 3})
        assert "Region | Orders | Value USD" in text["pages"][0]["text"] and "[notes] Coast is new" in \
               text["pages"][0]["text"]

    def test_a_deck_is_16_9_and_a_long_arabic_table_continues_without_overlap(self, agents):
        """1.0 made 4:3 decks, and a 40-row table ran off the slide under the bullets."""
        ex, provider = make()
        rows = [[f"فرع {n}", f"{n * 1000:,}.00", "دبي" if n % 2 else "الشارقة"] for n in range(1, 61)]
        result, raw = produced(agents, ex, "produce_pptx", {
            "filename": "مبيعات", "title": "مراجعة الربع الثالث", "subtitle": "سدرة للوازم المكتبية",
            "slides": [
                {"title": "المبيعات حسب الفرع", "bullets": ["ارتفعت المبيعات ١٢٪ عن الربع الثاني.",
                                                           "فرعان جديدان في الشارقة."],
                 "table_columns": ["الفرع", "المبيعات (درهم)", "الإمارة"], "table_rows": rows,
                 "notes": "لا تعد بتحديث آريا قبل يناير."},
                {"title": "الأسئلة"}]})
        assert result["verified"] is True and "layout_problems" not in result
        assert result["continued_slides"] and result["slides"] >= 5
        from pptx import Presentation

        deck = Presentation(io.BytesIO(raw))
        assert (deck.slide_width, deck.slide_height) == (int(13.333 * 914400), 6858000)
        tables = [shape.table for slide in deck.slides for shape in slide.shapes if shape.has_table]
        assert len(tables) >= 3 and sum(len(t.rows) - 1 for t in tables) == 60
        assert all(t.cell(0, 0).text == "الفرع" for t in tables)          # the header row on every slide
        assert all(t._tbl.tblPr.get("rtl") == "1" for t in tables)
        assert provider.data["output"][result["output_ref"]]["keys"]["kind"] == "pptx"
        titles = [slide.shapes.title.text for slide in deck.slides if slide.shapes.title is not None]
        assert "المبيعات حسب الفرع (تابع)" in titles

    def test_many_bullets_continue_rather_than_shrink_away(self, agents):
        ex, _ = make()
        bullets = [f"Point {n}: " + "a reasonably long sentence about the quarter " * 3 for n in range(30)]
        result, raw = produced(agents, ex, "produce_pptx", {"filename": "d", "title": "Review", "slides": [
            {"title": "Everything", "bullets": bullets}]})
        assert result["verified"] is True and result["slides"] > 2
        text, _ = invoke(agents, ex, "read_text", {"file_ref": ex.file("d.pptx", raw), "max_chars": 60000})
        assert all(f"Point {n}:" in "".join(p["text"] for p in text["pages"]) for n in range(30))

    def test_a_template_deck_keeps_its_size_and_drops_its_slides(self, agents):
        raw = (agents["documents"].folder / "samples" / "sidra-q3-review.pptx").read_bytes()
        ex, _ = make()
        result, made = produced(agents, ex, "produce_pptx", {
            "filename": "templated", "title": "New", "template_ref": ex.file("t.pptx", raw),
            "slides": [{"title": "One", "bullets": ["a", "b"], "table_columns": ["x", "y"],
                        "table_rows": [["1", "2"], ["3", "4"], ["5", "6"]]}]})
        from pptx import Presentation

        assert result["templated"] is True and result["slides"] == 2 and result["verified"] is True
        assert Presentation(io.BytesIO(made)).slide_width == Presentation(io.BytesIO(raw)).slide_width


class TestPdfPages:
    def test_merge_split_and_rotate(self, agents, quotes):
        ex, provider, a, b = quotes
        merged, raw = produced(agents, ex, "pdf_merge", {"file_refs": [a, b, a], "filename": "دمج"})
        assert merged["pages"] == 3 and merged["filename"] == "دمج.pdf"
        whole = ex.file("merged.pdf", raw)
        split, status = invoke(agents, ex, "pdf_split", {"file_ref": whole, "ranges": ["1-2", "3"]})
        assert status == "success", split
        assert [f["pages"] for f in split["files"]] == [2, 1]
        assert [f["filename"] for f in split["files"]] == ["merged-pages-1-2.pdf", "merged-pages-3.pdf"]
        refused, status = invoke(agents, ex, "pdf_split", {"file_ref": whole, "ranges": ["2-9"]})
        assert status == "error" and "3 page(s)" in refused["error"]
        turned, raw = produced(agents, ex, "pdf_rotate", {"file_ref": whole, "degrees": 90, "pages": "2"})
        from pypdf import PdfReader

        assert [p.rotation for p in PdfReader(io.BytesIO(raw)).pages] == [0, 90, 0]
        operations = sorted(r["keys"]["operation"] for r in provider.data["output"].values())
        assert operations.count("split") == 2 and "merge" in operations and "rotate" in operations

    def test_a_form_is_listed_and_filled_in_arabic(self, agents):
        ex, provider = make()
        ref = ex.file("form.pdf", form_pdf())
        fields, status = invoke(agents, ex, "pdf_fields", {"file_ref": ref})
        assert status == "success", fields
        by_name = {f["name"]: f for f in fields["fields"]}
        assert by_name["company"]["type"] == "text" and by_name["company"]["required"] is True
        assert by_name["agree"]["type"] == "checkbox" and by_name["agree"]["options"] == ["Yes"]
        assert by_name["emirate"]["options"] == ["Dubai", "Sharjah"]
        wrong, status = invoke(agents, ex, "pdf_fill", {"file_ref": ref, "values": {"emirate": "Abu Dhabi"}})
        assert status == "error" and "Nothing was filled" in wrong["error"] and not provider.data.get("output")
        unknown, status = invoke(agents, ex, "pdf_fill", {"file_ref": ref, "values": {"vat": "1"}})
        assert status == "error" and "no field named vat" in unknown["error"]
        result, raw = produced(agents, ex, "pdf_fill", {"file_ref": ref, "values": {
            "company": "سدرة للوازم المكتبية ذ.م.م.", "agree": True}})
        assert result["filled"] == ["agree", "company"] and result["unfilled"] == ["emirate"]
        from pypdf import PdfReader

        back = PdfReader(io.BytesIO(raw)).get_fields()
        assert back["company"]["/V"] == "سدرة للوازم المكتبية ذ.م.م." and back["agree"]["/V"] == "/Yes"

    def test_a_pdf_with_no_form_says_so(self, agents):
        ex, _ = make()
        result, _ = invoke(agents, ex, "pdf_fields", {"file_ref": ex.file("scan.pdf", SCANNED_PDF)})
        assert result["count"] == 0 and "no form fields" in result["problem"]
