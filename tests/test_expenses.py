"""The Expenses agent, its program started and spoken to in the standard's messages: Omar's trip receipts for
Sidra Office Supplies — text receipts, one photo, one duplicate, one over the
meal allowance, one in another currency — and the company policy."""

import io
import json

from openpyxl import load_workbook

from tests.harness import Chat, ChatSinks, Records, probe, run


RECEIPTS = {
    "lunch-0907.txt": "SHORELINE CAFE\nDubai Marina\n07 Sep 2026 13:12\nGrilled fish 48.00\nWater 7.00\nTOTAL AED 55.00\nPaid card",
    "dinner-0907.txt": "The Copper Pot\n2026-09-07\n2 x main course 140.00\nDessert 32.00\nService 17.20\nTotal USD 189.20",
    "taxi-0908.txt": "CAREEM RECEIPT\n8 September 2026\nTrip: Marina -> Harbourline offices\nFare USD 24.50\nTotal USD 24.50",
    "hotel-0908.txt": "Harbour View Hotel\nCheck-in 07/09/2026  Check-out 08/09/2026\nRoom 1 night 210.00\nCity tax 12.00\nAmount due USD 222.00",
    "dinner-0907-again.txt": "The Copper Pot\n2026-09-07\n2 x main course 140.00\nDessert 32.00\nService 17.20\nTotal USD 189.20",
    "stationery-0909.txt": "OfficeMart\nSep 9, 2026\nNotebooks 18.00\nPens 6.50\nTotal 24.50",
}
# Read by itself, to see the receipt line: the model misreads its total.
PARKING = "City Parking\nDubai Marina\n2026-09-08\nAll day\nTotal USD 30.00"
PHOTO = b"\xff\xd8\xff\xe0\x00\x10JFIF fictional photo bytes"
POLICY = """Sidra Office Supplies Expense Policy (fictional)

1. Meals: employees may claim up to USD 60 per person per day for meals while travelling.
2. Hotels: accommodation up to USD 200 per night; anything above needs prior approval.
3. Receipts are required for any expense above USD 25.
4. Claims must be submitted within 30 days of the expense date.
5. Alcohol is not reimbursable.
"""

MODEL_ANSWERS = {
    "lunch-0907.txt": {"merchant": "Shoreline Cafe", "date": "07 Sep 2026", "currency": "AED",
                       "amount": "55.00", "category": "meals", "quote_amount": "TOTAL AED 55.00"},
    "dinner-0907.txt": {"merchant": "The Copper Pot", "date": "2026-09-07", "currency": "USD",
                        "amount": "189.20", "category": "meals", "quote_amount": "Total USD 189.20"},
    "taxi-0908.txt": {"merchant": "Careem", "date": "8 September 2026", "currency": "USD",
                      "amount": "24.50", "category": "transport", "quote_amount": "Total USD 24.50"},
    "hotel-0908.txt": {"merchant": "Harbour View Hotel", "date": "08/09/2026", "currency": "USD",
                       "amount": "222.00", "category": "accommodation", "quote_amount": "Amount due USD 222.00"},
    "dinner-0907-again.txt": {"merchant": "The Copper Pot", "date": "2026-09-07", "currency": "USD",
                              "amount": "189.20", "category": "meals", "quote_amount": "Total USD 189.20"},
    "parking-0908.txt": {"merchant": "City Parking", "date": "2026-09-08", "currency": "USD",
                         "amount": "31.00", "category": "transport", "quote_amount": "Total USD 30.00"},
    # The model misreads the stationery total and names no currency.
    "stationery-0909.txt": {"merchant": "OfficeMart", "date": "Sep 9, 2026", "currency": "",
                            "amount": "25.40", "category": "office", "quote_amount": "Total 24.50"},
}
POLICY_ANSWER = {
    "meal_max_per_day": {"value": "60", "quote": "employees may claim up to USD 60 per person per day for meals while travelling."},
    "hotel_max_per_night": {"value": "200", "quote": "accommodation up to USD 200 per night"},
    "receipt_required_above": {"value": "25", "quote": "Receipts are required for any expense above USD 25."},
    "claim_within_days": {"value": "30", "quote": "Claims must be submitted within 30 days of the expense date."},
    "alcohol_allowed": {"value": "no", "quote": "Alcohol is never ever reimbursable."},   # misquoted
    "taxi_max_per_trip": {"found": False},
}


#: What the model reads off each picture, by the file's name; None for a refusal.
PICTURE_TEXT = {}
#: Receipts the model's provider refuses to read, by name.
REFUSED = set()
#: Every model call's pictures, as the runtime passed them.
SEEN_PICTURES = []
#: Policy sentences the model finds when they are in the part it was given: name -> (value, quote).
SENTENCES = {}


