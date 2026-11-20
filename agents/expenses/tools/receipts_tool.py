from standard import ToolBase

from .common import (CATEGORIES, DOLLARS, TooLarge, amounts_in, arabic_digits, code_of, currencies_in,
                     decimal_mark, folded, iso_date, keys_of, load, normalize_merchant, now, out, parse_date,
                     parse_json, parse_money, quantized, receipt_row, receipts, shown)

READ_SYSTEM = (
    "You read one receipt and answer ONLY with a JSON object: {\"merchant\": "
    "<the business name as printed>, \"date\": <the transaction date as "
    "printed>, \"currency\": <the ISO code the receipt shows, or \"\" when it shows none or only \"$\">, "
    "\"amount\": <the total paid, exactly as printed, with its separators>, "
    "\"category\": <one of meals, travel, accommodation, transport, office, "
    "other>, \"quote_amount\": <the line, copied verbatim, that shows the "
    "total>}. Copy what is printed, in its own script and digits; never guess a value the receipt does not "
    "show — use \"\" instead."
)
TRANSCRIBE_SYSTEM = (
    "You transcribe receipts. Copy every line of text you can see on the receipt exactly as printed, top to bottom, "
    "one line per line, in its own script and digits (Arabic stays Arabic). Do not summarize, translate, correct or "
    "add anything. If there is no readable text, answer NONE."
)
TEXT_MOST = 12000


