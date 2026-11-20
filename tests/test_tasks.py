"""The Commitments & Tasks agent, its program started and spoken to in the standard's messages. The notes are from
a fictional Sidra Office Supplies operations meeting on Monday 7 September
2026."""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.harness import Chat, ChatSinks, Records, probe, run


NOTES = """Ops meeting, 7 September 2026. Present: Sami, Dana, Omar.

Harbourline office: Dana will send the revised quotation to Harbourline by Thursday.
Omar said he will chase Northlight about the 15-day delivery and confirm by 11 September 2026.
We should probably review the chair warranty terms before the next order.
Sami to book the site visit once Dana has Harbourline's answer.
Someone needs to update the supplier list.
"""


def make(llm=None):
    provider = Records()
    return Chat(provider, sinks=ChatSinks(llm=llm)), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["tasks"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        agent = agents["tasks"]
        assert probe(agent) == []

    def test_overdue_is_schedulable_and_a_read(self, agents):
        function = agents["tasks"].function("tasks_overdue")
        assert function["schedulable"] is True and function["level"] == 0


class TestTasks:
    def test_create_update_and_list(self, agents):
        ex, provider = make()
        made, status = invoke(agents, ex, "tasks_create", {
            "title": "Send revised quotation", "owner": "Dana", "due": "2026-09-10",
            "source": "note_ops_0907"})
        assert status == "success", made
        assert made["unassigned"] is False and made["status"] == "open"

        bad, status = invoke(agents, ex, "tasks_create",
                             {"title": "x", "due": "Thursday"})
        assert status == "error" and "YYYY-MM-DD" in bad["error"]

        # Done needs evidence; blocked needs a blocker.
        refused, status = invoke(agents, ex, "tasks_update",
                                 {"task_ref": made["task_ref"], "status": "done"})
        assert status == "error" and "evidence" in refused["error"]
        refused, status = invoke(agents, ex, "tasks_update",
                                 {"task_ref": made["task_ref"], "status": "blocked"})
        assert status == "error" and "blocker" in refused["error"]

        done, status = invoke(agents, ex, "tasks_update", {
            "task_ref": made["task_ref"], "status": "done",
            "evidence": "mail draft_ref rec_12 sent as m0042", "done_on": "2026-09-09"})
        assert status == "success", done
        assert done["status"] == "done" and done["evidence"].endswith("m0042")
        record = provider.data["task"][made["task_ref"]]
        assert record["keys"]["done_on"] == "2026-09-09"

        invoke(agents, ex, "tasks_create", {"title": "Update supplier list"})
        listed, _ = invoke(agents, ex, "tasks_list", {"status": "open"})
        assert [t["title"] for t in listed["tasks"]] == ["Update supplier list"]
        assert listed["tasks"][0]["owner"] == ""

    def test_overdue_is_arithmetic_on_dates(self, agents):
        ex, _ = make()
        create = lambda **fields: invoke(agents, ex, "tasks_create", fields)[0]
        late = create(title="Chase Northlight", owner="Omar", due="2026-09-11")
        soon = create(title="Book site visit", owner="Sami", due="2026-09-16")
        far = create(title="Review warranty", owner="Dana", due="2026-10-30")
        nobody = create(title="Update supplier list", due="2026-09-12")
        undated = create(title="Plan next order", owner="Dana")
        stuck = create(title="Confirm delivery", owner="Omar", due="2026-09-09")
        invoke(agents, ex, "tasks_update", {
            "task_ref": stuck["task_ref"], "status": "blocked",
            "blocker": "Northlight has not answered"})
        finished = create(title="Send quotation", owner="Dana", due="2026-09-10")
        invoke(agents, ex, "tasks_update", {
            "task_ref": finished["task_ref"], "status": "done", "evidence": "sent m0042"})

        result, status = invoke(agents, ex, "tasks_overdue",
                                {"as_of": "2026-09-14", "within_days": 5})
        assert status == "success", result
        assert result["as_of"] == "2026-09-14"
        assert [(t["title"], t["days_late"]) for t in result["overdue"]] == [
            ("Chase Northlight", 3), ("Update supplier list", 2)]
        assert [(t["title"], t["days_left"]) for t in result["due_soon"]] == [
            ("Book site visit", 2)]
        assert [t["title"] for t in result["blocked"]] == ["Confirm delivery"]
        assert [t["title"] for t in result["unassigned"]] == ["Update supplier list"]
        assert [t["title"] for t in result["undated"]] == ["Plan next order"]
        assert far["task_ref"] not in json.dumps(result)
        assert finished["task_ref"] not in json.dumps(result)

        # Once everything late is handled, nothing is overdue.
        for ref in (late["task_ref"], nobody["task_ref"]):
            invoke(agents, ex, "tasks_update", {
                "task_ref": ref, "status": "done", "evidence": "handled"})
        result, _ = invoke(agents, ex, "tasks_overdue", {"as_of": "2026-09-14"})
        assert result["overdue"] == []

    def test_overdue_by_owner_and_a_bad_date(self, agents):
        ex, _ = make()
        invoke(agents, ex, "tasks_create", {"title": "A", "owner": "Omar", "due": "2026-09-01"})
        invoke(agents, ex, "tasks_create", {"title": "B", "owner": "Dana", "due": "2026-09-01"})
        result, _ = invoke(agents, ex, "tasks_overdue", {"as_of": "2026-09-14", "owner": "Omar"})
        assert [t["title"] for t in result["overdue"]] == ["A"]
        result, status = invoke(agents, ex, "tasks_overdue", {"as_of": "Friday"})
        assert status == "error"


class TestExtraction:
    @staticmethod
    async def model(messages, max_tokens, pictures):
        return json.dumps({"items": [
            {"title": "Send revised quotation to Harbourline", "kind": "commitment",
             "owner": "Dana", "due": "Thursday",
             "quote": "Dana will send the revised quotation to Harbourline by Thursday."},
            {"title": "Chase Northlight about delivery", "kind": "commitment",
             "owner": "Omar", "due": "11 September 2026",
             "quote": "confirm by 11 September 2026"},
            {"title": "Review chair warranty terms", "kind": "suggestion",
             "owner": "", "due": "before the next order",
             "quote": "We should probably review the chair warranty terms before the next order."},
            {"title": "Book the site visit", "kind": "commitment",
             "owner": "Sami", "due": "",
             "quote": "Sami to book the site visit once Dana has Harbourline's answer."},
            {"title": "Update the supplier list", "kind": "suggestion",
             "owner": "Priya", "due": "",                       # invented: not in the notes
             "quote": "Someone needs to update the supplier list."},
            {"title": "Order more chairs", "kind": "commitment",
             "owner": "Omar", "due": "",
             "quote": "Omar will order forty more chairs next week."},   # not in the notes
        ]})

    def test_proposals_quote_verify_and_never_invent(self, agents):
        ex, provider = make(llm=self.model)
        result, status = invoke(agents, ex, "extract_propose", {
            "notes": NOTES, "source": "note_ops_0907", "meeting_date": "2026-09-07",
            "people": ["Sami", "Dana", "Omar"]})
        assert status == "success", result
        by_title = {p["title"]: p for p in result["proposals"]}
        assert result["commitments"] == 4 and result["suggestions"] == 2

        quotation = by_title["Send revised quotation to Harbourline"]
        assert quotation["kind"] == "commitment" and quotation["owner"] == "Dana"
        assert quotation["due"] == "2026-09-10"           # Thursday after Monday the 7th
        assert quotation["due_text"] == "Thursday" and quotation["verified"] is True

        assert by_title["Chase Northlight about delivery"]["due"] == "2026-09-11"

        warranty = by_title["Review chair warranty terms"]
        assert warranty["kind"] == "suggestion" and warranty["owner"] == ""
        assert warranty["due"] == "" and warranty["due_text"] == "before the next order"

        supplier = by_title["Update the supplier list"]
        assert supplier["owner"] == ""                     # Priya was never in the notes
        assert supplier["verified"] is True

        invented = by_title["Order more chairs"]
        assert invented["verified"] is False               # the quote is not in the notes
        assert result["unassigned"] == 2
        assert all(r["keys"]["status"] == "proposed"
                   for r in provider.data["extraction"].values())
        assert provider.data.get("task", {}) == {}  # nothing is a task yet

    def test_confirming_makes_tasks_and_rejecting_makes_nothing(self, agents):
        ex, provider = make(llm=self.model)
        proposed, _ = invoke(agents, ex, "extract_propose", {
            "notes": NOTES, "meeting_date": "2026-09-07"})
        by_title = {p["title"]: p for p in proposed["proposals"]}

        confirmed, status = invoke(agents, ex, "extract_confirm", {"items": [
            {"extraction_ref": by_title["Send revised quotation to Harbourline"]["extraction_ref"]},
            {"extraction_ref": by_title["Update the supplier list"]["extraction_ref"],
             "owner": "Omar", "due": "2026-09-18"},           # the user supplies what the notes lacked
            {"extraction_ref": by_title["Book the site visit"]["extraction_ref"]},
            {"extraction_ref": by_title["Review chair warranty terms"]["extraction_ref"],
             "due": "soon"},
            {"extraction_ref": "rec_nope"},
        ]})
        assert status == "success", confirmed
        titles = {t["title"]: t for t in confirmed["tasks"]}
        assert titles["Send revised quotation to Harbourline"]["owner"] == "Dana"
        assert titles["Send revised quotation to Harbourline"]["due"] == "2026-09-10"
        assert titles["Update the supplier list"]["owner"] == "Omar"
        assert titles["Update the supplier list"]["due"] == "2026-09-18"
        assert confirmed["unassigned"] == []
        reasons = {s["extraction_ref"]: s["reason"] for s in confirmed["skipped"]}
        assert reasons["rec_nope"] == "no such proposal"
        assert "YYYY-MM-DD" in reasons[by_title["Review chair warranty terms"]["extraction_ref"]]

        # Confirmed proposals point at their tasks and cannot be confirmed twice.
        record = provider.data["extraction"][
            by_title["Book the site visit"]["extraction_ref"]]
        assert record["keys"]["status"] == "confirmed"
        assert record["keys"]["task_ref"] == titles["Book the site visit"]["task_ref"]
        # Confirming again (a retry after a lost answer) hands back the same task, and makes no second one.
        again, _ = invoke(agents, ex, "extract_confirm", {"items": [
            {"extraction_ref": by_title["Book the site visit"]["extraction_ref"]}]})
        assert again["skipped"] == []
        assert [(t["task_ref"], t["already"]) for t in again["tasks"]] == [
            (titles["Book the site visit"]["task_ref"], True)]

        rejected, status = invoke(agents, ex, "extract_reject", {
            "extraction_ref": by_title["Order more chairs"]["extraction_ref"]})
        assert (status, rejected) == ("success", {"rejected": True})
        listed, _ = invoke(agents, ex, "tasks_list", {})
        assert listed["total"] == 3
        assert all("From notes:" in r["values"]["note"]
                   for r in provider.data["task"].values())

    def test_extraction_needs_the_chats_model(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "extract_propose",
                                {"notes": NOTES, "meeting_date": "2026-09-07"})
        assert status == "error" and "model" in result["error"].lower()


# -- 1.1.0: the review's findings, and recurring tasks and priority ----------------------------------------

NOTES_1_1 = """Ops meeting, Monday 7 September 2026. Present: Alice, Dana Haddad, Omar.

Dana will send the revised quotation by Thursday.
Omar to chase Northlight next Friday.
Alice will renew the domain by 3 October.
"""


def model_of(items):
    async def model(messages, max_tokens, pictures):
        return json.dumps({"items": items})
    return model


class TestWithinDaysAndToday:
    def test_within_days_zero_means_today_only(self, agents):
        """1.0.0 read `within_days or 5`, so 0 became 5."""
        ex, _ = make()
        invoke(agents, ex, "tasks_create", {"title": "Today", "owner": "Dana", "due": "2026-09-14"})
        invoke(agents, ex, "tasks_create", {"title": "Tomorrow", "owner": "Dana", "due": "2026-09-15"})
        result, status = invoke(agents, ex, "tasks_overdue", {"as_of": "2026-09-14", "within_days": 0})
        assert status == "success", result
        assert [(t["title"], t["days_left"]) for t in result["due_soon"]] == [("Today", 0)]
        assert result["within_days"] == 0

    def test_today_is_the_persons_day_not_utcs(self, agents):
        """Kiritimati is UTC+14 and Pago Pago UTC-11: for most of every day their dates differ."""
        ex, _ = make()
        for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "Asia/Dubai"):
            result, status = invoke(agents, ex, "tasks_overdue", {"timezone": zone})
            assert status == "success", result
            assert result["timezone"] == zone
            assert result["as_of"] in {datetime.now(ZoneInfo(zone)).date().isoformat()}
        bad, status = invoke(agents, ex, "tasks_overdue", {"timezone": "Gulf/Nowhere"})
        assert status == "error" and "IANA" in bad["error"]
        upcoming, status = invoke(agents, ex, "deadlines_upcoming", {"timezone": "Pacific/Kiritimati"})
        assert status == "success" and upcoming["as_of"] == datetime.now(
            ZoneInfo("Pacific/Kiritimati")).date().isoformat()

    def test_done_on_defaults_to_today_in_the_zone(self, agents):
        ex, provider = make()
        made, _ = invoke(agents, ex, "tasks_create", {"title": "Send the quotation", "owner": "Dana"})
        done, status = invoke(agents, ex, "tasks_update", {
            "task_ref": made["task_ref"], "status": "done", "evidence": "sent m0042",
            "timezone": "Pacific/Kiritimati"})
        assert status == "success", done
        assert provider.data["task"][made["task_ref"]]["keys"]["done_on"] == datetime.now(
            ZoneInfo("Pacific/Kiritimati")).date().isoformat()