async def model(messages, max_tokens, pictures):
    system = messages[0]["text"] if messages[0]["role"] == "system" else ""
    prompt = messages[-1]["text"]
    SEEN_PICTURES.append(list(pictures))
    if system.startswith("You transcribe"):
        name = next((n for n in PICTURE_TEXT if n in prompt), None)
        text = PICTURE_TEXT.get(name) if name else "NONE"
        return ("", "refusal") if text is None else text
    if prompt.startswith("POLICY"):
        if "expense-policy.txt" in prompt:
            return json.dumps(POLICY_ANSWER)
        if "refusing-policy.txt" in prompt and "part 2 of" in prompt:
            return "", "refusal"
        return json.dumps({name: ({"value": value, "quote": quote} if quote in prompt else {"found": False})
                           for name, (value, quote) in SENTENCES.items()})
    for name, answer in MODEL_ANSWERS.items():
        if f"({name})" in prompt and prompt.startswith("RECEIPT"):
            if name in REFUSED:
                return "", "refusal"
            return json.dumps(answer, ensure_ascii=False)
    return "{}"


def make(answers=None):
    """A chat whose person answers each question with the first of ``answers`` that is among its choices (or None,
    as nobody answering), and whose model is the fake above."""
    provider = Records()
    asked = []

    def ask(text, choices, expects):
        asked.append(text)
        return next((a for a in (answers or []) if a in choices), None)

    chat = Chat(provider, sinks=ChatSinks(llm=model, ask=ask))
    chat.asked = asked
    return chat, provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["expenses"], name, inputs, chat_level=chat_level))


def upload(ex, filename, raw: bytes) -> str:
    """A file the person attaches to the chat: its ref, which a call names."""
    return ex.file(filename, raw)


def trip(agents, ex, names=None):
    claim, _ = invoke(agents, ex, "claims_create", {"title": "Dubai trip, 7–9 Sep", "claimant": "Omar", "currency": "USD"})
    read = {}
    for name in (RECEIPTS if names is None else names):
        ref = upload(ex, name, RECEIPTS[name].encode())
        read[name], status = invoke(agents, ex, "receipts_read",
                                    {"claim_ref": claim["claim_ref"], "file_ref": ref})
        assert status == "success", read[name]
    return claim["claim_ref"], read


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        agent = agents["expenses"]
        assert probe(agent) == []


class TestReceipts:
    def test_facts_are_verified_against_the_receipt(self, agents):
        ex, _ = make()
        _, read = trip(agents, ex, ["dinner-0907.txt", "taxi-0908.txt", "hotel-0908.txt"])
        dinner = read["dinner-0907.txt"]
        assert dinner["status"] == "extracted"
        assert dinner["amount"] == 189.2 and dinner["currency"] == "USD" and dinner["date"] == "2026-09-07"
        assert set(dinner["verified"]) == {"merchant", "date", "amount", "currency"}
        assert dinner["assumed"] == ["category"]
        taxi = read["taxi-0908.txt"]
        assert taxi["date"] == "2026-09-08" and taxi["category"] == "transport"
        assert "merchant" in taxi["verified"]             # "Careem" appears in CAREEM RECEIPT
        # The hotel's dates are day/month-ambiguous (07/09/2026): not read,
        # never guessed — the receipt needs review for its date.
        hotel = read["hotel-0908.txt"]
        assert hotel["status"] == "needs_review" and hotel["date"] == ""
        assert "date not readable" in hotel["note"] and hotel["amount"] == 222.0

    def test_a_misread_amount_and_a_missing_currency_need_review(self, agents):
        ex, _ = make()
        _, read = trip(agents, ex, ["stationery-0909.txt"])
        item = read["stationery-0909.txt"]
        assert item["status"] == "needs_review"
        assert "amount" in item["assumed"]                # 25.40 is not on the receipt
        assert item["currency"] == "" and "currency not shown" in item["note"]
        assert item["date"] == "2026-09-09"

    def test_a_picture_that_cant_be_opened_is_manual_and_confirm_fills_it(self, agents):
        ex, _ = make()
        claim_ref, _ = trip(agents, ex, [])
        ref = upload(ex, "parking.jpg", PHOTO)
        result, status = invoke(agents, ex, "receipts_read", {"claim_ref": claim_ref, "file_ref": ref})
        assert status == "success" and result["status"] == "manual"
        assert "picture could not be opened" in result["note"] and result["read_from"] == "text"
        refused, status = invoke(agents, ex, "receipts_confirm", {
            "receipt_ref": result["receipt_ref"], "merchant": "Marina Parking", "amount": 8})
        assert status == "error" and "date" in refused["error"]
        confirmed, status = invoke(agents, ex, "receipts_confirm", {
            "receipt_ref": result["receipt_ref"], "merchant": "Marina Parking", "date": "2026-09-08",
            "currency": "usd", "amount": 8, "category": "transport"})
        assert status == "success", confirmed
        assert confirmed["status"] == "confirmed" and confirmed["currency"] == "USD"
        got, _ = invoke(agents, ex, "claims_get", {"claim_ref": claim_ref})
        assert got["totals"] == [{"currency": "USD", "amount": 8.0, "receipts": 1}]
        assert got["needs_attention"] == 0

    def test_a_duplicate_is_seen_on_read(self, agents):
        ex, _ = make()
        _, read = trip(agents, ex, ["dinner-0907.txt", "dinner-0907-again.txt"])
        again = read["dinner-0907-again.txt"]
        assert again["status"] == "duplicate"
        assert again["duplicate_of"] == read["dinner-0907.txt"]["receipt_ref"]


