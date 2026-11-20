"""The expense policy as rules: read whole, in windows when it is long, each rule with the sentence and the place it
came from. Nothing past a window is left unread without saying so."""

from standard import ToolBase

from .common import TooLarge, decimal_mark, folded, keys_of, load, parse_json, parse_money

RULE_NAMES = ("meal_max_per_day", "hotel_max_per_night", "taxi_max_per_trip",
              "receipt_required_above", "claim_within_days", "alcohol_allowed")
NUMBERS = ("meal_max_per_day", "hotel_max_per_night", "taxi_max_per_trip", "receipt_required_above")
YES = {"yes", "true", "allowed", "نعم", "مسموح"}
NO = {"no", "false", "not allowed", "لا", "غير مسموح", "ممنوع"}

#: How much of the policy one model call reads, and how many calls a policy may take.
WINDOW = 24000
WINDOWS_MOST = 20
SCAN_PAGES_MOST = 40

LOAD_SYSTEM = (
    "You read part of a company expense policy and answer ONLY with a JSON object "
    "keyed by rule name. Rules: meal_max_per_day (a number, the most that "
    "may be claimed for meals per person per day), hotel_max_per_night (a "
    "number), taxi_max_per_trip (a number), receipt_required_above (a "
    "number: receipts are required for amounts above it), claim_within_days "
    "(a number of days within which a claim must be made), alcohol_allowed "
    "(yes or no). For each rule this part states: {\"value\": <as stated, the number with its separators as "
    "written>, \"quote\": <the sentence, copied verbatim in its own language>}. "
    "For a rule this part does not state: {\"found\": false}. Never infer a "
    "number the policy does not give."
)
TRANSCRIBE_SYSTEM = (
    "You transcribe a scanned page of a document. Copy every line of text exactly as printed, top to bottom, in its "
    "own script and digits (Arabic stays Arabic). Do not summarize, translate or add anything. If the page has no "
    "readable text, answer NONE."
)


def windows_of(pages):
    """The policy in windows of at most WINDOW characters, cut between lines, never inside one: each window its
    text and the (page, start, end) spans of its pages within it."""
    windows, text, spans = [], "", []
    for number, page in enumerate(pages, 1):
        for line in page.splitlines(keepends=True):
            while len(line) > WINDOW:                     # one line longer than a window: cut it, said in the place
                head, line = line[:WINDOW], line[WINDOW:]
                if text:
                    windows.append((text, spans))
                windows.append((head, [(number, 0, len(head))]))
                text, spans = "", []
            if len(text) + len(line) > WINDOW and text:
                windows.append((text, spans))
                text, spans = "", []
            if not spans or spans[-1][0] != number:
                spans.append((number, len(text), len(text)))
            text += line
            spans[-1] = (number, spans[-1][1], len(text))
        if text and not text.endswith("\n"):
            text += "\n"
    if text.strip():
        windows.append((text, spans))
    return windows


