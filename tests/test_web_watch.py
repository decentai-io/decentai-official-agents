"""The Web Watch agent's program (Feeds merged in), watching fictional
pages and feeds on a loopback web server that the tests change between
checks. Each change must be handed on exactly once; a page that cannot
be read is a problem, never a change, and never costs another watch
its news.
"""

import asyncio
import time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.harness import Chat, Records, probe, run
from tests.web_stub import LOOPBACK_VARIABLE, WebStub, web  # noqa: F401

ROOT = Path(__file__).resolve().parents[1] / "agents"
QUIET = {"changed": [], "items": [], "problems": [], "closed": [], "more": False}


def product(price: str, stock: str = "In stock") -> str:
    return f"""<html><head><title>Northlight Desk Lamp</title></head><body>
    <nav><a href="/">Shop</a> <span>{price} basket</span></nav>
    <h1>Northlight Desk Lamp</h1>
    <p class="price">Price: USD {price}</p>
    <p>{stock}. Ships in 2 working days.</p>
    </body></html>"""


def news(*items: str) -> str:
    rows = "".join(f"<li>{item}</li>" for item in items)
    return f"<html><body><h1>Riverside Book Fair news</h1><ul>{rows}</ul></body></html>"


def make():
    provider = Records()
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["web_watch"], name, inputs, chat_level=chat_level))


def records(provider):
    return provider.data.get("watch", {})


def quiet(result, checked=1):
    return result == {"checked": checked, **QUIET}


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        assert probe(agents["web_watch"]) == []

    def test_changed_is_schedulable_at_level_one_without_a_model(self, agents):
        function = agents["web_watch"].function("watches_changed")
        assert function["schedulable"] is True and function["level"] == 1
        assert "model" not in function["calls"]
        levels = {name: agents["web_watch"].function(name)["level"] for name in (
            "watches_add", "watches_list", "watches_close", "watches_remove", "watches_check_now",
            "feeds_read", "feeds_import")}
        assert levels == {"watches_add": 1, "watches_list": 0, "watches_close": 1, "watches_remove": 2,
                          "watches_check_now": 0, "feeds_read": 0, "feeds_import": 1}

    def test_the_fetch_code_is_the_same_in_both_web_agents(self):
        """Each program carries only its own folder: the shared code is two identical copies."""
        for name in ("fetch.py", "page_text.py"):
            reader = (ROOT / "web_reader" / "tools" / name).read_bytes()
            watch = (ROOT / "web_watch" / "tools" / name).read_bytes()
            assert reader == watch, f"tools/{name} differs between web_reader and web_watch"

    def test_feeds_was_absorbed(self):
        assert not (ROOT / "feeds").exists()

    def test_the_instructions_send_javascript_and_sign_ins_to_the_browser(self, agents):
        words = agents["web_watch"].manifest["agent"]["instructions"]
        assert "JavaScript" in words and "sign-in" in words and "Browser agent" in words