class TestPolicy:
    def test_rules_come_with_verified_quotes_and_honest_gaps(self, agents):
        ex, _ = make()
        ref = upload(ex, "expense-policy.txt", POLICY.encode())
        result, status = invoke(agents, ex, "policies_load", {"file_ref": ref})
        assert status == "success", result
        by_name = {r["name"]: r for r in result["rules"]}
        assert by_name["meal_max_per_day"]["value"] == "60" and by_name["meal_max_per_day"]["verified"] is True
        assert by_name["alcohol_allowed"]["verified"] is False       # the quote is not in the policy
        assert result["not_found"] == ["taxi_max_per_trip"]
        assert result["unverified"] == ["alcohol_allowed"]
        # A person sets what the policy lacks, and it replaces nothing else.
        set_, _ = invoke(agents, ex, "policies_set_rule", {"name": "taxi_max_per_trip", "value": "40"})
        rules, _ = invoke(agents, ex, "policies_get", {})
        assert {r["name"]: r["value"] for r in rules["rules"]}["taxi_max_per_trip"] == "40"
        assert len(rules["rules"]) == 6
        # Loading again replaces, never duplicates.
        invoke(agents, ex, "policies_load", {"file_ref": ref})
        rules, _ = invoke(agents, ex, "policies_get", {})
        assert len(rules["rules"]) == 6


