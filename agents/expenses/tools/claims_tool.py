import asyncio
import io
import re
from collections import defaultdict
from decimal import Decimal

from standard import ToolBase

from .common import (CODES, code_of, iso_date, keys_of, money, normalize_merchant, now, out, parse_rate, quantized,
                     read_all, receipts, rules_of, shown, today)
from .receipts_tool import release

CLAIM_TEXT = ("title", "claimant", "status", "currency", "note")


def when(call, name):
    """A date input, or today in the person's zone: (date, error)."""
    if call.inputs.get(name):
        found = iso_date(call.inputs[name])
        return (found, "") if found else (None, f"{name} must be YYYY-MM-DD.")
    try:
        return today(str(call.inputs.get("timezone") or "")), ""
    except Exception:
        return None, f"{call.inputs.get('timezone')} is not a time zone (an IANA name such as Asia/Dubai)."


def totals_of(rows):
    """currency -> [total, receipts], duplicates and unread amounts left out."""
    totals = defaultdict(lambda: [Decimal("0"), 0])
    for row in rows:
        if row["status"] == "duplicate" or row.get("amount") is None:
            continue
        totals[row["currency"] or "?"][0] += row["amount"]
        totals[row["currency"] or "?"][1] += 1
    return totals


class ClaimsTool(ToolBase):
    id = "claims"

    async def list(self, call):
        wanted = str(call.inputs.get("status") or "")
        listed = await call.records.list("claim", {"status": wanted} if wanted else None)
        read = await read_all(call, "claim", [r["ref"] for r in listed])
        return {"claims": [{"claim_ref": r["ref"], **{k: str(keys_of(r).get(k) or "") for k in CLAIM_TEXT}}
                           for r in read]}, "success"

    async def create(self, call):
        currency = code_of(call.inputs["currency"])
        if not currency:
            return {"error": f"{call.inputs['currency']} is not a currency this agent knows: give its ISO code "
                             f"({', '.join(CODES[:8])}…)."}, "error"
        claim_ref = await call.records.create("claim", {
            "title": str(call.inputs["title"]).strip(), "claimant": str(call.inputs["claimant"]).strip(),
            "status": "draft", "currency": currency, "created": now()})
        return {"claim_ref": claim_ref, "status": "draft", "currency": currency}, "success"

    async def _receipts(self, call, claim_ref):
        rows = await receipts(call, {"claim_ref": claim_ref})
        rows.sort(key=lambda r: (r["date"] or "9999", r["filename"]))
        return rows

    def _rates(self, call, claim_currency):
        """The rates given, as exact decimals: {currency: rate into the claim's currency}, or an error."""
        rates = {}
        for name, value in (call.inputs.get("rates") or {}).items():
            code = code_of(name)
            rate, problem = parse_rate(value)
            if not code or rate is None:
                return None, f"Rate {name} = {value}: {problem or 'not a currency code'}."
            rates[code] = rate
        return rates, ""

    @staticmethod
    def _informed(totals, rates, claim_currency):
        """Each other currency's total converted at the rate given: information beside the claim, never in it."""
        shown_rows, missing = [], []
        for currency, (total, _) in sorted(totals.items()):
            if currency in (claim_currency, "?"):
                continue
            if currency not in rates:
                missing.append(currency)
                continue
            shown_rows.append({"currency": currency, "amount": out(total), "rate": str(rates[currency]),
                               "converted": out(quantized(total * rates[currency], claim_currency)),
                               "into": claim_currency})
        return shown_rows, missing

    async def get(self, call):
        ref = str(call.inputs["claim_ref"])
        claim = keys_of(await call.records.read("claim", ref))
        claim_currency = str(claim.get("currency") or "")
        rates, problem = self._rates(call, claim_currency)
        if rates is None:
            return {"error": problem}, "error"
        rows = await self._receipts(call, ref)
        totals = totals_of(rows)
        result = {
            "claim_ref": ref, "title": str(claim.get("title") or ""),
            "claimant": str(claim.get("claimant") or ""), "status": str(claim.get("status") or "draft"),
            "currency": claim_currency,
            "decided_by": str(claim.get("decided_by") or ""), "decided_on": str(claim.get("decided_on") or ""),
            "needs_attention": sum(1 for r in rows if r["status"] in ("needs_review", "manual", "duplicate")),
            "totals": [{"currency": c, "amount": out(t[0]), "receipts": t[1]} for c, t in sorted(totals.items())],
            "receipts": [{k: v for k, v in shown(r).items() if k != "claim_ref"} for r in rows],
        }
        if rates:
            informed, missing = self._informed(totals, rates, claim_currency)
            result["for_information"] = {
                "note": "Converted at the rates given, for information only: the claim stays in each receipt's own "
                        "currency and its totals are unchanged.",
                "converted": informed, "no_rate": missing}
        return result, "success"

    async def _exceptions(self, call, claim_ref, as_of):
        """Every reason a reviewer would stop on a receipt, from the rules in force and the receipts as recorded.
        Deterministic."""
        claim = keys_of(await call.records.read("claim", claim_ref))
        claim_currency = str(claim.get("currency") or "")
        rows = await self._receipts(call, claim_ref)
        by_ref = {row["receipt_ref"]: row for row in rows}
        rules = rules_of(await call.records.list("rule", {}))
        exceptions = []

        def add(row, kind, detail):
            exceptions.append({"receipt_ref": row["receipt_ref"], "filename": row["filename"],
                               "kind": kind, "detail": detail})

        # Duplicates across everything recorded, not only this claim. A duplicate has the same date, so each of this
        # claim's dates is listed, in the order its receipts were added: the first added is the original.
        for day in sorted({row["date"] for row in rows if row["date"]}):
            originals = defaultdict(list)
            for row in await receipts(call, {"date": day}):
                if row["status"] == "duplicate" or row.get("amount") is None or not normalize_merchant(row["merchant"]):
                    continue
                key = (normalize_merchant(row["merchant"]), row["amount"], row["currency"])
                apart = set(row["not_duplicate_of"].split(","))
                found = next((o for o in originals[key] if o["receipt_ref"] not in apart
                              and row["receipt_ref"] not in o["not_duplicate_of"].split(",")), None)
                if found is None:
                    originals[key].append(row)
                elif row["receipt_ref"] in by_ref:
                    await call.records.update("receipt", row["receipt_ref"], {
                        "status": "duplicate", "duplicate_of": found["receipt_ref"],
                        "read_status": row["read_status"] or row["status"]})
                    by_ref[row["receipt_ref"]].update(status="duplicate", duplicate_of=found["receipt_ref"])

        meals_by_day = defaultdict(Decimal)
        for row in rows:
            if row["status"] == "duplicate":
                add(row, "duplicate", f"same merchant, date and amount as {row['duplicate_of'] or 'another receipt'}")
                continue
            if row["status"] == "manual":
                add(row, "manual", "not read: details must be entered and confirmed")
                continue
            if row["status"] == "needs_review":
                add(row, "needs_review", row["note"] or f"assumed: {row['assumed']}")
            if row["currency"] and row["currency"] != claim_currency:
                add(row, "currency", f"{row['currency']} receipt in a {claim_currency} claim; not converted")
            amount = row.get("amount")
            if amount is None:
                continue
            if row["category"] == "meals" and row["date"] and row["currency"] == claim_currency:
                meals_by_day[row["date"]] += amount
            if row["category"] == "accommodation" and "hotel_max_per_night" in rules:
                cap = money(rules["hotel_max_per_night"][0])
                if cap is not None and amount > cap and row["currency"] == claim_currency:
                    add(row, "policy", f"accommodation {out(amount)} exceeds hotel_max_per_night {out(cap)}")
            if row["category"] == "transport" and "taxi_max_per_trip" in rules:
                cap = money(rules["taxi_max_per_trip"][0])
                if cap is not None and amount > cap and row["currency"] == claim_currency:
                    add(row, "policy", f"transport {out(amount)} exceeds taxi_max_per_trip {out(cap)}")
            if "claim_within_days" in rules and row["date"]:
                days = rules["claim_within_days"][0]
                receipt_date = iso_date(row["date"])
                if days.isdigit() and receipt_date and (as_of - receipt_date).days > int(days):
                    add(row, "policy", f"receipt dated {row['date']} is older than claim_within_days {days}")
            # "A receipt is required above" asks that the receipt prove the amount: read from its own text (or off
            # its picture), or confirmed by a person.
            if "receipt_required_above" in rules and row["currency"] == claim_currency:
                floor = money(rules["receipt_required_above"][0])
                if floor is not None and amount > floor and "amount" not in row["verified"]:
                    add(row, "policy", f"{out(amount)} is above receipt_required_above {out(floor)}, "
                                       f"and the receipt does not show that amount")
        if "meal_max_per_day" in rules:
            cap = money(rules["meal_max_per_day"][0])
            if cap is not None:
                for day, total in sorted(meals_by_day.items()):
                    if total > cap:
                        row = next(r for r in rows if r["category"] == "meals" and r["date"] == day
                                   and r["status"] != "duplicate")
                        add(row, "policy", f"meals on {day} total {out(total)}, over meal_max_per_day {out(cap)}")
        applied = sorted(f"{name} = {value}" for name, (value, _) in rules.items())
        return rows, exceptions, applied

    async def check(self, call):
        ref = str(call.inputs["claim_ref"])
        as_of, problem = when(call, "as_of")
        if as_of is None:
            return {"error": problem}, "error"
        rows, exceptions, applied = await self._exceptions(call, ref, as_of)
        flagged = {e["receipt_ref"] for e in exceptions}
        return {"claim_ref": ref, "receipts": len(rows),
                "ok": sum(1 for r in rows if r["receipt_ref"] not in flagged),
                "exceptions": exceptions, "rules_applied": applied}, "success"

    async def workbook(self, call):
        ref = str(call.inputs["claim_ref"])
        as_of, problem = when(call, "as_of")
        if as_of is None:
            return {"error": problem}, "error"
        claim = keys_of(await call.records.read("claim", ref))
        rates, problem = self._rates(call, str(claim.get("currency") or ""))
        if rates is None:
            return {"error": problem}, "error"
        rows, exceptions, applied = await self._exceptions(call, ref, as_of)
        informed, missing = self._informed(totals_of(rows), rates, str(claim.get("currency") or ""))
        raw = await asyncio.to_thread(self._book, claim, rows, exceptions, applied, as_of, informed, missing)
        claimant = re.sub(r"[\\/:*?\"<>|\s]+", "-", str(claim.get("claimant") or "claim")).strip("-") or "claim"
        filename = f"claim-{claimant}-{as_of.isoformat()}.xlsx"
        try:
            verified = await asyncio.to_thread(self._read_back, raw)
        except Exception as exc_:
            return {"error": f"The workbook did not read back: {exc_}"}, "error"
        return {"file_ref": call.give(filename, raw), "filename": filename,
                "receipts": len(rows), "exceptions": len(exceptions), "verified": verified}, "success"

    @staticmethod
    def _read_back(raw):
        from openpyxl import load_workbook

        return "Receipts" in load_workbook(io.BytesIO(raw), read_only=True).sheetnames

    @staticmethod
    def _book(claim, rows, exceptions, applied, as_of, informed, missing):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Receipts"
        ws.append([f"Expense claim — {claim.get('title')} — {claim.get('claimant')} — status {claim.get('status')}"])
        ws.append([f"Prepared {as_of.isoformat()}; amounts as on the receipts, no conversion"])
        ws.append([])
        ws.append(["Date", "Merchant", "Category", "Currency", "Amount", "Status", "Verified",
                   "Assumed", "Duplicate of", "Note", "File", "Receipt ref", "Read from"])
        for row in rows:
            ws.append([row["date"], row["merchant"], row["category"], row["currency"],
                       out(row["amount"]) if row.get("amount") is not None else None, row["status"],
                       row["verified"], row["assumed"], row["duplicate_of"], row["note"], row["filename"],
                       row["receipt_ref"], row["read_from"]])
        summary = wb.create_sheet("Summary")
        summary.append(["Category", "Currency", "Receipts", "Total"])
        by = defaultdict(lambda: [0, Decimal("0")])
        for row in rows:
            if row["status"] == "duplicate" or row.get("amount") is None:
                continue
            by[(row["category"], row["currency"])][0] += 1
            by[(row["category"], row["currency"])][1] += row["amount"]
        for (category, currency), (count, total) in sorted(by.items()):
            summary.append([category, currency, count, out(total)])
        exc = wb.create_sheet("Exceptions")
        exc.append(["Receipt ref", "File", "Kind", "Detail"])
        for e in exceptions:
            exc.append([e["receipt_ref"], e["filename"], e["kind"], e["detail"]])
        rules = wb.create_sheet("Rules applied")
        rules.append(["Rule"])
        for line in applied:
            rules.append([line])
        if informed or missing:
            info = wb.create_sheet("For information")
            info.append(["Converted at the rates given, for information only; the claim's totals are unchanged."])
            info.append(["Currency", "Total", "Rate", "Converted", "Into"])
            for item in informed:
                info.append([item["currency"], item["amount"], item["rate"], item["converted"], item["into"]])
            for currency in missing:
                info.append([currency, None, "no rate given", None, None])
        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    async def set_status(self, call):
        ref = str(call.inputs["claim_ref"])
        claim = keys_of(await call.records.read("claim", ref))
        current = str(claim.get("status") or "draft")
        wanted = str(call.inputs["status"])
        by = str(call.inputs.get("by") or "").strip()
        on, problem = when(call, "on_date")
        if on is None:
            return {"error": problem}, "error"
        allowed = {"draft": {"submitted"}, "submitted": {"approved", "rejected"},
                   "approved": {"paid"}, "rejected": set(), "paid": set()}
        if wanted not in allowed[current]:
            return {"error": f"A {current} claim cannot become {wanted}."}, "error"
        if wanted == "submitted":
            rows = await self._receipts(call, ref)
            pending = [r["filename"] or r["receipt_ref"] for r in rows if r["status"] in ("needs_review", "manual")]
            if pending:
                return {"error": "Receipts still need review or entry: " + ", ".join(pending)}, "error"
            if not rows:
                return {"error": "The claim has no receipts."}, "error"
            totals = "; ".join(f"{out(t[0])} {c}" for c, t in sorted(totals_of(rows).items()))
            answer = await call.ask(f"Submit \"{claim.get('title')}\" for approval: {len(rows)} receipt(s), "
                                    f"{totals}? Once submitted, its receipts can't be changed or deleted.",
                                    ["Submit", "Not now"])
            if answer != "Submit":
                return {"claim_ref": ref, "status": current,
                        "note": "Not submitted: the person did not confirm. Nothing was changed."}, "success"
        elif not by:
            return {"error": f"{wanted} is a reviewer's decision: name who decided in \"by\"."}, "error"
        changes = {"status": wanted, "note": str(call.inputs.get("note") or "")}
        if wanted != "submitted":
            changes.update({"decided_by": by, "decided_on": on.isoformat()})
        await call.records.update("claim", ref, changes)
        return {"claim_ref": ref, "status": wanted, "decided_by": changes.get("decided_by", ""),
                "decided_on": changes.get("decided_on", "")}, "success"

    async def delete(self, call):
        """A draft or rejected claim and its receipts, after the person says yes. A submitted, approved or paid
        claim is the trail of what was asked and decided, and stays."""
        ref = str(call.inputs["claim_ref"])
        claim = keys_of(await call.records.read("claim", ref))
        status = str(claim.get("status") or "draft")
        if status not in ("draft", "rejected"):
            return {"error": f"The claim is {status}: a submitted, approved or paid claim stays as the record of "
                             f"what was asked and decided. Nothing was deleted."}, "error"
        listed = [r["ref"] for r in await call.records.list("receipt", {"claim_ref": ref})]
        answer = await call.ask(f"Delete the claim \"{claim.get('title')}\" ({status}) and its {len(listed)} "
                                f"receipt record(s)? This can't be undone; the receipt files themselves are kept.",
                                ["Delete it", "Keep it"])
        if answer != "Delete it":
            return {"claim_ref": ref, "deleted": False, "receipts": 0, "released": []}, "success"
        released = await release(call, listed)
        for receipt_ref in listed:
            await call.records.delete("receipt", receipt_ref)
        await call.records.delete("claim", ref)
        return {"claim_ref": ref, "deleted": True, "receipts": len(listed), "released": released}, "success"