class TestProposals:
    ITEMS = [
        {"title": "Send the revised quotation", "kind": "commitment", "owner": "Dana", "due": "Thursday",
         "quote": "Dana will send the revised quotation by Thursday."},
        {"title": "Chase Northlight", "kind": "commitment", "owner": "Omar", "due": "next Friday",
         "quote": "Omar to chase Northlight next Friday."},
        {"title": "Renew the domain", "kind": "commitment", "owner": "Al", "due": "3 October",
         "quote": "Alice will renew the domain by 3 October."},
        {"title": "Order chairs", "kind": "commitment", "owner": "Omar", "due": "",
         "quote": "Omar will order forty chairs."},                      # not in the notes
    ]

    def propose(self, agents, ex, people=None):
        result, status = invoke(agents, ex, "extract_propose", {
            "notes": NOTES_1_1, "meeting_date": "2026-09-07", **({"people": people} if people else {})})
        assert status == "success", result
        return result, {p["title"]: p for p in result["proposals"]}

    def test_owners_match_whole_names_only(self, agents):
        """1.0.0 matched a substring, so "Al" passed as an owner because the notes say "Alice"."""
        items = self.ITEMS + [{"title": "Price the chairs", "kind": "commitment", "owner": "Dana",
                               "due": "", "quote": "Dana will send the revised quotation by Thursday."}]
        ex, _ = make(llm=model_of(items))
        _, by_title = self.propose(agents, ex, people=["Dana Haddad"])
        assert by_title["Renew the domain"]["owner"] == ""            # "Al" is not "Alice"
        assert by_title["Price the chairs"]["owner"] == "Dana"         # a whole name of "Dana Haddad"

    def test_dates_read_rather_than_stated_are_said(self, agents):
        """1.0.0 turned "Thursday" and "next Friday" into dates and said nothing."""
        ex, _ = make(llm=model_of(self.ITEMS))
        result, by_title = self.propose(agents, ex)
        thursday = by_title["Send the revised quotation"]
        assert thursday["due"] == "2026-09-10" and "relative" in thursday["due_reading"]
        assert "2026-09-10" in thursday["due_reading"] and "2026-09-07" in thursday["due_reading"]
        friday = by_title["Chase Northlight"]
        assert friday["due"] == "" and "not resolved" in friday["due_reading"]
        october = by_title["Renew the domain"]
        assert october["due"] == "2026-10-03" and "no year" in october["due_reading"]
        assert len(result["date_readings"]) == 3
        assert all(not p.get("due_reading") for p in result["proposals"] if p["title"] == "Order chairs")

    def test_verified_is_kept_and_confirm_refuses_an_unverified_proposal(self, agents):
        """1.0.0 dropped `verified` before confirm, so an invented quote became a task."""
        ex, provider = make(llm=model_of(self.ITEMS))
        result, by_title = self.propose(agents, ex)
        invented = by_title["Order chairs"]
        assert invented["verified"] is False and result["unverified"] == 1
        assert provider.data["extraction"][invented["extraction_ref"]]["keys"]["verified"] == "no"
        refused, status = invoke(agents, ex, "extract_confirm", {"items": [
            {"extraction_ref": invented["extraction_ref"]}]})
        assert status == "success" and refused["tasks"] == []
        assert refused["skipped"][0]["reason"].startswith("unverified")
        assert provider.data.get("task", {}) == {}
        accepted, _ = invoke(agents, ex, "extract_confirm", {"items": [
            {"extraction_ref": invented["extraction_ref"], "accept_unverified": True}]})
        assert [t["title"] for t in accepted["tasks"]] == ["Order chairs"]

    def test_confirm_is_safe_against_a_retry(self, agents):
        """A confirm whose answer was lost, retried: the proposal was marked before the task was made, and the task
        names its proposal, so the retry finds it. Also a crash between the mark and the task: one task, not none."""
        ex, provider = make(llm=model_of(self.ITEMS))
        _, by_title = self.propose(agents, ex)
        ref = by_title["Send the revised quotation"]["extraction_ref"]
        first, _ = invoke(agents, ex, "extract_confirm", {"items": [{"extraction_ref": ref}]})
        task_ref = first["tasks"][0]["task_ref"]
        assert provider.data["task"][task_ref]["keys"]["extraction_ref"] == ref
        assert first["tasks"][0]["due_reading"].startswith("'Thursday' is relative")

        # The task was made but the proposal was left "confirming" (the process ended before the last write).
        provider.data["extraction"][ref]["keys"].update({"status": "confirming"})
        provider.data["extraction"][ref]["keys"].pop("task_ref", None)
        retried, _ = invoke(agents, ex, "extract_confirm", {"items": [{"extraction_ref": ref}]})
        assert [(t["task_ref"], t["already"]) for t in retried["tasks"]] == [(task_ref, True)]
        assert provider.data["extraction"][ref]["keys"]["status"] == "confirmed"
        assert len(provider.data["task"]) == 1

        # Marked, but the task never made: the retry makes exactly one.
        other = by_title["Renew the domain"]["extraction_ref"]
        provider.data["extraction"][other]["keys"]["status"] = "confirming"
        made, _ = invoke(agents, ex, "extract_confirm", {"items": [{"extraction_ref": other}]})
        assert len(made["tasks"]) == 1 and not made["tasks"][0].get("already")
        assert len(provider.data["task"]) == 2
        rejected, status = invoke(agents, ex, "extract_reject", {"extraction_ref": other})
        assert status == "error"

    def test_two_overlapping_confirms_make_one_task(self, agents):
        ex, provider = make(llm=model_of(self.ITEMS))
        _, by_title = self.propose(agents, ex)
        ref = by_title["Send the revised quotation"]["extraction_ref"]
        agent = agents["tasks"]

        async def both():
            return await asyncio.gather(*(ex.invoke(agent, "extract_confirm", {"items": [{"extraction_ref": ref}]})
                                          for _ in range(2)))
        answers = run(both())
        assert all(status == "success" for _, status in answers)
        assert len({t["task_ref"] for output, _ in answers for t in output["tasks"]}) == 1
        assert len(provider.data["task"]) == 1