class TestTheClaim:
    def test_a_claim_that_exists_is_found_by_listing(self, agents):
        ex, _ = make()
        claim_ref, _ = trip(agents, ex, names=[])
        result, status = invoke(agents, ex, "claims_list", {})
        assert status == "success", result
        assert [(c["claim_ref"], c["title"], c["status"]) for c in result["claims"]] == [
            (claim_ref, "Dubai trip, 7–9 Sep", "draft")]
        none, _ = invoke(agents, ex, "claims_list", {"status": "paid"})
        assert none["claims"] == []

    def test_check_flags_duplicates_allowance_currency_and_review(self, agents):
        ex, _ = make()
        policy = upload(ex, "expense-policy.txt", POLICY.encode())
        invoke(agents, ex, "policies_load", {"file_ref": policy})
        claim_ref, read = trip(agents, ex)
        result, status = invoke(agents, ex, "claims_check", {"claim_ref": claim_ref, "as_of": "2026-09-12"})
        assert status == "success", result
        kinds = {(e["filename"], e["kind"]) for e in result["exceptions"]}
        assert ("dinner-0907-again.txt", "duplicate") in kinds
        assert ("lunch-0907.txt", "currency") in kinds                  # AED, not converted
        assert ("dinner-0907.txt", "policy") in kinds                    # 189.20 > 60 meals per day
        assert ("hotel-0908.txt", "policy") in kinds                     # 222 > 200 per night
        assert ("stationery-0909.txt", "needs_review") in kinds
        # Amounts the receipts show pass the receipt line, whatever their size.
        assert not any("receipt_required_above" in e["detail"] for e in result["exceptions"])
        assert result["receipts"] == 6 and result["ok"] == 1            # only the taxi
        assert "meal_max_per_day = 60" in result["rules_applied"]
        detail = next(e["detail"] for e in result["exceptions"]
                      if e["filename"] == "hotel-0908.txt" and e["kind"] == "policy")
        assert detail == "accommodation 222.0 exceeds hotel_max_per_night 200.0"

    def test_an_amount_above_the_receipt_line_must_be_on_the_receipt(self, agents):
        ex, _ = make()
        policy = upload(ex, "expense-policy.txt", POLICY.encode())
        invoke(agents, ex, "policies_load", {"file_ref": policy})
        claim, _ = invoke(agents, ex, "claims_create", {"title": "Parking", "claimant": "Omar", "currency": "USD"})
        ref = upload(ex, "parking-0908.txt", PARKING.encode())
        read, status = invoke(agents, ex, "receipts_read",
                              {"claim_ref": claim["claim_ref"], "file_ref": ref})
        assert status == "success", read
        result, status = invoke(agents, ex, "claims_check",
                                {"claim_ref": claim["claim_ref"], "as_of": "2026-09-12"})
        assert status == "success", result
        # 31.00 is the model's, not the receipt's (it says 30.00), and is
        # over the 25 above which the policy wants the receipt to show it.
        details = [e["detail"] for e in result["exceptions"] if e["kind"] == "policy"]
        assert any("above receipt_required_above 25" in d and "does not show" in d for d in details), details

        # Confirmed by a person, the amount is proven, and the line passes.
        invoke(agents, ex, "receipts_confirm",
               {"receipt_ref": read["receipt_ref"], "currency": "USD", "amount": 30})
        again, _ = invoke(agents, ex, "claims_check",
                          {"claim_ref": claim["claim_ref"], "as_of": "2026-09-12"})
        assert not any("receipt_required_above" in e["detail"] for e in again["exceptions"])

    def test_the_workbook_and_the_status_rules(self, agents):
        ex, _ = make(["Submit"])
        claim_ref, read = trip(agents, ex, ["dinner-0907.txt", "taxi-0908.txt", "stationery-0909.txt"])
        made, status = invoke(agents, ex, "claims_workbook",
                              {"claim_ref": claim_ref, "as_of": "2026-09-12"}, chat_level=2)
        assert status == "success", made
        assert made["receipts"] == 3 and made["exceptions"] == 1 and made["verified"] is True
        raw = ex.files[made["file_ref"]]["content"]
        wb = load_workbook(io.BytesIO(raw))
        assert wb.sheetnames == ["Receipts", "Summary", "Exceptions", "Rules applied"]
        rows = list(wb["Receipts"].iter_rows(values_only=True))
        assert rows[3][:6] == ("Date", "Merchant", "Category", "Currency", "Amount", "Status")
        assert any(r[1] == "OfficeMart" and r[5] == "needs_review" for r in rows[4:])
        summary = list(wb["Summary"].iter_rows(values_only=True))
        assert ("meals", "USD", 1, 189.2) in summary

        # Cannot submit while a receipt needs review; can after it is confirmed.
        refused, status = invoke(agents, ex, "claims_set_status", {"claim_ref": claim_ref, "status": "submitted"})
        assert status == "error" and "stationery-0909.txt" in refused["error"]
        invoke(agents, ex, "receipts_confirm", {
            "receipt_ref": read["stationery-0909.txt"]["receipt_ref"], "currency": "USD", "amount": 24.5})
        submitted, status = invoke(agents, ex, "claims_set_status", {"claim_ref": claim_ref, "status": "submitted"})
        assert status == "success" and submitted["status"] == "submitted"
        # Approval needs a reviewer's name; paid needs approval first.
        refused, status = invoke(agents, ex, "claims_set_status", {"claim_ref": claim_ref, "status": "approved"})
        assert status == "error" and "reviewer" in refused["error"]
        refused, status = invoke(agents, ex, "claims_set_status", {"claim_ref": claim_ref, "status": "paid", "by": "Finance"})
        assert status == "error" and "cannot become paid" in refused["error"]
        approved, _ = invoke(agents, ex, "claims_set_status", {
            "claim_ref": claim_ref, "status": "approved", "by": "Dana Whitlock", "on_date": "2026-09-14"})
        assert approved["decided_by"] == "Dana Whitlock" and approved["decided_on"] == "2026-09-14"
        # No more receipts into a submitted claim.
        ref = upload(ex, "late.txt", RECEIPTS["taxi-0908.txt"].encode())
        late, status = invoke(agents, ex, "receipts_read", {"claim_ref": claim_ref, "file_ref": ref})
        assert status == "error" and "approved" in late["error"]


# -- Arabic, the decimal comma, pictures, duplicates by when they were added, deleting, submitting --------------------

KARAK = ("مقهى الكرك الذهبي\nشارع الرقة، ديرة، دبي\nفاتورة رقم ٤٤١٢\nالتاريخ: ٣ سبتمبر ٢٠٢٦   ٠٧:٤٥\n\n"
         "شاي كرك × ٢              ٧٫٠٠\nخبز رقاق بالجبن           ٥٫٥٠\nالمجموع                  ١٢٫٥٠ د.إ\nمدفوع نقداً")
