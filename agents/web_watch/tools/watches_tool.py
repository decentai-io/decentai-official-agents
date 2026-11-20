"""Watches: a public page or feed, and what about it matters.

Kinds, because "any change" is the wrong question for most pages:

- ``page``: any change to the readable text (whitespace aside);
- ``selector``: any change to one part of the page, picked by a CSS selector;
- ``contains``: a phrase appearing or disappearing;
- ``pattern``: the first match of a regular expression changing;
- ``number``: a number (a price, a count) read in code, reported when it
  crosses ``above`` or ``below``, moves by ``change_percent``, or, with
  none of those, changes at all;
- ``feed``: an RSS, Atom, RDF or JSON feed; its new items, by id (an
  item dated in the past that was never seen is still new), filtered by
  keywords if asked.

A run goes through ``standard.watch``: each watch is read on its own,
one that fails is listed in ``problems`` and keeps its last reading,
and readings are written only once every watch was read, together
with what they hand on. Runs in one process take turns, so two
overlapping runs never hand the same change on twice.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from standard import ResourceDenied, ToolBase
from standard.http import HttpError
from standard.watch import Gone, Seen, active, check, close, listing

from .feed_fetch import USER_AGENT, fetch_feed, resolve_feed
from .feed_parse import FeedProblem
from .fetch import FetchError, SafeFetcher, fetch
from .looking import (Reading, TooSlow, decimal, diff, now_utc, pack, pattern_refusal,
                      selector_refusal, unpack)
from .numbers import number_of, shown
from .page_text import UnsupportedContent, phrase_pattern, read_page

KINDS = ("page", "selector", "contains", "pattern", "number", "feed")
#: A run stops starting new reads after this; the rest is the next run's.
RUN_BUDGET_SECONDS = 170.0
#: Pages read at once in a run.
AT_ONCE = 6
#: A page answering 404 this many checks in a row is gone, and its watch closes.
GONE_AFTER_404 = 5
SEEN_MOST = 1500
LIST_SHOWN = 100
BROWSER = (" A page that needs JavaScript or a sign-in cannot be watched here; the Browser agent can "
           "read it.")

_turn = None


def turn() -> asyncio.Lock:
    """One run at a time in this process."""
    global _turn
    if _turn is None:
        _turn = asyncio.Lock()
    return _turn


def invalid(words: str):
    return {"error": words, "kind": "invalid"}, "error"


class WatchesTool(ToolBase):
    id = "watches"

    # -- the person's functions ---------------------------------------------------------------------------------
    async def add(self, call):
        inputs = call.inputs
        url, kind = str(inputs["url"]).strip(), str(inputs["kind"])
        spec, refusal = self._spec(kind, inputs)
        if refusal:
            return invalid(refusal)
        label = " ".join(str(inputs.get("label") or "").split())
        await call.progress(f"Taking a first reading of {url}")
        if kind == "feed":
            return await self._add_feed(call, url, label, spec)
        try:
            fetched = await fetch(url, USER_AGENT)
            page = await asyncio.to_thread(read_page, fetched)
        except FetchError as exc:
            out = exc.result()
            if exc.needs_browser:
                out["error"] += BROWSER
            return out, "error"
        except UnsupportedContent as exc:
            return {"error": str(exc), "kind": "unsupported"}, "error"
        if page.needs_browser:
            return {"error": f"{fetched.final_url} cannot be watched here: {page.browser_note}"
                             + BROWSER, "kind": page.browser_reason, "needs_browser": True}, "error"
        for record in await call.records.list("watch", {"url": url, "kind": kind, "status": "active"}):
            same = await call.records.read("watch", record["ref"])
            if all(str(same["fields"].get(k) or "") == str(v or "") for k, v in spec.items()):
                return {"watch_ref": record["ref"], "url": url, "final_url": fetched.final_url,
                        "kind": kind, "already": True, "found": bool(int(same["fields"].get("found") or 0)),
                        "value": str(same["fields"].get("value") or ""),
                        "excerpt": str(same["fields"].get("excerpt") or ""),
                        "checked_at": str(same["fields"].get("checked_at") or "")}, "success"
        try:
            reading = await asyncio.to_thread(Reading, fetched, page, spec.get("selector", ""))
            seen = await asyncio.to_thread(reading.look, {"kind": kind, **spec})
        except TooSlow as exc:
            return {"error": str(exc), "kind": "too_slow"}, "error"
        snapshot, kept = await asyncio.to_thread(pack, reading.lines)
        fields = {"status": "active", "kind": kind, "url": url, "label": label,
                  "final_url": reading.final_url, **spec,
                  "value": seen["value"], "found": 1 if seen["found"] else 0,
                  "excerpt": seen["excerpt"], "hash": reading.hash, **snapshot,
                  "snapshot_complete": 1 if kept == len(reading.lines) else 0,
                  "checked_at": reading.at, "tried_at": reading.at, "changed_at": "",
                  "failures": 0, "last_error": ""}
        if kind == "number" and seen["number"] is not None:
            fields["number"] = shown(seen["number"])
            fields["baseline"] = fields["number"]
            fields["met"] = 1 if self._met(seen["number"], spec) else 0
        ref = await call.records.create("watch", fields)
        result = {"watch_ref": ref, "url": url, "final_url": reading.final_url, "kind": kind,
                  "target": spec.get("target", ""), "found": seen["found"], "value": seen["value"],
                  "excerpt": seen["excerpt"], "checked_at": reading.at, "already": False}
        if spec.get("selector"):
            result["selector"] = spec["selector"]
        if kind == "number" and seen["number"] is not None:
            result["written"] = seen["written"]
            result["met"] = bool(fields["met"])
        if kind != "page" and not seen["found"]:
            result["note"] = "Not on the page now; the watch will report when it appears."
        return result, "success"

    async def list(self, call):
        rows = await listing(call, "watch", self._described,
                             include_closed=bool(call.inputs.get("include_closed")))
        rows.sort(key=lambda row: row.get("checked_at") or "")
        return {"watches": rows[:LIST_SHOWN], "total": len(rows)}, "success"

    async def close(self, call):
        ref = str(call.inputs["watch_ref"])
        try:
            done = await close(call, "watch", ref)
        except ResourceDenied:
            return {"error": f"There is no watch {ref}.", "kind": "not_found"}, "error"
        return done, "success"

    async def remove(self, call):
        ref = str(call.inputs["watch_ref"])
        try:
            record = await call.records.read("watch", ref)
        except ResourceDenied:
            return {"error": f"There is no watch {ref}.", "kind": "not_found"}, "error"
        deleted = await call.records.delete("watch", ref)
        return {"removed": bool(deleted), "watch_ref": ref,
                "url": str(record["fields"].get("url") or "")}, "success"

    async def check_now(self, call):
        ref = str(call.inputs["watch_ref"])
        try:
            record = await call.records.read("watch", ref)
        except ResourceDenied:
            return {"error": f"There is no watch {ref}.", "kind": "not_found"}, "error"
        await call.progress(f"Checking {record['fields'].get('url')}")
        try:
            seen = await self._evaluate(record, int(call.inputs.get("max_items") or 20))
        except Gone as gone:
            return {"error": str(gone), "kind": "gone"}, "error"
        except HttpError as failed:
            return failed.answer()
        # Shown, never stored: the next scheduled check still compares with the kept reading.
        changes, items = self._split(seen.rows)
        result = {"watch_ref": ref, "url": str(record["fields"].get("url") or ""),
                  "kind": str(record["fields"].get("kind") or ""), "changed": bool(seen.rows),
                  "checked_at": now_utc()}
        if changes:
            result["change"] = changes[0]
        if items or result["kind"] == "feed":
            result["items"] = items
            result["more"] = seen.more
        return result, "success"

    async def changed(self, call):
        most = int(call.inputs.get("max_items") or 20)
        async with turn():
            wanted = str(call.inputs.get("watch_ref") or "")
            if wanted:
                try:
                    records = [await call.records.read("watch", wanted)]
                except ResourceDenied:
                    return {"error": f"There is no watch {wanted}.", "kind": "not_found"}, "error"
            else:
                records = await call.records.list("watch")
            records = active(records)
            records.sort(key=lambda r: str(r["fields"].get("tried_at") or ""))
            gate, tasks = asyncio.Semaphore(AT_ONCE), {}

            async def evaluated(record):
                async with gate:
                    whole = record if wanted else await call.records.read("watch", record["ref"])
                    return await self._evaluate(whole, most)

            for record in records:
                tasks[record["ref"]] = asyncio.ensure_future(evaluated(record))

            async def read(record):
                return await tasks[record["ref"]]

            try:
                result = await check(call, "watch", records, read, budget=RUN_BUDGET_SECONDS,
                                     name=lambda r: str(r["fields"].get("label") or r["fields"].get("url") or r["ref"]))
            finally:
                for task in tasks.values():
                    if not task.done():
                        task.cancel()
            known = {r["ref"]: r["fields"] for r in records}
            for problem in result["problems"]:
                keys = known.get(problem["watch_ref"]) or {}
                failures = int(keys.get("failures") or 0) + 1
                problem.update({"url": str(keys.get("url") or ""), "failures": failures})
                try:
                    await call.records.update("watch", problem["watch_ref"], {
                        "failures": failures, "last_error": str(problem["error"])[:500],
                        "tried_at": now_utc()})
                except ResourceDenied:
                    pass
        changes, items = self._split(result["rows"])
        return {"checked": result["checked"], "changed": changes, "items": items,
                "problems": result["problems"], "closed": result["closed"],
                "more": result["more"]}, "success"

    # -- reading one watch --------------------------------------------------------------------------------------
    async def _evaluate(self, record, most: int) -> Seen:
        """What is new on one watch, and its fields once handed on; failures as HttpError or Gone."""
        keys = record["fields"]
        failures = int(keys.get("failures") or 0)
        try:
            if str(keys.get("kind") or "") == "feed":
                return await self._feed_news(record, most)
            return await self._page_news(record)
        except FetchError as exc:
            if exc.status == 410:
                raise Gone(f"{keys.get('url')} answered 410 Gone: the page was removed.")
            if exc.status == 404 and failures + 1 >= GONE_AFTER_404:
                raise Gone(f"{keys.get('url')} answered 404 Not Found on {failures + 1} checks in a row.")
            raise HttpError(exc.kind if exc.kind != "not_found" else "http", str(exc), exc.status)
        except TooSlow as exc:
            raise HttpError("too_slow", str(exc))
        except FeedProblem as exc:
            raise HttpError("not_a_feed", str(exc))
        except UnsupportedContent as exc:
            raise HttpError("unsupported", str(exc))
        except (HttpError, Gone, ResourceDenied, asyncio.CancelledError):
            raise
        except Exception as exc:  # one watch's surprise never fails the others
            raise HttpError("failed", f"Checking it failed: {type(exc).__name__}: {exc}"[:500])

    async def _page_news(self, record) -> Seen:
        ref, keys = record["ref"], record["fields"]
        fetched = await fetch(str(keys.get("url") or ""), USER_AGENT)
        page = await asyncio.to_thread(read_page, fetched)
        if page.needs_browser:
            raise HttpError(page.browser_reason, f"{fetched.final_url}: {page.browser_note}" + BROWSER)
        reading = await asyncio.to_thread(Reading, fetched, page, str(keys.get("selector") or ""))
        now = await asyncio.to_thread(reading.look, keys)
        entry, cursor = await asyncio.to_thread(self._compare, ref, keys, reading, now)
        return Seen(rows=[{"_row": "change", **entry}] if entry["changed"] else [], cursor=cursor)

    def _compare(self, ref, keys, reading, now) -> tuple:
        kind = str(keys.get("kind") or "page")
        was_found = bool(int(keys.get("found") or 0))
        was_value = str(keys.get("value") or "")
        entry = {"watch_ref": ref, "label": str(keys.get("label") or ""),
                 "url": str(keys.get("url") or ""), "final_url": reading.final_url, "kind": kind,
                 "target": str(keys.get("target") or ""), "found_before": was_found,
                 "found_after": now["found"], "before_checked_at": str(keys.get("checked_at") or ""),
                 "checked_at": reading.at}
        cursor = {"final_url": reading.final_url, "value": now["value"],
                  "found": 1 if now["found"] else 0, "excerpt": now["excerpt"], "hash": reading.hash,
                  "checked_at": reading.at, "tried_at": reading.at, "failures": 0, "last_error": ""}
        if kind in ("page", "selector"):
            changed = reading.hash != str(keys.get("hash") or "")
            entry.update(before=str(keys.get("excerpt") or ""), after=now["excerpt"])
            if kind == "selector" and now["found"] != was_found:
                entry["after"] = now["excerpt"] if now["found"] else "(the part is no longer on the page)"
            if changed:
                before = unpack(keys)
                entry.update(diff(before, reading.lines))
                complete = bool(int(keys.get("snapshot_complete") or 0))
                if not entry["diff"] and not complete:
                    entry["note"] = (f"The text changed past the first {len(before):,} lines kept for "
                                     f"comparison; read the page to see where.")
                elif not complete:
                    entry["note"] = (f"The diff covers the first {len(before):,} lines kept; the text "
                                     f"may have changed further down too.")
            snapshot, kept = pack(reading.lines)
            cursor.update(snapshot)
            cursor["snapshot_complete"] = 1 if kept == len(reading.lines) else 0
        elif kind == "contains":
            changed = now["found"] != was_found
            entry.update(before="present" if was_found else "absent",
                         after="present" if now["found"] else "absent",
                         excerpt_before=str(keys.get("excerpt") or ""), excerpt_after=now["excerpt"])
        elif kind == "pattern":
            changed = now["found"] != was_found or now["value"] != was_value
            entry.update(before=was_value if was_found else "(no match)",
                         after=now["value"] if now["found"] else "(no match)",
                         excerpt_before=str(keys.get("excerpt") or ""), excerpt_after=now["excerpt"])
        else:
            changed = self._number_change(keys, now, entry, cursor)
        entry["changed"] = changed
        if changed:
            cursor["changed_at"] = reading.at
        return entry, cursor

    def _number_change(self, keys, now, entry, cursor) -> bool:
        """A number's change, by the watch's rule; every comparison and sum in exact decimals."""
        spec = {k: keys.get(k) for k in ("above", "below", "change_percent")}
        was, value = decimal(keys.get("number")), now["number"]
        was_met = bool(int(keys.get("met") or 0))
        entry.update(before=shown(was) if was is not None else "(not found)",
                     after=shown(value) if value is not None else "(not found)",
                     written=now["written"], excerpt_after=now["excerpt"])
        if value is None:
            # Gone from the page: said once, and the last value is kept to compare with.
            cursor.update(value=str(keys.get("value") or ""), found=0)
            if bool(int(keys.get("found") or 0)):
                entry["event"] = "gone"
                return True
            return False
        cursor["number"] = shown(value)
        if was is not None:
            entry["difference"] = shown(value - was)
            if was != 0:
                entry["percent"] = shown(((value - was) / abs(was) * 100).quantize(Decimal("0.01")))
        thresholds = spec["above"] not in (None, "") or spec["below"] not in (None, "")
        met = self._met(value, spec)
        cursor["met"] = 1 if met else 0
        entry["met"] = met
        reasons = []
        if thresholds and met != was_met:
            reasons.append("met" if met else "no_longer_met")
        percent = decimal(spec["change_percent"])
        if percent is not None:
            base = decimal(keys.get("baseline"))
            if base is None:
                cursor["baseline"] = shown(value)
            else:
                moved = abs(value - base) / abs(base) * 100 if base != 0 else (Decimal(100) if value != 0 else Decimal(0))
                entry["percent_since_reported"] = shown(moved.quantize(Decimal("0.01")))
                if moved >= percent:
                    reasons.append("moved")
                    cursor["baseline"] = shown(value)
        if not thresholds and percent is None and (was is None or value != was):
            reasons.append("changed")
        if not bool(int(keys.get("found") or 0)) and was is None and not reasons:
            reasons.append("appeared")
        if reasons:
            entry["event"] = reasons[0]
        return bool(reasons)

    @staticmethod
    def _met(value, spec) -> bool:
        above, below = decimal(spec.get("above")), decimal(spec.get("below"))
        return bool((above is not None and value > above) or (below is not None and value < below))

    async def _feed_news(self, record, most: int) -> Seen:
        ref, keys = record["ref"], record["fields"]
        feed = await fetch_feed(str(keys.get("url") or ""))
        blob = str(keys.get("seen") or "")
        old = [blob[at:at + 12] for at in range(0, len(blob), 12)]
        known = set(old)
        at = now_utc()
        current, fresh = [], []
        for position, item in enumerate(feed.items):
            if item.key in current:
                continue
            current.append(item.key)
            if item.key not in known:
                fresh.append((position, item))
        cursor = {"feed_title": feed.title or str(keys.get("feed_title") or ""), "format": feed.format,
                  "checked_at": at, "tried_at": at, "failures": 0, "last_error": "", "primed": 1}
        if not int(keys.get("primed") or 0):
            # The first look: what the feed holds already is not news.
            cursor["seen"] = self._seen(current, old)
            return Seen(cursor=cursor)
        wanted = [w for w in str(keys.get("keywords") or "").split("\n") if w]
        unwanted = [w for w in str(keys.get("exclude") or "").split("\n") if w]
        candidates, skipped = [], set()
        for position, item in fresh:
            words = f"{item.title}\n{item.text}"
            if (wanted and not any(phrase_pattern(w).search(words) for w in wanted)) or \
                    any(phrase_pattern(w).search(words) for w in unwanted):
                skipped.add(item.key)            # not news by the person's filter, and not again
            else:
                candidates.append((position, item))
        # Oldest first: dated by time, then undated; a feed lists newest first.
        candidates.sort(key=lambda c: (0 if c[1].published else 1, c[1].published, -c[0]))
        handed = candidates[:most]
        handed_keys = {item.key for _, item in handed}
        kept = [k for k in current if k in known or k in handed_keys or k in skipped]
        cursor["seen"] = self._seen(kept, old)
        if handed:
            cursor["changed_at"] = at
        rows = [{"_row": "item", "watch_ref": ref, "label": str(keys.get("label") or ""),
                 "feed_title": cursor["feed_title"], "feed_url": feed.url, **item.row()}
                for _, item in handed]
        return Seen(rows=rows, cursor=cursor, more=len(candidates) > most)

    @staticmethod
    def _seen(first, then) -> str:
        kept = list(dict.fromkeys(list(first) + [k for k in then if k not in set(first)]))
        return "".join(kept[:SEEN_MOST])

    async def _add_feed(self, call, url, label, spec):
        try:
            feed, page_url = await resolve_feed(url)
        except FetchError as exc:
            out = exc.result()
            if exc.needs_browser:
                out["error"] += BROWSER
            return out, "error"
        except FeedProblem as exc:
            return {"error": str(exc), "kind": "not_a_feed"}, "error"
        for record in await call.records.list("watch", {"url": feed.url, "kind": "feed", "status": "active"}):
            return {"watch_ref": record["ref"], "url": feed.url, "kind": "feed", "feed_url": feed.url,
                    "discovered_from": page_url, "feed_title": feed.title, "format": feed.format,
                    "items": len(feed.items), "already": True, "found": True, "value": "",
                    "excerpt": "", "checked_at": str(record["fields"].get("checked_at") or ""),
                    "final_url": feed.url}, "success"
        at = now_utc()
        keys = list(dict.fromkeys(item.key for item in feed.items))
        ref = await call.records.create("watch", {
            "status": "active", "kind": "feed", "url": feed.url, "label": label or feed.title[:120],
            "final_url": feed.url, "discovered_from": page_url, "feed_title": feed.title,
            "format": feed.format, "seen": self._seen(keys, []), "primed": 1, **spec,
            "checked_at": at, "tried_at": at, "changed_at": "", "failures": 0, "last_error": ""})
        return {"watch_ref": ref, "url": feed.url, "final_url": feed.url, "kind": "feed",
                "feed_url": feed.url, "discovered_from": page_url, "feed_title": feed.title,
                "format": feed.format, "items": len(feed.items),
                "latest": [item.row() for item in feed.items[:3]], "already": False, "found": True,
                "value": "", "excerpt": "", "checked_at": at}, "success"

    # -- helpers --------------------------------------------------------------------------------------------------
    @staticmethod
    def _spec(kind: str, inputs: dict) -> tuple:
        """(the watch's own fields, refusal) from what the person asked."""
        phrase = " ".join(str(inputs.get("phrase") or "").split())
        pattern = str(inputs.get("pattern") or "")
        selector = str(inputs.get("selector") or "").strip()
        spec = {}
        if selector:
            if kind == "feed":
                return {}, "A feed watch takes no CSS selector."
            refusal = selector_refusal(selector)
            if refusal:
                return {}, refusal
            spec["selector"] = selector
        if kind == "selector" and not selector:
            return {}, "A selector watch needs the CSS selector of the part to watch, such as #price or .status."
        if kind == "contains":
            if not phrase:
                return {}, "A contains watch needs the phrase to look for."
            spec["target"] = phrase
        elif kind == "pattern":
            if not pattern.strip():
                return {}, "A pattern watch needs a regular expression."
            refusal = pattern_refusal(pattern)
            if refusal:
                return {}, refusal
            spec["target"] = pattern
        elif kind == "number":
            if pattern.strip():
                refusal = pattern_refusal(pattern)
                if refusal:
                    return {}, refusal
                spec.update(locate="pattern", target=pattern)
            elif phrase:
                spec.update(locate="phrase", target=phrase)
            elif selector:
                spec.update(locate="part", target="")
            else:
                return {}, ("A number watch needs to know where the number is: the words just before it "
                            "(phrase, such as \"Price:\"), a pattern, or a CSS selector.")
            comma = inputs.get("decimal_comma")
            if comma is not None:
                spec["decimal_comma"] = 1 if comma else 0
            for name in ("above", "below"):
                given = inputs.get(name)
                if given not in (None, ""):
                    value = number_of(str(given), bool(comma) if comma is not None else None)
                    if value is None:
                        return {}, f"{name} is not a number: {given!r}."
                    spec[name] = shown(value)
            if inputs.get("change_percent") not in (None, ""):
                percent = number_of(str(inputs["change_percent"]))
                if percent is None or percent <= 0:
                    return {}, "change_percent must be a number above 0, such as 5 for 5%."
                spec["change_percent"] = shown(percent)
        elif kind == "feed":
            for name in ("keywords", "exclude"):
                words = [" ".join(str(w).split()) for w in inputs.get(name) or []]
                if any(words):
                    spec[name] = "\n".join(w for w in words if w)
        elif kind not in KINDS:
            return {}, f"No watch kind {kind}."
        return spec, ""

    @staticmethod
    def _split(rows) -> tuple:
        changes, items = [], []
        for row in rows:
            row = dict(row)
            (items if row.pop("_row", "") == "item" else changes).append(row)
        return changes, items

    @staticmethod
    def _described(record) -> dict:
        keys = record["fields"]
        return {"label": str(keys.get("label") or ""), "url": str(keys.get("url") or ""),
                "kind": str(keys.get("kind") or ""), "checked_at": str(keys.get("checked_at") or ""),
                "changed_at": str(keys.get("changed_at") or ""), "failures": int(keys.get("failures") or 0),
                "last_error": str(keys.get("last_error") or "")}


def checked_address(url: str) -> str:
    """Why an address would never be fetched (scheme, credentials, a private address written out), or ''."""
    try:
        SafeFetcher(USER_AGENT).check_url(url, resolve=False)
    except FetchError as exc:
        return str(exc)
    return ""