class TestPatternWatch:
    def test_a_price_change_is_reported_once_with_before_and_after(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        added, status = invoke(agents, ex, "watches_add", {
            "url": url, "kind": "pattern", "pattern": r"Price: USD\s+([\d.,]+)",
            "label": "Desk lamp price"})
        assert status == "success", added
        assert (added["found"], added["value"]) == (True, "129.00")
        assert "Price: USD 129.00" in added["excerpt"]
        # What the page says is content: kept encrypted, not in plain keys.
        record = records(provider)[added["watch_ref"]]
        assert "value" in record["values"] and "target" in record["values"]
        assert record["keys"]["status"] == "active"

        result, status = invoke(agents, ex, "watches_changed", {})
        assert status == "success" and quiet(result), result

        web.page("/lamp", product("119.00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert (change["before"], change["after"]) == ("129.00", "119.00")
        assert change["watch_ref"] == added["watch_ref"] and change["label"] == "Desk lamp price"
        again, _ = invoke(agents, ex, "watches_changed", {})
        assert again["changed"] == []

    def test_other_text_changing_is_not_a_pattern_change(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, _ = make()
        invoke(agents, ex, "watches_add", {"url": url, "kind": "pattern", "pattern": r"USD\s+([\d.]+)"})
        web.page("/lamp", product("129.00", stock="Only 2 left"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []

    def test_an_invalid_regular_expression_is_refused_with_why(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        result, status = invoke(agents, ex, "watches_add", {
            "url": url, "kind": "pattern", "pattern": r"Price: (USD"})
        assert status == "error" and result["kind"] == "invalid"
        assert "missing )" in result["error"]
        assert records(provider) == {} and web.hits == {}

    def test_a_pattern_too_slow_is_refused_and_never_hangs_the_agent(self, agents, web):
        url = web.page("/codes", f"<html><body><p>{'a' * 60}b</p></body></html>")
        ex, provider = make()
        started = time.monotonic()
        result, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "pattern", "pattern": r"(a|aa)+$"})
        assert status == "error" and result["kind"] == "too_slow", result
        assert time.monotonic() - started < 15 and records(provider) == {}
        # The program still answers.
        lamp = web.page("/lamp", product("129.00"))
        added, status = invoke(agents, ex, "watches_add", {"url": lamp, "kind": "page"})
        assert status == "success", added

    def test_a_pattern_that_turns_slow_is_one_watchs_problem_not_the_runs(self, agents, web):
        codes = web.page("/codes", "<html><body><p>Status: bbb</p></body></html>")
        lamp = web.page("/lamp", product("129.00"))
        ex, provider = make()
        slow, _ = invoke(agents, ex, "watches_add", {"url": codes, "kind": "pattern", "pattern": r"(a|aa)+$"})
        fine, _ = invoke(agents, ex, "watches_add", {"url": lamp, "kind": "pattern", "pattern": r"USD ([\d.]+)"})
        before = dict(records(provider)[slow["watch_ref"]]["values"])
        web.page("/codes", f"<html><body><p>{'a' * 60}b</p></body></html>")
        web.page("/lamp", product("99.00"))
        result, status = invoke(agents, ex, "watches_changed", {})
        assert status == "success", result
        assert [c["watch_ref"] for c in result["changed"]] == [fine["watch_ref"]]
        (problem,) = result["problems"]
        assert problem["watch_ref"] == slow["watch_ref"] and problem["kind"] == "too_slow"
        assert problem["failures"] == 1
        assert records(provider)[slow["watch_ref"]]["values"]["hash"] == before["hash"]


class TestContainsWatch:
    def test_a_phrase_appearing_and_disappearing_is_reported_each_time_once(self, agents, web):
        url = web.page("/news", news("Author talks announced"))
        ex, _ = make()
        added, status = invoke(agents, ex, "watches_add", {
            "url": url, "kind": "contains", "phrase": "registration   open"})
        assert status == "success", added
        assert added["found"] is False and "note" in added and added["target"] == "registration open"

        web.page("/news", news("Author talks announced", "Registration\nopen for stall holders"))
        appeared, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = appeared["changed"]
        assert (change["before"], change["after"]) == ("absent", "present")
        assert "Registration open for stall holders" in change["excerpt_after"]
        again, _ = invoke(agents, ex, "watches_changed", {})
        assert again["changed"] == []

        web.page("/news", news("Registration closed"))
        gone, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = gone["changed"]
        assert (change["before"], change["after"]) == ("present", "absent")

    def test_an_arabic_phrase_on_a_windows_1256_page(self, agents, web):
        page = "<html><head><meta charset=\"windows-1256\"></head><body><h1>أخبار المعرض</h1><ul>{}</ul></body></html>"
        url = web.page("/ar", page.format("<li>افتتاح المعرض</li>").encode("cp1256"), "text/html")
        ex, _ = make()
        added, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "contains", "phrase": "التسجيل مفتوح"})
        assert status == "success" and added["found"] is False, added
        web.page("/ar", page.format("<li>التسجيل مفتوح الآن</li>").encode("cp1256"), "text/html")
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert change["after"] == "present" and "التسجيل مفتوح الآن" in change["excerpt_after"]

    def test_a_contains_watch_needs_its_phrase(self, agents, web):
        ex, provider = make()
        result, status = invoke(agents, ex, "watches_add", {"url": web.url + "/news", "kind": "contains"})
        assert status == "error" and result["kind"] == "invalid"
        assert records(provider) == {}


class TestPageWatch:
    def test_a_text_change_is_reported_once_with_a_line_diff(self, agents, web):
        url = web.page("/news", news("Author talks announced", "Parking opens at 8"))
        ex, _ = make()
        added, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
        assert status == "success", added
        web.page("/news", news("Author   talks\n announced", "Parking opens at 8"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []          # whitespace alone is not a change

        web.page("/news", news("Author talks announced", "Parking opens at 9"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert change["kind"] == "page"
        assert change["diff"] == ["- - Parking opens at 8", "+ - Parking opens at 9"]
        assert (change["diff_truncated"], change["lines_added"], change["lines_removed"]) == (False, 1, 1)
        again, _ = invoke(agents, ex, "watches_changed", {})
        assert again["changed"] == []

    def test_a_change_far_down_a_long_page_is_in_the_diff(self, agents, web):
        """The old snapshot kept 8,000 characters, so this change had no diff."""
        rows = [f"Stall {n}: handmade books and prints from the riverside studios" for n in range(1, 1201)]
        url = web.page("/stalls", news(*rows))
        ex, _ = make()
        invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
        rows[1099] = "Stall 1100: moved to the east hall"
        web.page("/stalls", news(*rows))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert "+ - Stall 1100: moved to the east hall" in change["diff"]
        assert "note" not in change

    def test_a_big_change_says_how_big_when_the_diff_is_cut(self, agents, web):
        rows = [f"Stall {n}: open" for n in range(1, 201)]
        url = web.page("/stalls", news(*rows))
        ex, _ = make()
        invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
        web.page("/stalls", news(*[f"Stall {n}: closed" for n in range(1, 201)]))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert change["diff_truncated"] is True
        assert (change["lines_added"], change["lines_removed"]) == (200, 200)

    def test_check_now_shows_the_change_without_moving_the_reading(self, agents, web):
        url = web.page("/news", news("Author talks announced"))
        ex, _ = make()
        added, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
        web.page("/news", news("Author talks postponed"))
        for _ in range(2):
            shown, status = invoke(agents, ex, "watches_check_now", {"watch_ref": added["watch_ref"]})
            assert status == "success" and shown["changed"] is True, shown
            assert "- - Author talks announced" in shown["change"]["diff"]
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert len(result["changed"]) == 1

    def test_a_javascript_page_or_a_bot_check_cannot_be_watched(self, agents, web):
        shell = web.page("/app", "<html><head><script src='/app.js'></script></head><body>"
                                 "<noscript>You need to enable JavaScript to run this app.</noscript>"
                                 "<div id='root'></div></body></html>")
        web.routes["/wall"] = (403, {"Content-Type": "text/html"},
                               b"<html><head><title>Just a moment...</title></head><body>"
                               b"<script src='/cdn-cgi/challenge-platform/h/b'></script></body></html>")
        ex, provider = make()
        result, status = invoke(agents, ex, "watches_add", {"url": shell, "kind": "page"})
        assert status == "error" and result["kind"] == "javascript"
        assert "Browser agent" in result["error"] and '"needs_browser": true' in result["error"]
        result, status = invoke(agents, ex, "watches_add", {"url": web.url + "/wall", "kind": "page"})
        assert status == "error" and result["kind"] == "blocked" and "Browser agent" in result["error"]
        assert records(provider) == {}


class TestSelectorWatch:
    STATUS = """<html><body><h1>Harbourline status</h1><p>Updated {time}</p>
    <div id="status"><h2>Exports</h2><p class="state">{state}</p></div>
    <aside>Tip of the day: {tip}</aside></body></html>"""

    def test_only_the_part_picked_is_watched(self, agents, web):
        url = web.page("/status", self.STATUS.format(time="09:00", state="Operational", tip="save often"))
        ex, _ = make()
        added, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "selector", "selector": "#status"})
        assert status == "success", added
        assert added["selector"] == "#status" and "Operational" in added["excerpt"]
        web.page("/status", self.STATUS.format(time="09:05", state="Operational", tip="name layers"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []
        web.page("/status", self.STATUS.format(time="09:10", state="Degraded performance", tip="x"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert change["diff"] == ["- Operational", "+ Degraded performance"]

    def test_a_part_that_disappears_is_said_once(self, agents, web):
        url = web.page("/status", self.STATUS.format(time="09:00", state="Operational", tip="a"))
        ex, _ = make()
        invoke(agents, ex, "watches_add", {"url": url, "kind": "selector", "selector": "#status .state"})
        web.page("/status", "<html><body><h1>Harbourline status</h1><p>Moved to a new page.</p></body></html>")
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert change["found_after"] is False and "no longer on the page" in change["after"]
        again, _ = invoke(agents, ex, "watches_changed", {})
        assert again["changed"] == []

    def test_an_invalid_selector_is_refused(self, agents, web):
        ex, provider = make()
        result, status = invoke(agents, ex, "watches_add", {"url": web.url + "/x", "kind": "selector",
                                                            "selector": "div[[class"})
        assert status == "error" and result["kind"] == "invalid" and "selector" in result["error"]
        assert records(provider) == {} and web.hits == {}


class TestNumberWatch:
    def test_a_price_falling_below_a_threshold_is_said_when_it_crosses(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, _ = make()
        added, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "number", "phrase": "Price: USD",
                                                           "below": 100})
        assert status == "success", added
        assert (added["value"], added["written"], added["met"]) == ("129", "129.00", False)
        web.page("/lamp", product("119.00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []          # lower, but not below 100
        web.page("/lamp", product("99.50"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert (change["event"], change["before"], change["after"], change["met"]) == ("met", "119", "99.5", True)
        assert (change["difference"], change["percent"]) == ("-19.5", "-16.39")
        web.page("/lamp", product("98.00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []          # still below: said already
        web.page("/lamp", product("105.00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"][0]["event"] == "no_longer_met"

    def test_a_decimal_comma_and_a_percentage_move(self, agents, web):
        page = "<html><body><h1>Schreibtischlampe</h1><p>Preis: 1.299,00 €</p></body></html>"
        url = web.page("/lampe", page)
        ex, _ = make()
        added, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "number", "phrase": "Preis:",
                                                           "decimal_comma": True, "change_percent": 5})
        assert status == "success" and added["value"] == "1299", added
        web.page("/lampe", page.replace("1.299,00", "1.249,00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []          # 3.85% from 1299
        web.page("/lampe", page.replace("1.299,00", "1.199,00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert (change["event"], change["after"], change["percent_since_reported"]) == ("moved", "1199", "7.7")
        web.page("/lampe", page.replace("1.299,00", "1.150,00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []          # 4.09% from the 1199 reported

    def test_arabic_indic_digits_on_an_arabic_page(self, agents, web):
        page = "<html><body><h1>مصباح المكتب</h1><p>السعر: {} ريال</p></body></html>"
        url = web.page("/misbah", page.format("١٢٩٫٥٠"))
        ex, _ = make()
        added, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "number", "phrase": "السعر:",
                                                           "below": "100"})
        assert status == "success" and added["value"] == "129.5", added
        web.page("/misbah", page.format("٩٩٫٠٠"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert (change["event"], change["after"], change["written"]) == ("met", "99", "٩٩٫٠٠")

    def test_a_number_watch_needs_to_know_where_the_number_is(self, agents, web):
        ex, _ = make()
        result, status = invoke(agents, ex, "watches_add", {"url": web.url + "/lamp", "kind": "number", "below": 5})
        assert status == "error" and result["kind"] == "invalid" and web.hits == {}

    def test_numbers_are_read_in_code(self):
        from agents.web_watch.tools.numbers import number_of

        cases = {("1,299.00", None): "1299.00", ("1.299,00", None): "1299.00", ("1 299,50", None): "1299.50",
                 ("1'299.00", None): "1299.00", ("129,5", None): "129.5", ("1,299", None): "1299",
                 ("1.299", True): "1299", ("1,299", True): "1.299", ("0.125", None): "0.125",
                 ("١٬٢٩٩٫٥٠", None): "1299.50", ("۱۲۹", None): "129", ("−4.5", None): "-4.5"}
        for (written, comma), value in cases.items():
            assert number_of(written, comma) == Decimal(value), written


class TestDelivery:
    def test_a_failed_fetch_is_a_problem_counted_and_never_a_change(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        added, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "pattern", "pattern": r"USD\s+([\d.]+)"})
        ref = added["watch_ref"]
        web.fail("/lamp", 503)
        first, status = invoke(agents, ex, "watches_changed", {})
        assert status == "success", first
        assert first["changed"] == []
        assert [(p["watch_ref"], p["failures"]) for p in first["problems"]] == [(ref, 1)]
        assert "503" in first["problems"][0]["error"]
        second, _ = invoke(agents, ex, "watches_changed", {})
        assert second["problems"][0]["failures"] == 2
        assert records(provider)[ref]["values"]["value"] == "129.00"
        web.page("/lamp", product("129.00"))
        back, _ = invoke(agents, ex, "watches_changed", {})
        assert quiet(back) and records(provider)[ref]["keys"]["failures"] == 0

    def test_one_failing_watch_costs_the_others_nothing_and_loses_nothing(self, agents, web):
        lamp = web.page("/lamp", product("129.00"))
        board = web.page("/news", news("Author talks announced"))
        ex, _ = make()
        a, _ = invoke(agents, ex, "watches_add", {"url": lamp, "kind": "pattern", "pattern": r"USD ([\d.]+)"})
        b, _ = invoke(agents, ex, "watches_add", {"url": board, "kind": "contains", "phrase": "postponed"})
        web.fail("/lamp", 500)
        web.page("/news", news("Author talks postponed"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert [c["watch_ref"] for c in result["changed"]] == [b["watch_ref"]]
        assert [p["watch_ref"] for p in result["problems"]] == [a["watch_ref"]]
        # The failing one's news waits for it, and comes once it can be read.
        web.page("/lamp", product("89.00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        (change,) = result["changed"]
        assert change["watch_ref"] == a["watch_ref"] and (change["before"], change["after"]) == ("129.00", "89.00")

    def test_a_page_gone_for_good_closes_its_watch(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        added, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
        web.fail("/lamp", 410)
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert [c["watch_ref"] for c in result["closed"]] == [added["watch_ref"]]
        assert "410" in result["closed"][0]["why"] and result["problems"] == []
        assert records(provider)[added["watch_ref"]]["keys"]["status"] == "closed"
        after, _ = invoke(agents, ex, "watches_changed", {})
        assert quiet(after, checked=0)

    def test_a_page_missing_five_times_in_a_row_closes_its_watch(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, _ = make()
        added, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
        web.fail("/lamp", 404)
        for attempt in range(1, 5):
            result, _ = invoke(agents, ex, "watches_changed", {})
            assert result["problems"][0]["failures"] == attempt and result["closed"] == []
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert [c["watch_ref"] for c in result["closed"]] == [added["watch_ref"]]

    def test_overlapping_runs_hand_a_change_on_once(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, _ = make()
        invoke(agents, ex, "watches_add", {"url": url, "kind": "pattern", "pattern": r"USD ([\d.]+)"})
        web.page("/lamp", product("119.00"))

        async def twice():
            return await asyncio.gather(*(ex.invoke(agents["web_watch"], "watches_changed", {}) for _ in range(2)))

        results = run(twice())
        assert sum(len(result["changed"]) for result, _ in results) == 1

    def test_a_watch_is_listed_closed_and_removed(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        a, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "page", "label": "Lamp"})
        b, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "contains", "phrase": "sold out"})
        listed, _ = invoke(agents, ex, "watches_list", {})
        assert listed["total"] == 2 and {w["watch_ref"] for w in listed["watches"]} == {a["watch_ref"], b["watch_ref"]}
        assert next(w for w in listed["watches"] if w["watch_ref"] == a["watch_ref"])["label"] == "Lamp"
        closed, status = invoke(agents, ex, "watches_close", {"watch_ref": a["watch_ref"]})
        assert status == "success" and closed == {"watch_ref": a["watch_ref"], "closed": True, "already": False}
        listed, _ = invoke(agents, ex, "watches_list", {})
        assert [w["watch_ref"] for w in listed["watches"]] == [b["watch_ref"]]
        everything, _ = invoke(agents, ex, "watches_list", {"include_closed": True})
        assert everything["total"] == 2
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["checked"] == 1
        removed, status = invoke(agents, ex, "watches_remove", {"watch_ref": b["watch_ref"]}, chat_level=2)
        assert status == "success" and removed["removed"] is True
        assert list(records(provider)) == [a["watch_ref"]]
        missing, status = invoke(agents, ex, "watches_close", {"watch_ref": "data_999"})
        assert status == "error" and missing["kind"] == "not_found"

    def test_the_same_watch_twice_is_the_one_watch(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        first, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "contains", "phrase": "sold out"})
        second, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "contains", "phrase": "sold out"})
        assert second["already"] is True and second["watch_ref"] == first["watch_ref"]
        assert len(records(provider)) == 1


# -- feeds --------------------------------------------------------------------------------------------------------

def rss(*items, title="Riverside Book Fair", head='<?xml version="1.0" encoding="UTF-8"?>') -> str:
    body = ""
    for item in items:
        date = f"<pubDate>{item['date']}</pubDate>" if item.get("date") else ""
        guid = f"<guid isPermaLink=\"false\">{item['id']}</guid>" if item.get("id") else ""
        body += (f"<item><title>{item['title']}</title><link>{item.get('link', '')}</link>"
                 f"{guid}{date}<description><![CDATA[{item.get('summary', '')}]]></description>"
                 f"<dc:creator>{item.get('author', '')}</dc:creator></item>")
    return (f'{head}<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<channel><title>{title}</title><link>https://fair.example/</link>"
            f"{body}</channel></rss>")


def atom(*entries, title="Harbourline Studio Releases") -> str:
    body = "".join(
        f"<entry><id>{e['id']}</id><title>{e['title']}</title>"
        f"<link rel=\"alternate\" href=\"{e['link']}\"/>"
        f"<link rel=\"enclosure\" href=\"https://cdn.harbourline.example/x.zip\"/>"
        f"<updated>{e['updated']}</updated><summary>{e.get('summary', '')}</summary>"
        f"<author><name>Harbourline Team</name></author></entry>"
        for e in entries)
    return (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<feed xmlns="http://www.w3.org/2005/Atom"><title>{title}</title>'
            f"<id>urn:harbourline:releases</id>{body}</feed>")


RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://notes.example/"><title>Field Notes</title></channel>
  <item rdf:about="https://notes.example/2">
    <title>Second note</title><link>https://notes.example/2</link>
    <dc:date>2026-09-10T08:30:00+02:00</dc:date><dc:creator>Mira</dc:creator>
  </item>
  <item rdf:about="https://notes.example/1">
    <title>First note</title><link>https://notes.example/1</link>
    <dc:date>2026-09-01</dc:date>
  </item>
</rdf:RDF>"""

JSON_FEED = """{"version": "https://jsonfeed.org/version/1.1", "title": "Harbourline Journal",
 "items": [{"id": "j-2", "url": "https://journal.harbourline.example/2", "title": "Colour profiles",
            "content_html": "<p>Why <b>sRGB</b> still matters.</p>", "date_published": "2026-09-11T10:00:00+04:00",
            "authors": [{"name": "Noor"}]},
           {"id": "j-1", "url": "https://journal.harbourline.example/1", "title": "Hello",
            "content_text": "The journal opens.", "date_published": "2026-09-01T09:00:00Z"}]}"""

FAIR_ITEMS = [
    {"id": "fair-2", "title": "Programme published", "link": "https://fair.example/programme",
     "date": "Wed, 09 Sep 2026 10:00:00 +0400",
     "summary": "<p>The <b>full programme</b> is out.</p><script>x()</script>", "author": "Fair Office"},
    {"id": "fair-1", "title": "Stall bookings open", "link": "https://fair.example/stalls",
     "date": "Tue, 01 Sep 2026 09:00:00 GMT"},
]

RELEASES = [
    {"id": "urn:harbourline:release:2.1", "title": "Studio 2.1", "link": "https://harbourline.example/2.1",
     "updated": "2026-09-08T12:00:00Z", "summary": "Faster exports."},
    {"id": "urn:harbourline:release:2.0", "title": "Studio 2.0", "link": "https://harbourline.example/2.0",
     "updated": "2026-08-20T09:15:00+01:00"},
]


def page_advertising(href: str, kind: str = "application/rss+xml") -> str:
    return (f'<html><head><title>Riverside Book Fair</title>'
            f'<link rel="stylesheet" href="/site.css">'
            f'<link rel="alternate" type="{kind}" title="News" href="{href}">'
            f"</head><body><h1>Welcome</h1></body></html>")


def follow(agents, ex, url, **more):
    added, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "feed", **more})
    assert status == "success", added
    return added


class TestReadingFeeds:
    def test_an_rss_feed_is_read_with_dates_in_utc_and_plain_summaries(self, agents, web):
        url = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        ex, provider = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": url})
        assert status == "success", result
        assert (result["title"], result["format"], result["total"]) == ("Riverside Book Fair", "rss", 2)
        first = result["items"][0]
        assert first["published"] == "2026-09-09T06:00:00Z"
        assert first["summary"] == "The full programme is out."
        assert first["author"] == "Fair Office" and first["id"] == "fair-2"
        assert provider.data == {}

    def test_an_atom_feed_is_discovered_from_its_web_page(self, agents, web):
        web.page("/releases.atom", atom(*RELEASES), "application/atom+xml")
        page = web.page("/", page_advertising("/releases.atom", "application/atom+xml"))
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": page, "max_results": 1})
        assert status == "success", result
        assert result["feed_url"] == web.url + "/releases.atom" and result["discovered_from"] == page
        assert (result["format"], result["total"], len(result["items"])) == ("atom", 2, 1)
        assert result["items"][0]["link"] == "https://harbourline.example/2.1"

    def test_an_rdf_feed_and_a_json_feed(self, agents, web):
        rdf = web.page("/notes.rdf", RDF, "application/rdf+xml")
        journal = web.page("/feed.json", JSON_FEED, "application/feed+json")
        ex, _ = make()
        result, _ = invoke(agents, ex, "feeds_read", {"url": rdf})
        assert [i["published"] for i in result["items"]] == ["2026-09-10T06:30:00Z", "2026-09-01T00:00:00Z"]
        result, status = invoke(agents, ex, "feeds_read", {"url": journal})
        assert status == "success", result
        assert (result["format"], result["title"]) == ("json", "Harbourline Journal")
        first = result["items"][0]
        assert (first["summary"], first["author"], first["published"]) == (
            "Why sRGB still matters.", "Noor", "2026-09-11T06:00:00Z")

    def test_a_site_with_no_advertised_feed_is_probed(self, agents, web):
        web.page("/blog/", "<html><head><title>Blog</title></head><body><h1>Blog</h1></body></html>")
        web.page("/rss.xml", rss(*FAIR_ITEMS), "text/xml")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": web.url + "/blog/"})
        assert status == "success", result
        assert result["feed_url"] == web.url + "/rss.xml" and result["discovered_from"] == web.url + "/blog/"

    def test_a_site_without_any_feed_says_where_it_looked(self, agents, web):
        url = web.page("/about", "<html><head><title>About</title></head><body>Hi</body></html>")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": url})
        assert status == "error" and result["kind"] == "not_a_feed"
        assert "advertises no RSS, Atom or JSON feed" in result["error"] and "/atom.xml" in result["error"]

    def test_html_entities_and_a_bare_ampersand_are_read(self, agents, web):
        feed = rss({"id": "e-1", "title": "Prints&nbsp;&amp; posters &eacute;dition &mdash; AT&T &hellip;",
                    "link": "https://fair.example/e1", "date": "Thu, 10 Sep 2026 07:00:00 GMT"})
        url = web.page("/feed.xml", feed, "application/rss+xml")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": url})
        assert status == "success", result
        assert result["items"][0]["title"] == "Prints & posters édition — AT&T …"

    def test_a_bom_and_text_xml_are_a_feed(self, agents, web):
        url = web.page("/bom.xml", b"\xef\xbb\xbf\n" + rss(*FAIR_ITEMS).encode("utf-8"), "text/xml")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": url})
        assert status == "success" and result["total"] == 2, result

    def test_an_arabic_feed_in_windows_1256_declared_in_its_xml(self, agents, web):
        items = [{"id": "ar-1", "title": "افتتاح باب التسجيل في المعرض", "link": "https://fair.example/ar/1",
                  "date": "Thu, 10 Sep 2026 07:00:00 GMT", "summary": "<p>التسجيل مفتوح حتى نهاية الشهر</p>"}]
        body = rss(*items, title="أخبار المعرض", head='<?xml version="1.0" encoding="windows-1256"?>')
        url = web.page("/ar.xml", body.encode("cp1256"), "text/xml")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": url, "keywords": ["التسجيل"]})
        assert status == "success", result
        assert result["title"] == "أخبار المعرض"
        assert result["items"][0]["title"] == "افتتاح باب التسجيل في المعرض"
        assert result["items"][0]["summary"] == "التسجيل مفتوح حتى نهاية الشهر"

    def test_a_feed_declaring_entities_is_not_parsed(self, agents, web):
        bomb = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">'
                '<!ENTITY b "&a;&a;&a;&a;">]><rss version="2.0"><channel>'
                '<title>&b;</title></channel></rss>')
        url = web.page("/bomb.xml", bomb, "application/rss+xml")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds_read", {"url": url})
        assert status == "error" and "entities" in result["error"]


class TestFollowingFeeds:
    def test_items_are_handed_on_exactly_once_oldest_first(self, agents, web):
        web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        page = web.page("/", page_advertising("/feed.xml"))
        ex, provider = make()
        added = follow(agents, ex, page)
        assert added["feed_url"] == web.url + "/feed.xml" and added["discovered_from"] == page
        again = follow(agents, ex, web.url + "/feed.xml")
        assert again["already"] is True and again["watch_ref"] == added["watch_ref"]
        assert len(records(provider)) == 1
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert quiet(result), result            # what the feed held already is not news

        newer = [
            {"id": "fair-4", "title": "Parking update", "link": "https://fair.example/parking",
             "date": "Fri, 11 Sep 2026 08:00:00 GMT"},
            {"id": "fair-3", "title": "Author talks", "link": "https://fair.example/talks",
             "date": "Fri, 11 Sep 2026 08:00:00 GMT"},
            {"id": "fair-2b", "title": "Map", "link": "https://fair.example/map",
             "date": "Thu, 10 Sep 2026 07:00:00 GMT"},
        ]
        web.page("/feed.xml", rss(*newer, *FAIR_ITEMS), "application/rss+xml")
        result, _ = invoke(agents, ex, "watches_changed", {"max_items": 2})
        assert [i["id"] for i in result["items"]] == ["fair-2b", "fair-3"]
        assert result["more"] is True and result["items"][0]["feed_title"] == "Riverside Book Fair"
        rest, _ = invoke(agents, ex, "watches_changed", {})
        assert [i["id"] for i in rest["items"]] == ["fair-4"] and rest["more"] is False
        done, _ = invoke(agents, ex, "watches_changed", {})
        assert done["items"] == []

    def test_a_back_dated_item_is_still_news(self, agents, web):
        """News sites and GitHub publish items dated before the newest one; a date cursor dropped them."""
        url = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        ex, _ = make()
        follow(agents, ex, url)
        late = {"id": "fair-1b", "title": "Volunteer briefing (written last week)",
                "link": "https://fair.example/briefing", "date": "Thu, 03 Sep 2026 09:00:00 GMT"}
        web.page("/feed.xml", rss(FAIR_ITEMS[0], late, FAIR_ITEMS[1]), "application/rss+xml")
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert [i["id"] for i in result["items"]] == ["fair-1b"]
        again, _ = invoke(agents, ex, "watches_changed", {})
        assert again["items"] == []

    def test_keywords_and_exclusions_filter_what_is_handed_on(self, agents, web):
        url = web.page("/releases.atom", atom(*RELEASES), "application/atom+xml")
        ex, _ = make()
        follow(agents, ex, url, keywords=["exports", "pricing"], exclude=["beta"])
        web.page("/releases.atom", atom(
            {"id": "r-3", "title": "Studio 2.3 beta", "link": "https://harbourline.example/2.3b",
             "updated": "2026-09-14T10:00:00Z", "summary": "Faster exports, in beta."},
            {"id": "r-2", "title": "Studio 2.2", "link": "https://harbourline.example/2.2",
             "updated": "2026-09-12T10:00:00Z", "summary": "New pricing for teams."},
            {"id": "r-1", "title": "Typo fixes", "link": "https://harbourline.example/t",
             "updated": "2026-09-11T10:00:00Z", "summary": "Small things."},
            *RELEASES), "application/atom+xml")
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert [i["id"] for i in result["items"]] == ["r-2"]
        again, _ = invoke(agents, ex, "watches_changed", {})
        assert again["items"] == []             # the filtered ones are not news later either

    def test_two_feeds_and_a_page_in_one_run(self, agents, web):
        fair = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        releases = web.page("/releases.atom", atom(*RELEASES), "application/atom+xml")
        lamp = web.page("/lamp", product("129.00"))
        ex, _ = make()
        a, b = follow(agents, ex, fair), follow(agents, ex, releases)
        invoke(agents, ex, "watches_add", {"url": lamp, "kind": "number", "phrase": "USD"})
        web.page("/releases.atom", atom(
            {"id": "urn:harbourline:release:2.2", "title": "Studio 2.2",
             "link": "https://harbourline.example/2.2", "updated": "2026-09-12T10:00:00+02:00"},
            *RELEASES), "application/atom+xml")
        web.page("/feed.xml", rss(
            {"id": "fair-5", "title": "Volunteers wanted", "link": "https://fair.example/volunteer",
             "date": "Sat, 12 Sep 2026 09:00:00 GMT"}, *FAIR_ITEMS), "application/rss+xml")
        web.page("/lamp", product("119.00"))
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert sorted((i["title"], i["watch_ref"]) for i in result["items"]) == [
            ("Studio 2.2", b["watch_ref"]), ("Volunteers wanted", a["watch_ref"])]
        assert [c["after"] for c in result["changed"]] == ["119"] and result["checked"] == 3

    def test_a_failing_feed_is_a_problem_and_is_not_news(self, agents, web):
        url = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        ex, _ = make()
        added = follow(agents, ex, url)
        web.page("/feed.xml", "<html><body>Maintenance</body></html>", "text/html")
        first, _ = invoke(agents, ex, "watches_changed", {})
        assert first["items"] == [] and first["problems"][0]["kind"] == "not_a_feed"
        assert first["problems"][0]["watch_ref"] == added["watch_ref"]
        web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        back, _ = invoke(agents, ex, "watches_changed", {})
        assert quiet(back)

    def test_an_opml_list_becomes_feed_watches(self, agents, web):
        fair = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        journal = web.page("/feed.json", JSON_FEED, "application/feed+json")
        ex, provider = make()
        follow(agents, ex, fair)
        opml = f"""<?xml version="1.0" encoding="UTF-8"?><opml version="2.0"><head><title>Mine</title></head>
        <body><outline text="Reading"><outline type="rss" text="Fair" xmlUrl="{fair}"/>
        <outline type="rss" text="Journal &amp; notes" xmlUrl="{journal}"/>
        <outline type="rss" text="Inside" xmlUrl="http://10.0.0.7/feed.xml"/></outline></body></opml>"""
        ref = ex.file("feeds.opml", opml)
        result, status = invoke(agents, ex, "feeds_import", {"file_ref": ref})
        assert status == "success", result
        assert (result["added"], result["already"]) == (1, 1)
        assert result["skipped"][0]["url"] == "http://10.0.0.7/feed.xml" and "private" in result["skipped"][0]["why"]
        assert result["watches"][0]["label"] == "Journal & notes"
        first, _ = invoke(agents, ex, "watches_changed", {})
        assert first["items"] == [] and first["checked"] == 2   # its first look takes it as it is
        web.page("/feed.json", JSON_FEED.replace('"items": [', '"items": [{"id": "j-3", "title": "Margins", '
                                                               '"url": "https://journal.harbourline.example/3"},'),
                 "application/feed+json")
        news_, _ = invoke(agents, ex, "watches_changed", {})
        assert [i["id"] for i in news_["items"]] == ["j-3"]


class TestAddressSafety:
    def test_a_private_address_is_refused_even_with_the_test_variable(self, agents, web):
        ex, provider = make()
        result, status = invoke(agents, ex, "watches_add",
                                {"url": "http://169.254.169.254/latest/meta-data/", "kind": "page"})
        assert status == "error" and result["kind"] == "forbidden" and "link-local" in result["error"]
        result, status = invoke(agents, ex, "watches_add", {"url": "http://192.168.0.10/feed.xml", "kind": "feed"})
        assert status == "error" and result["kind"] == "forbidden" and "private" in result["error"]
        assert records(provider) == {}

    def test_loopback_is_refused_without_the_test_variable(self, agents, monkeypatch):
        monkeypatch.delenv(LOOPBACK_VARIABLE, raising=False)
        stub = WebStub().start()
        try:
            url = stub.page("/lamp", product("129.00"))
            ex, _ = make()
            result, status = invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
            assert status == "error" and result["kind"] == "forbidden"
            assert "loopback" in result["error"] and stub.hits == {}
        finally:
            stub.stop()

    def test_a_page_advertising_a_private_feed_is_refused(self, agents, web):
        page = web.page("/", page_advertising("http://10.0.0.1/feed.xml"))
        ex, provider = make()
        result, status = invoke(agents, ex, "watches_add", {"url": page, "kind": "feed"})
        assert status == "error" and result["kind"] == "forbidden" and "10.0.0.1" in result["error"]
        assert records(provider) == {}

    def test_a_redirect_to_a_private_address_is_a_problem_not_a_change(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, _ = make()
        added, _ = invoke(agents, ex, "watches_add", {"url": url, "kind": "page"})
        web.redirect("/lamp", "http://10.0.0.1/lamp")
        result, _ = invoke(agents, ex, "watches_changed", {})
        assert result["changed"] == []
        (problem,) = result["problems"]
        assert problem["watch_ref"] == added["watch_ref"] and "10.0.0.1" in problem["error"]
        assert problem["kind"] == "forbidden"