class TestRecurringAndPriority:
    def test_a_monthly_task_keeps_its_day_and_makes_the_next_once(self, agents):
        ex, provider = make()
        made, status = invoke(agents, ex, "tasks_create", {
            "title": "Pay the office internet bill", "owner": "Priya", "due": "2027-01-31",
            "repeat": "monthly", "priority": "high"})
        assert status == "success" and made["repeat"] == "monthly" and made["priority"] == "high"
        done, status = invoke(agents, ex, "tasks_update", {
            "task_ref": made["task_ref"], "status": "done", "evidence": "paid, receipt 0131",
            "done_on": "2027-01-30"})
        assert status == "success", done
        assert done["next_due"] == "2027-02-28"
        february = done["next_ref"]
        nxt = provider.data["task"][february]["keys"]
        assert (nxt["title"], nxt["owner"], nxt["priority"], nxt["repeat"], nxt["previous_ref"]) == (
            "Pay the office internet bill", "Priya", "high", "monthly", made["task_ref"])
        # Done again (a retry): no second next task.
        again, _ = invoke(agents, ex, "tasks_update", {
            "task_ref": made["task_ref"], "status": "done", "evidence": "paid, receipt 0131"})
        assert len(provider.data["task"]) == 2 and again.get("next_ref", february) == february
        march, _ = invoke(agents, ex, "tasks_update", {
            "task_ref": february, "status": "done", "evidence": "paid", "done_on": "2027-02-27"})
        assert march["next_due"] == "2027-03-31"                       # back to the 31st

    def test_a_late_weekly_task_is_next_due_after_the_day_it_was_done(self, agents):
        ex, _ = make()
        made, _ = invoke(agents, ex, "tasks_create", {
            "title": "Back up the accounts", "owner": "Priya", "due": "2026-09-04", "repeat": "weekly"})
        done, _ = invoke(agents, ex, "tasks_update", {
            "task_ref": made["task_ref"], "status": "done", "evidence": "backup log 0920", "done_on": "2026-09-20"})
        assert done["next_due"] == "2026-09-25"                        # 09-11 and 09-18 were missed, not made
        daily, _ = invoke(agents, ex, "tasks_create", {
            "title": "Check the till", "due": "2026-09-04", "repeat": "daily"})
        early, _ = invoke(agents, ex, "tasks_update", {
            "task_ref": daily["task_ref"], "status": "done", "evidence": "counted", "done_on": "2026-09-03"})
        assert early["next_due"] == "2026-09-05"

    def test_a_repeating_task_needs_a_due_date_and_cancelling_makes_none(self, agents):
        ex, provider = make()
        refused, status = invoke(agents, ex, "tasks_create", {"title": "Water the plants", "repeat": "weekly"})
        assert status == "error" and "due date" in refused["error"]
        made, _ = invoke(agents, ex, "tasks_create", {
            "title": "Water the plants", "due": "2026-09-04", "repeat": "weekly"})
        invoke(agents, ex, "tasks_update", {"task_ref": made["task_ref"], "status": "cancelled"})
        assert len(provider.data["task"]) == 1

    def test_priority_orders_a_day_and_filters(self, agents):
        ex, _ = make()
        for title, priority in (("Low one", "low"), ("High one", "high"), ("Normal one", None)):
            invoke(agents, ex, "tasks_create", {"title": title, "due": "2026-09-10",
                                                **({"priority": priority} if priority else {})})
        listed, _ = invoke(agents, ex, "tasks_list", {})
        assert [t["title"] for t in listed["tasks"]] == ["High one", "Normal one", "Low one"]
        high, _ = invoke(agents, ex, "tasks_list", {"priority": "high"})
        assert [t["title"] for t in high["tasks"]] == ["High one"]
        overdue, _ = invoke(agents, ex, "tasks_overdue", {"as_of": "2026-09-14"})
        assert [t["priority"] for t in overdue["overdue"]] == ["high", "normal", "low"]