_KARAK_ANSWER = {"merchant": "مقهى الكرك الذهبي", "date": "٣ سبتمبر ٢٠٢٦", "currency": "AED", "amount": "١٢٫٥٠",
                 "category": "meals", "quote_amount": "المجموع ١٢٫٥٠ د.إ"}
MODEL_ANSWERS.update({
    "karak.txt": _KARAK_ANSWER,
    "karak-cp1256.txt": {**_KARAK_ANSWER, "date": "3 سبتمبر 2026", "amount": "12.50"},
    "karak.jpg": _KARAK_ANSWER,
    # The model "reads" a total the picture's own text does not show.
    "shawarma.png": {"merchant": "مطعم الشاورما", "date": "2026-09-04", "currency": "AED", "amount": "19,50",
                     "category": "meals", "quote_amount": "المجموع 18,50 درهم"},
    "scan.pdf": {"merchant": "صيدلية النور", "date": "2026-09-05", "currency": "SAR", "amount": "1.234,50",
                 "category": "other", "quote_amount": "الإجمالي 1.234,50 ر.س"},
    "refused.jpg": {"merchant": "x", "date": "2026-09-05", "currency": "AED", "amount": "1", "category": "other"},
    "lumiere.txt": {"merchant": "Café Lumière", "date": "2026-09-04", "currency": "EUR", "amount": "12,50",
                    "category": "meals", "quote_amount": "Total EUR 12,50"},
    "diner.txt": {"merchant": "Joe's Diner", "date": "2026-09-05", "currency": "USD", "amount": "24.50",
                  "category": "meals", "quote_amount": "Total $24.50"},
    "refused-text.txt": {"merchant": "x"},
    "souq.txt": {"merchant": "سوق المدينة", "date": "the third of September", "currency": "SAR", "amount": "45,00",
                 "category": "office", "quote_amount": "المجموع ٤٥٫٠٠ ر.س"},
})
PICTURE_TEXT.update({
    "karak.jpg": KARAK,
    "shawarma.png": "مطعم الشاورما\n2026-09-04\nشاورما دجاج 12,00\nعصير 6,50\nالمجموع 18,50 درهم",
    "scan.pdf": "صيدلية النور\nالرياض\n2026-09-05\nأدوية 1.200,00\nضريبة 34,50\nالإجمالي 1.234,50 ر.س",
    "refused.jpg": None,
    "blank.jpg": "NONE",
})


def photo(kind="JPEG", size=(480, 640)) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle([40, 40, size[0] - 40, 120], outline="black")
    buffer = io.BytesIO()
    image.save(buffer, kind)
    return buffer.getvalue()


def claim_of(agents, ex, currency="AED", title="رحلة دبي"):
    made, status = invoke(agents, ex, "claims_create", {"title": title, "claimant": "سامي", "currency": currency})
    assert status == "success", made
    return made["claim_ref"]


def read(agents, ex, claim_ref, name, raw, **more):
    ref = upload(ex, name, raw if isinstance(raw, bytes) else raw.encode())
    result, status = invoke(agents, ex, "receipts_read", {"claim_ref": claim_ref, "file_ref": ref, **more})
    assert status == "success", result
    return result


class TestArabicAndTheDecimalComma:
    def test_an_arabic_receipt_with_arabic_digits_is_read_and_verified(self, agents):
        ex, _ = make()
        result = read(agents, ex, claim_of(agents, ex), "karak.txt", KARAK)
        assert result["status"] == "extracted", result
        assert (result["merchant"], result["date"], result["currency"], result["amount"]) == (
            "مقهى الكرك الذهبي", "2026-09-03", "AED", 12.5)
        assert set(result["verified"]) == {"merchant", "date", "currency", "amount"}
        assert result["read_from"] == "text"

    def test_a_windows_arabic_receipt_is_decoded(self, agents):
        ex, _ = make()
        text = KARAK.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789."))
        result = read(agents, ex, claim_of(agents, ex), "karak-cp1256.txt", text.encode("cp1256"))
        assert result["merchant"] == "مقهى الكرك الذهبي" and result["amount"] == 12.5
        assert {"merchant", "date", "amount", "currency"} <= set(result["verified"])

    def test_twelve_fifty_with_a_comma_is_twelve_fifty(self, agents):
        """The bug: "12,50" was read as 1250."""
        ex, _ = make()
        receipt = "Café Lumière\nParis\n2026-09-04\nCroissant 3,50\nCafé 9,00\nTotal EUR 12,50"
        result = read(agents, ex, claim_of(agents, ex, "EUR"), "lumiere.txt", receipt)
        assert result["amount"] == 12.5 and "amount" in result["verified"] and result["status"] == "extracted"

    def test_a_person_confirms_an_amount_with_a_comma(self, agents):
        ex, _ = make()
        claim_ref = claim_of(agents, ex)
        result = read(agents, ex, claim_ref, "blank.jpg", photo())
        assert result["status"] == "manual" and result["read_from"] == "picture"
        confirmed, status = invoke(agents, ex, "receipts_confirm", {
            "receipt_ref": result["receipt_ref"], "merchant": "كافتيريا", "date": "2026-09-03", "currency": "AED",
            "amount": "12,50", "category": "meals"})
        assert status == "success", confirmed
        assert confirmed["amount"] == 12.5

    def test_a_date_the_model_paraphrased_is_never_verified(self, agents):
        ex, _ = make()
        receipt = "سوق المدينة\nالرياض\n٣ سبتمبر ٢٠٢٦\nدفاتر ٤٥٫٠٠\nالمجموع ٤٥٫٠٠ ر.س"
        result = read(agents, ex, claim_of(agents, ex, "SAR"), "souq.txt", receipt)
        assert result["date"] == "2026-09-03"
        assert "date" in result["assumed"] and "date" not in result["verified"]
        assert result["status"] == "needs_review" and "assumed" in result["note"]
        assert result["amount"] == 45.0 and "amount" in result["verified"]