class PoliciesTool(ToolBase):
    id = "policies"

    async def _set(self, call, name, value, quote="", source="", place=""):
        existing = [r for r in await call.records.list("rule", {"name": name})]
        fields = {"name": name, "value": str(value), "quote": quote[:300], "source": source, "place": place}
        if existing:
            ref = existing[-1]["ref"]
            await call.records.update("rule", ref, fields)
            return ref
        return await call.records.create("rule", fields)

    @staticmethod
    def normalized(name, value, mark=""):
        """A rule's value as code applies it: a plain number ("1250.50"), whole days, or yes/no; or (None, why)."""
        text = str(value).strip()
        if name == "alcohol_allowed":
            lowered = text.lower()
            return ("yes", "") if lowered in YES else ("no", "") if lowered in NO else (None, f"{text} is not yes or no")
        amount, problem = parse_money(text, "", mark)
        if amount is None or amount < 0:
            return None, problem or f"{text} is not a number"
        if name == "claim_within_days":
            return (str(int(amount)), "") if amount == amount.to_integral_value() else (None, f"{text} is not whole days")
        return format(amount.normalize(), "f"), ""

    async def load(self, call):
        try:
            loaded = await load(call, "policy_file", str(call.inputs["file_ref"]), pictures_most=SCAN_PAGES_MOST)
        except TooLarge as exc:
            return {"error": str(exc), "kind": "too_large"}, "error"
        except Exception as exc:
            return {"error": f"The policy could not be read: {exc}"}, "error"
        pages, read_from, unread = list(loaded.pages), "text", []
        if not loaded.text and loaded.pictures:
            if loaded.page_count > SCAN_PAGES_MOST:
                return {"error": f"The policy is a scan of {loaded.page_count} pages; at most {SCAN_PAGES_MOST} "
                                 f"scanned pages are read. Send a PDF with text, or the pages that hold the rules."}, \
                    "error"
            read_from, pages = "picture", []
            for number, page in enumerate(loaded.pictures, 1):
                await call.progress(f"Reading page {number} of {len(loaded.pictures)} of {loaded.filename}")
                try:
                    said = await call.model(f"Transcribe page {number} of {loaded.filename}.",
                                            system=TRANSCRIBE_SYSTEM, pictures=[page], max_tokens=6000)
                except Exception as exc:
                    return {"error": f"The model could not be asked to read page {number}: {exc}"}, "error"
                if said.refused or said.cut:
                    unread.append({"place": f"page {number}",
                                   "reason": "the model refused to read it" if said.refused
                                   else "its reading was cut short"})
                pages.append("" if said.refused or str(said).strip().upper() == "NONE" else str(said))
        if not "".join(pages).strip():
            return {"error": loaded.problem or "The policy has no readable text."}, "error"

        windows = windows_of(pages)
        if len(windows) > WINDOWS_MOST:
            characters = sum(len(p) for p in pages)
            return {"error": f"The policy is {characters:,} characters, {len(windows)} windows of {WINDOW:,}; at most "
                             f"{WINDOWS_MOST} are read. Send the part that holds the expense rules."}, "error"
        paged = len(pages) > 1
        found = {name: [] for name in RULE_NAMES}
        for index, (text, spans) in enumerate(windows, 1):
            place = f"window {index} of {len(windows)}"
            await call.progress(f"Reading {loaded.filename} for the known rules ({place})")
            prompt = f"POLICY ({loaded.filename}, part {index} of {len(windows)}):\n{text}\n\nAnswer with the JSON object."
            try:
                answer = await call.model(prompt, system=LOAD_SYSTEM)
            except Exception as exc:
                return {"error": f"The model could not be asked: {exc}"}, "error"
            parsed = parse_json(answer)
            if answer.refused or not isinstance(parsed, dict):
                unread.append({"place": place, "reason": "the model refused to read it" if answer.refused
                               else "its answer was cut short" if answer.cut else "its answer was not a JSON object"})
                continue
            flat, mark = folded(text), decimal_mark(text)
            for name in RULE_NAMES:
                item = parsed.get(name)
                if not isinstance(item, dict) or item.get("found") is False or item.get("value") in (None, ""):
                    continue
                quote = " ".join(str(item.get("quote") or "").split())
                verified = len(quote) >= 8 and folded(quote) in flat
                where = place
                if verified and paged:
                    at = flat.find(folded(quote))
                    page = next((n for n, start, end in spans if len(folded(text[:end])) > at), spans[-1][0])
                    where = f"page {page}, {place}"
                found[name].append({"value": str(item["value"]).strip(), "quote": quote, "verified": verified,
                                    "place": where, "mark": mark})

        rules, not_found, unverified, conflicts = [], [], [], []
        for name in RULE_NAMES:
            candidates = [c for c in found[name] if c["verified"]] or found[name]
            if not candidates:
                not_found.append(name)
                continue
            readings = []
            for candidate in candidates:
                value, problem = self.normalized(name, candidate["value"], candidate["mark"])
                readings.append((value, problem, candidate))
            values = {value for value, _, _ in readings if value is not None}
            if len(values) > 1:
                conflicts.append({"name": name, "readings": [
                    {"value": c["value"], "quote": c["quote"], "place": c["place"]} for _, _, c in readings]})
                continue
            if not values:
                _, problem, candidate = readings[0]
                conflicts.append({"name": name, "readings": [{"value": candidate["value"], "quote": candidate["quote"],
                                                              "place": candidate["place"], "problem": problem}]})
                continue
            value, _, candidate = next(r for r in readings if r[0] is not None)
            if not candidate["verified"]:
                unverified.append(name)
            ref = await self._set(call, name, value, candidate["quote"], loaded.file_ref, candidate["place"])
            rules.append({"rule_ref": ref, "name": name, "value": value, "quote": candidate["quote"],
                          "verified": candidate["verified"], "place": candidate["place"]})
        result = {"rules": rules, "not_found": not_found, "unverified": unverified, "windows": len(windows),
                  "read_from": read_from}
        if conflicts:
            result["conflicts"] = conflicts
        if unread:
            result["unread"] = unread
            result["note"] = ("Parts of the policy were not read (see unread): a rule listed as not found may be "
                              "stated there.")
        return result, "success"

    async def set_rule(self, call):
        name = str(call.inputs["name"])
        value, problem = self.normalized(name, call.inputs["value"])
        if value is None:
            return {"error": f"{name}: {problem}."}, "error"
        ref = await self._set(call, name, value, quote="set by a person")
        return {"rule_ref": ref, "name": name, "value": value}, "success"

    async def get(self, call):
        rows = []
        for record in await call.records.list("rule", {}):
            keys = keys_of(record)
            rows.append({"rule_ref": record["ref"], **{k: str(keys.get(k) or "")
                                                       for k in ("name", "value", "quote", "place")}})
        rows.sort(key=lambda r: r["name"])
        return {"rules": rows}, "success"