class TestMoney:
    def agreement(self, agents, ex, **fields):
        inputs = {"name": "Office lease", "counterparty": "Pier 9 Properties", "kind": "lease",
                  "renewal_date": "2027-03-01", "notice_days": 90, "notice_basis": "calendar",
                  "auto_renews": "yes", **fields}
        return invoke(agents, ex, "agreements_create", inputs)

    def test_money_is_exact_text_and_never_a_float(self, agents):
        ex, provider = make()
        made, status = self.agreement(agents, ex, value="48000.10", currency="aed")
        assert status == "success", made
        assert (made["value"], made["currency"]) == ("48000.10", "AED")
        kept = provider.data["agreement"][made["agreement_ref"]]
        assert kept["values"]["value"] == "48000.10" and "value" not in kept["keys"]
        got, _ = invoke(agents, ex, "agreements_get", {"agreement_ref": made["agreement_ref"]})
        assert got["value"] == "48000.10"
        changed, status = invoke(agents, ex, "agreements_update", {
            "agreement_ref": made["agreement_ref"], "value": "0.30"})
        assert status == "success", changed
        assert provider.data["agreement"][made["agreement_ref"]]["values"]["value"] == "0.30"

    def test_an_amount_with_a_comma_is_refused_not_guessed(self, agents):
        ex, provider = make()
        refused, status = self.agreement(agents, ex, value="4,800", currency="USD")
        assert status == "error"
        assert provider.data.get("agreement", {}) == {}

    def test_no_currency_is_invented(self, agents):
        """1.0.0 wrote "USD" when none was given."""
        ex, provider = make()
        made, status = self.agreement(agents, ex, value="4800")
        assert status == "success", made
        assert "currency" not in made
        assert "currency" not in provider.data["agreement"][made["agreement_ref"]]["keys"]
        assert any("currency" in ask for ask in made["asks"])
        plain, _ = self.agreement(agents, ex, name="No value")
        assert "currency" not in plain and not any("currency" in ask for ask in plain["asks"])

    def test_an_amount_kept_by_1_0_0_as_a_number_reads_as_exact_text(self, agents):
        ex, provider = make()
        ref = provider.create_data("agreement", {
            "name": "Old", "counterparty": "Pier 9", "kind": "other", "notice_basis": "unknown",
            "auto_renews": "unknown", "status": "active", "decision": "undecided", "value": 1440.0,
            "currency": "USD"})
        got, status = invoke(agents, ex, "agreements_get", {"agreement_ref": ref})
        assert status == "success", got
        assert got["value"] == "1440"