class TestPictures:
    def test_a_photo_is_read_off_the_picture_and_checked_against_what_was_read(self, agents):
        ex, _ = make()
        SEEN_PICTURES.clear()
        result = read(agents, ex, claim_of(agents, ex), "karak.jpg", photo())
        assert result["read_from"] == "picture" and result["status"] == "extracted", result
        assert result["amount"] == 12.5 and set(result["verified"]) == {"merchant", "date", "currency", "amount"}
        # The model was shown the picture once, as a JPEG, to transcribe it; the fields are read from that text.
        assert [len(p) for p in SEEN_PICTURES] == [1, 0]
        assert SEEN_PICTURES[0][0][:3] == b"\xff\xd8\xff"

    def test_a_value_the_picture_does_not_show_is_assumed(self, agents):
        ex, _ = make()
        result = read(agents, ex, claim_of(agents, ex), "shawarma.png", photo("PNG"))
        assert result["read_from"] == "picture"
        assert "amount" in result["assumed"] and result["status"] == "needs_review"
        assert result["amount"] == 19.5

    def test_a_scanned_pdf_is_rendered_and_read(self, agents):
        ex, _ = make()
        SEEN_PICTURES.clear()
        result = read(agents, ex, claim_of(agents, ex, "SAR"), "scan.pdf", photo("PDF"))
        assert result["read_from"] == "picture", result
        assert result["amount"] == 1234.5 and result["currency"] == "SAR"
        assert set(result["verified"]) >= {"merchant", "amount", "currency"}
        assert len(SEEN_PICTURES[0]) == 1

    def test_a_refusal_is_said_and_the_receipt_is_manual(self, agents):
        ex, _ = make()
        claim_ref = claim_of(agents, ex)
        result = read(agents, ex, claim_ref, "refused.jpg", photo())
        assert result["status"] == "manual" and "refused" in result["note"]
        REFUSED.add("refused-text.txt")
        result = read(agents, ex, claim_ref, "refused-text.txt", "Some shop\n2026-09-05\nTotal AED 10.00")
        assert result["status"] == "manual" and "refused" in result["note"]

    def test_a_file_over_the_chat_limit_is_refused_before_it_is_read(self, agents):
        ex, _ = make()
        ref = upload(ex, "huge.txt", b"a" * (8 * 1024 * 1024))
        result, status = invoke(agents, ex, "receipts_read", {"claim_ref": claim_of(agents, ex), "file_ref": ref})
        assert status == "error" and result.get("kind") == "too_large", result
        assert "at most" in result["error"]