class ReceiptsTool(ToolBase):
    id = "receipts"

    async def read(self, call):
        claim_ref = str(call.inputs["claim_ref"])
        claim = keys_of(await call.records.read("claim", claim_ref))
        if claim.get("status") not in ("draft", None, ""):
            return {"error": f"The claim is {claim.get('status')}; receipts go into a draft."}, "error"
        stated = str(call.inputs.get("currency") or "").strip().upper()
        try:
            loaded = await load(call, "receipt_file", str(call.inputs["file_ref"]))
        except TooLarge as exc:
            return {"error": str(exc), "kind": "too_large"}, "error"
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"

        notes = [loaded.problem] if loaded.problem and loaded.pictures else []
        text, read_from = loaded.text, "text"
        if not text and loaded.pictures:
            read_from = "picture"
            await call.progress(f"Reading the picture of {loaded.filename}")
            try:
                said = await call.model(
                    f"Transcribe this receipt ({loaded.filename}).", system=TRANSCRIBE_SYSTEM,
                    pictures=loaded.pictures, max_tokens=4000)
            except Exception as exc:
                return {"error": f"The model could not be asked to read the picture: {exc}"}, "error"
            if said.refused:
                return await self._manual(call, claim_ref, loaded, read_from,
                                          "The model refused to read this picture (its provider's filter); "
                                          "enter its details and confirm them.")
            text = str(said).strip()
            if not text or text.upper() == "NONE":
                return await self._manual(call, claim_ref, loaded, read_from,
                                          "No text could be read off the picture; enter its details and confirm "
                                          "them.")
            if said.cut:
                notes.append("the reading of the picture was cut short, so lines after the cut were not read")
        if not text:
            return await self._manual(call, claim_ref, loaded, read_from, loaded.problem or "The file has no text.")
        if len(text) > TEXT_MOST:
            notes.append(f"only the first {TEXT_MOST:,} of its {len(text):,} characters were read")

        source = "RECEIPT" + (" (read from a picture)" if read_from == "picture" else "")
        prompt = f"{source} ({loaded.filename}):\n{text[:TEXT_MOST]}\n\nAnswer with the JSON object."
        await call.progress(f"Reading {loaded.filename}")
        try:
            answer = await call.model(prompt, system=READ_SYSTEM)
        except Exception as exc:
            return {"error": f"The model could not be asked: {exc}"}, "error"
        if answer.refused:
            return await self._manual(call, claim_ref, loaded, read_from,
                                      "The model refused to read this receipt (its provider's filter); enter its "
                                      "details and confirm them.", text)
        parsed = parse_json(answer)
        if not isinstance(parsed, dict):
            why = "was cut short" if answer.cut else "was not a JSON object"
            return {"error": f"The model's answer {why}; nothing was recorded. Try again."}, "error"

        checked = self._checked(parsed, text, stated)
        verified, assumed = checked["verified"], checked["assumed"]
        notes += checked["notes"]
        merchant, date_iso, currency, amount = (checked["merchant"], checked["date"], checked["currency"],
                                                checked["amount"])

        duplicate_of = ""
        if merchant and date_iso and amount is not None:
            for row in await receipts(call, {"date": date_iso}):
                if (row["status"] != "duplicate" and normalize_merchant(row["merchant"]) == normalize_merchant(merchant)
                        and row.get("amount") == amount and (not row["currency"] or not currency
                                                             or row["currency"] == currency)):
                    duplicate_of = row["receipt_ref"]
                    break

        needs_review = bool(notes) or any(f in assumed for f in ("merchant", "date", "amount", "currency"))
        read_status = "needs_review" if needs_review else "extracted"
        status = "duplicate" if duplicate_of else read_status
        fields = {"claim_ref": claim_ref, "file_ref": loaded.file_ref, "filename": loaded.filename,
                  "merchant": merchant, "date": date_iso, "currency": currency,
                  "category": checked["category"], "status": status, "read_status": read_status,
                  "read_from": read_from, "added": now(),
                  "verified": ", ".join(verified), "assumed": ", ".join(assumed),
                  "duplicate_of": duplicate_of, "note": "; ".join(notes)[:1000]}
        if read_from == "picture":
            fields["text_read"] = text[:20000]
        if amount is not None:
            fields["amount"] = out(amount)
        result = {"receipt_ref": await call.records.create("receipt", fields), "status": status,
                  "merchant": merchant, "date": date_iso, "currency": currency, "category": checked["category"],
                  "verified": verified, "assumed": assumed, "read_from": read_from}
        if amount is not None:
            result["amount"] = out(amount)
        if duplicate_of:
            result["duplicate_of"] = duplicate_of
        if notes:
            result["note"] = "; ".join(notes)
        return result, "success"

    @staticmethod
    def _checked(parsed, text, stated):
        """Each field the model gave, held to the receipt's text: verified when the text shows it, assumed when not,
        left out when it can't be read."""
        verified, assumed, notes = [], [], []
        plain = arabic_digits(text)
        flat = folded(text)

        merchant = str(parsed.get("merchant") or "").strip()[:120]
        if merchant and normalize_merchant(merchant) and normalize_merchant(merchant) in normalize_merchant(text):
            verified.append("merchant")
        elif merchant:
            assumed.append("merchant")
        else:
            notes.append("no merchant found")

        date_text = arabic_digits(parsed.get("date") or "").strip()
        found = parse_date(date_text) if date_text else None
        fallback = False
        if found is None and date_text:
            found = parse_date(plain)           # the model paraphrased: the receipt's own first date, assumed
            fallback = found is not None
        date_iso = found.isoformat() if found else ""
        if found and not fallback and (folded(date_text) in flat or date_iso in plain):
            verified.append("date")
        elif found:
            assumed.append("date")
            if fallback:
                notes.append(f"the date \"{date_text}\" could not be read; {date_iso} is the first date on the "
                             f"receipt, assumed")
        else:
            notes.append("date not readable" if date_text else "no date found")

        named = code_of(parsed.get("currency"))
        codes, bare_dollar = currencies_in(text)
        currency = ""
        if stated:
            currency = stated
            if codes and stated not in codes:
                assumed.append("currency")
                notes.append(f"the receipt shows {', '.join(codes)}, not {stated} as stated")
            elif codes:
                verified.append("currency")
            elif bare_dollar and stated not in DOLLARS:
                assumed.append("currency")
                notes.append(f"the receipt shows \"$\", not {stated} as stated")
            else:
                verified.append("currency (as stated by the person)")
        elif named and named in codes:
            currency = named
            verified.append("currency")
        elif len(codes) == 1:
            currency = codes[0]
            verified.append("currency")
        elif bare_dollar and (not named or named in DOLLARS):
            notes.append("the receipt shows only \"$\", which could be USD, CAD, AUD or another dollar: say which "
                         "currency (receipts_read with currency, or receipts_confirm)")
        elif named:
            currency = named
            assumed.append("currency")
        else:
            notes.append("currency not shown")

        mark = decimal_mark(plain)
        amount, problem = parse_money(parsed.get("amount"), currency, mark)
        on_receipt = amounts_in(plain, currency, mark)
        if amount is not None:
            amount = quantized(amount, currency)
            if amount in on_receipt:
                verified.append("amount")
                if amount < max(on_receipt):
                    notes.append(f"a larger figure ({out(max(on_receipt))}) appears on the receipt")
            else:
                assumed.append("amount")
        elif problem:
            notes.append(f"the amount {problem}")
        else:
            notes.append("no amount found")

        category = str(parsed.get("category") or "").strip().lower()
        category = category if category in CATEGORIES else "other"
        assumed.append("category")             # always the model's judgement
        return {"verified": verified, "assumed": assumed, "notes": notes, "merchant": merchant, "date": date_iso,
                "currency": currency, "amount": amount, "category": category}

    async def _manual(self, call, claim_ref, loaded, read_from, note, text=""):
        """A receipt that could not be read: recorded with its file, for a person to enter and confirm."""
        fields = {"claim_ref": claim_ref, "file_ref": loaded.file_ref, "filename": loaded.filename,
                  "category": "unknown", "status": "manual", "read_status": "manual", "read_from": read_from,
                  "added": now(), "verified": "", "assumed": "", "note": note[:1000]}
        if text:
            fields["text_read"] = text[:20000]
        receipt_ref = await call.records.create("receipt", fields)
        return {"receipt_ref": receipt_ref, "status": "manual", "category": "unknown", "verified": [],
                "assumed": [], "read_from": read_from, "note": note}, "success"

    async def confirm(self, call):
        ref = str(call.inputs["receipt_ref"])
        current = receipt_row(await call.records.read("receipt", ref))
        claim = keys_of(await call.records.read("claim", current["claim_ref"])) if current["claim_ref"] else {}
        if claim.get("status") not in (None, "", "draft"):
            return {"error": f"The receipt is on a {claim.get('status')} claim; it stays as it was sent."}, "error"
        changes = {}
        if call.inputs.get("date"):
            found = iso_date(call.inputs["date"])
            if found is None:
                return {"error": "date must be YYYY-MM-DD."}, "error"
            changes["date"] = found.isoformat()
        for name in ("merchant", "category", "note"):
            if call.inputs.get(name):
                changes[name] = str(call.inputs[name]).strip()
        if call.inputs.get("currency"):
            changes["currency"] = code_of(call.inputs["currency"]) or str(call.inputs["currency"]).strip().upper()
        currency = changes.get("currency") or current["currency"]
        if call.inputs.get("amount") is not None:
            amount, problem = parse_money(call.inputs["amount"], currency)
            if amount is None or amount < 0:
                return {"error": f"The amount {problem or 'is not an amount'}."}, "error"
            changes["amount"] = out(quantized(amount, currency))
        merged = {**current, **changes}
        missing = [f for f in ("merchant", "date", "currency") if not merged.get(f)] + \
                  (["amount"] if merged.get("amount") is None else [])
        if missing:
            return {"error": f"A confirmed receipt needs {', '.join(missing)}."}, "error"
        if merged.get("category") in ("", "unknown"):
            return {"error": "A confirmed receipt needs a category."}, "error"
        if len(merged["currency"]) != 3 or not merged["currency"].isalpha():
            return {"error": f"{merged['currency']} is not a currency code (AED, SAR, USD…)."}, "error"
        changes.update({"status": "confirmed", "read_status": "confirmed",
                        "verified": "merchant, date, currency, amount, category (confirmed by a person)",
                        "assumed": ""})
        await call.records.update("receipt", ref, changes)
        after = shown(receipt_row(await call.records.read("receipt", ref)))
        return {k: after[k] for k in ("receipt_ref", "status", "merchant", "date", "currency", "amount", "category")
                if k in after}, "success"

    async def not_duplicate(self, call):
        """A person's word that a receipt flagged as a duplicate is its own expense: it goes back to what it was
        read as, and is never matched to that receipt again."""
        ref = str(call.inputs["receipt_ref"])
        row = receipt_row(await call.records.read("receipt", ref))
        if row["status"] != "duplicate":
            return {"error": f"The receipt is {row['status']}, not marked as a duplicate."}, "error"
        claim = keys_of(await call.records.read("claim", row["claim_ref"])) if row["claim_ref"] else {}
        if claim.get("status") not in (None, "", "draft"):
            return {"error": f"The receipt is on a {claim.get('status')} claim; it stays as it was sent."}, "error"
        kept = [r for r in row["not_duplicate_of"].split(",") if r.strip()]
        kept += [row["duplicate_of"]] if row["duplicate_of"] and row["duplicate_of"] not in kept else []
        status = row["read_status"] or "needs_review"
        reason = str(call.inputs.get("reason") or "").strip()
        note = "; ".join(filter(None, [row["note"], f"not a duplicate of {row['duplicate_of'] or 'another receipt'}"
                                                     f" (a person's word{': ' + reason if reason else ''})"]))
        await call.records.update("receipt", ref, {"status": status, "duplicate_of": "",
                                                    "not_duplicate_of": ",".join(kept), "note": note[:1000]})
        return {"receipt_ref": ref, "status": status, "not_duplicate_of": kept}, "success"

    async def delete(self, call):
        """A receipt taken out of a draft claim, after the person says yes. Receipts that were marked duplicates of
        it are released back to what they were read as."""
        ref = str(call.inputs["receipt_ref"])
        row = receipt_row(await call.records.read("receipt", ref))
        claim = keys_of(await call.records.read("claim", row["claim_ref"])) if row["claim_ref"] else {}
        if claim.get("status") not in (None, "", "draft"):
            return {"error": f"The receipt is on a {claim.get('status')} claim: a claim's receipts stay as they were "
                             f"submitted. Nothing was deleted."}, "error"
        described = ", ".join(filter(None, [row["filename"], row["merchant"], row["date"],
                                            f"{out(row['amount'])} {row['currency']}".strip()
                                            if "amount" in row else ""]))
        answer = await call.ask(f"Delete the receipt {described} from the claim "
                                f"\"{claim.get('title') or row['claim_ref']}\"? This can't be undone; the file "
                                f"itself is kept.", ["Delete it", "Keep it"])
        if answer != "Delete it":
            return {"receipt_ref": ref, "deleted": False, "released": []}, "success"
        released = await release(call, [ref])
        await call.records.delete("receipt", ref)
        return {"receipt_ref": ref, "deleted": True, "released": released}, "success"


async def release(call, refs):
    """Receipts marked as duplicates of any of these, back to what they were read as: the receipts named are going
    away, so the copies are originals now unless a check finds otherwise."""
    released = []
    for ref in refs:
        for row in await receipts(call, {"duplicate_of": ref}):
            if row["receipt_ref"] in refs:
                continue
            await call.records.update("receipt", row["receipt_ref"], {
                "status": row["read_status"] or "needs_review", "duplicate_of": ""})
            released.append(row["receipt_ref"])
    return released