class TestWorkingWeek:
    def test_business_days_follow_a_sunday_to_thursday_week(self, agents):
        """Renewing on Saturday 5 December 2026 with one business day's notice: in the Gulf's week the last working
        day before is Thursday the 3rd; Monday to Friday it is Friday the 4th."""
        ex, _ = make()
        common = {"kind": "maintenance", "renewal_date": "2026-12-05", "notice_days": 1,
                  "notice_basis": "business", "auto_renews": "yes"}
        gulf, status = invoke(agents, ex, "agreements_create", {
            "name": "Dubai office cleaning", "counterparty": "Gulf Clean LLC", "work_week": "sun_thu", **common})
        assert status == "success", gulf
        assert (gulf["notice_deadline"], gulf["work_week"]) == ("2026-12-03", "sun_thu")
        western, _ = invoke(agents, ex, "agreements_create", {
            "name": "London office cleaning", "counterparty": "Clean Ltd", **common})
        assert (western["notice_deadline"], western["work_week"]) == ("2026-12-04", "mon_fri")
        listed, _ = invoke(agents, ex, "deadlines_upcoming", {"as_of": "2026-12-02", "within_days": 10})
        left = {(d["agreement"], d["kind"]): d["business_days_left"] for d in listed["due"]}
        assert left[("Dubai office cleaning", "renewal")] == 1       # Thursday; Friday and Saturday are off
        assert left[("London office cleaning", "renewal")] == 2      # Thursday and Friday


def test_a_yearly_task_keeps_its_month_and_day():
    """Seen live: a licence renewal could repeat only monthly. Yearly keeps the date; 29 February falls to the 28th
    in a year without it, and comes back in a leap year."""
    import importlib.util
    from datetime import date
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "tasks_repeat", Path(__file__).resolve().parent.parent / "agents" / "tasks" / "tools" / "repeat.py")
    repeat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repeat)
    assert "yearly" in repeat.RULES
    assert repeat.next_due(date(2026, 11, 30), "yearly") == date(2027, 11, 30)
    assert repeat.next_due(date(2028, 2, 29), "yearly", 29) == date(2029, 2, 28)
    assert repeat.next_due(date(2031, 2, 28), "yearly", 29) == date(2032, 2, 29)
    assert repeat.first_after(date(2024, 3, 1), "yearly", 1, date(2026, 6, 1)) == date(2027, 3, 1)