class TestCurrencies:
    def test_a_bare_dollar_is_never_taken_as_usd(self, agents):
        ex, _ = make()
        claim_ref = claim_of(agents, ex, "USD")
        receipt = "Joe's Diner\nToronto\n2026-09-05\nBurger 18.00\nCoffee 6.50\nTotal $24.50"
        result = read(agents, ex, claim_ref, "diner.txt", receipt)
        assert result["currency"] == "" and result["status"] == "needs_review"
        assert "$" in result["note"] and "USD, CAD" in result["note"]
        stated = read(agents, ex, claim_ref, "diner.txt", receipt, currency="CAD")
        assert stated["currency"] == "CAD" and "currency (as stated by the person)" in stated["verified"]

    def test_a_conversion_is_information_only(self, agents):
        ex, _ = make()
        claim_ref = claim_of(agents, ex, "USD")
        read(agents, ex, claim_ref, "karak.txt", KARAK)
        before, _ = invoke(agents, ex, "claims_get", {"claim_ref": claim_ref})
        got, status = invoke(agents, ex, "claims_get", {"claim_ref": claim_ref, "rates": {"AED": "0,2723"}})
        assert status == "success", got
        assert got["totals"] == before["totals"] == [{"currency": "AED", "amount": 12.5, "receipts": 1}]
        assert got["for_information"]["converted"] == [
            {"currency": "AED", "amount": 12.5, "rate": "0.2723", "converted": 3.4, "into": "USD"}]
        assert "information only" in got["for_information"]["note"]
        assert "for_information" not in before
        made, status = invoke(agents, ex, "claims_workbook",
                              {"claim_ref": claim_ref, "as_of": "2026-09-12", "rates": {"AED": 0.2723}})
        assert status == "success", made
        book = load_workbook(io.BytesIO(ex.files[made["file_ref"]]["content"]))
        assert "For information" in book.sheetnames
        assert any("مقهى الكرك الذهبي" in r for r in book["Receipts"].iter_rows(values_only=True))


def put(agents, provider, ref, fields):
    """A receipt as already recorded, under a ref of the test's choosing, after those before it."""
    _, resource = agents["expenses"].resource("receipt")
    keys, values = Records.split(resource, fields)
    provider.data.setdefault("receipt", {})[ref] = {"keys": keys, "values": values}


class TestDuplicates:
    def test_the_original_is_the_receipt_added_first_not_the_lower_ref(self, agents):
        ex, provider = make()
        claim_ref = claim_of(agents, ex)
        same = {"claim_ref": claim_ref, "file_ref": "f", "merchant": "مقهى الكرك الذهبي", "date": "2026-09-03",
                "currency": "AED", "amount": 12.5, "category": "meals", "status": "extracted",
                "read_status": "extracted", "verified": "merchant, date, amount, currency"}
        put(agents, provider, "receipt_z", {**same, "filename": "first.txt"})     # added first
        put(agents, provider, "receipt_a", {**same, "filename": "second.txt"})    # added later, sorts first
        result, status = invoke(agents, ex, "claims_check", {"claim_ref": claim_ref, "as_of": "2026-09-10"})
        assert status == "success", result
        duplicates = [(e["receipt_ref"], e["detail"]) for e in result["exceptions"] if e["kind"] == "duplicate"]
        assert duplicates == [("receipt_a", "same merchant, date and amount as receipt_z")]

        # The person says the second is its own expense (two karak breakfasts): cleared, and never matched again.
        cleared, status = invoke(agents, ex, "receipts_not_duplicate",
                                 {"receipt_ref": "receipt_a", "reason": "فطور لشخصين"})
        assert status == "success", cleared
        assert cleared["status"] == "extracted" and cleared["not_duplicate_of"] == ["receipt_z"]
        again, _ = invoke(agents, ex, "claims_check", {"claim_ref": claim_ref, "as_of": "2026-09-10"})
        assert not [e for e in again["exceptions"] if e["kind"] == "duplicate"]
        got, _ = invoke(agents, ex, "claims_get", {"claim_ref": claim_ref})
        assert got["totals"] == [{"currency": "AED", "amount": 25.0, "receipts": 2}]
        refused, status = invoke(agents, ex, "receipts_not_duplicate", {"receipt_ref": "receipt_a"})
        assert status == "error" and "not marked as a duplicate" in refused["error"]


class TestDeleting:
    def test_a_receipt_is_deleted_only_on_the_persons_word_and_its_copy_released(self, agents):
        answers = ["Keep it"]
        ex, provider = make(answers)
        claim_ref = claim_of(agents, ex)
        first = read(agents, ex, claim_ref, "karak.txt", KARAK)
        copy = read(agents, ex, claim_ref, "karak.txt", KARAK)
        assert copy["status"] == "duplicate" and copy["duplicate_of"] == first["receipt_ref"]
        kept, status = invoke(agents, ex, "receipts_delete", {"receipt_ref": first["receipt_ref"]}, chat_level=2)
        assert status == "success" and kept["deleted"] is False
        assert "مقهى الكرك الذهبي" in ex.asked[-1] and first["receipt_ref"] in provider.data["receipt"]
        answers[:] = ["Delete it"]
        gone, status = invoke(agents, ex, "receipts_delete", {"receipt_ref": first["receipt_ref"]}, chat_level=2)
        assert status == "success", gone
        assert gone == {"receipt_ref": first["receipt_ref"], "deleted": True, "released": [copy["receipt_ref"]]}
        got, _ = invoke(agents, ex, "claims_get", {"claim_ref": claim_ref})
        assert [(r["receipt_ref"], r["status"]) for r in got["receipts"]] == [(copy["receipt_ref"], "extracted")]

    def test_a_claim_is_deleted_with_its_receipts_but_never_once_submitted(self, agents):
        ex, provider = make(["Delete it", "Submit"])
        claim_ref = claim_of(agents, ex)
        read(agents, ex, claim_ref, "karak.txt", KARAK)
        other = claim_of(agents, ex, "EUR", title="submitted")
        read(agents, ex, other, "lumiere.txt", "Café Lumière\n2026-09-04\nTotal EUR 12,50")
        submitted, status = invoke(agents, ex, "claims_set_status", {"claim_ref": other, "status": "submitted"})
        assert status == "success" and submitted["status"] == "submitted", submitted
        refused, status = invoke(agents, ex, "claims_delete", {"claim_ref": other}, chat_level=2)
        assert status == "error" and "submitted" in refused["error"]
        first, last = list(provider.data["receipt"])
        refused, status = invoke(agents, ex, "receipts_delete", {"receipt_ref": last}, chat_level=2)
        assert status == "error" and "Nothing was deleted" in refused["error"]
        gone, status = invoke(agents, ex, "claims_delete", {"claim_ref": claim_ref}, chat_level=2)
        assert status == "success" and gone["deleted"] is True and gone["receipts"] == 1, gone
        assert claim_ref not in provider.data["claim"] and list(provider.data["receipt"]) == [last]


class TestSubmitting:
    def test_submit_asks_first_and_a_no_or_no_answer_changes_nothing(self, agents):
        for answers in ([], ["Not now"]):
            ex, provider = make(answers)
            claim_ref = claim_of(agents, ex)
            read(agents, ex, claim_ref, "karak.txt", KARAK)
            result, status = invoke(agents, ex, "claims_set_status", {"claim_ref": claim_ref, "status": "submitted"})
            assert status == "success" and result["status"] == "draft" and "Not submitted" in result["note"]
            assert "12.5 AED" in ex.asked[-1]
            assert provider.data["claim"][claim_ref]["keys"]["status"] == "draft"


FILLER = ("Travel is booked through the office; employees keep their boarding passes and name the customer or the "
          "purpose of each trip on the claim form, together with the dates and the cities visited.\n")


class TestLongPolicies:
    def test_a_rule_past_thirty_thousand_characters_is_read(self, agents):
        """The bug: the policy was cut at 30,000 characters without a word, so a rule past it was "not found"."""
        SENTENCES.clear()
        SENTENCES.update({
            "meal_max_per_day": ("١٥٠", "الحد الأقصى للوجبات ١٥٠ درهماً للشخص في اليوم."),
            "hotel_max_per_night": ("600", "Hotels are reimbursed up to 600 AED per night."),
        })
        policy = ("سياسة المصروفات — شركة سدرة\n" + SENTENCES["meal_max_per_day"][1] + "\n" + FILLER * 220
                  + SENTENCES["hotel_max_per_night"][1] + "\n")
        assert len(policy) > 40000
        ex, _ = make()
        ref = upload(ex, "long-policy.txt", policy.encode())
        result, status = invoke(agents, ex, "policies_load", {"file_ref": ref})
        assert status == "success", result
        assert result["windows"] == 2
        rules = {r["name"]: r for r in result["rules"]}
        assert rules["meal_max_per_day"]["value"] == "150" and rules["meal_max_per_day"]["verified"] is True
        assert rules["hotel_max_per_night"]["value"] == "600"
        assert rules["hotel_max_per_night"]["place"] == "window 2 of 2"
        assert "hotel_max_per_night" not in result["not_found"] and "unread" not in result

    def test_a_part_the_model_refused_is_named(self, agents):
        SENTENCES.clear()
        SENTENCES.update({"meal_max_per_day": ("60,50", "Meals up to 60,50 EUR per day.")})
        policy = SENTENCES["meal_max_per_day"][1] + "\n" + FILLER * 220
        ex, _ = make()
        ref = upload(ex, "refusing-policy.txt", policy.encode())
        result, status = invoke(agents, ex, "policies_load", {"file_ref": ref})
        assert status == "success", result
        assert result["rules"][0]["value"] == "60.5"
        assert result["unread"] == [{"place": "window 2 of 2", "reason": "the model refused to read it"}]
        assert "not read" in result["note"]

    def test_a_policy_too_long_to_read_whole_is_refused_with_its_size(self, agents):
        ex, _ = make()
        ref = upload(ex, "endless-policy.txt", (FILLER * 3000).encode())
        result, status = invoke(agents, ex, "policies_load", {"file_ref": ref})
        assert status == "error" and "windows" in result["error"] and "at most 20" in result["error"]
