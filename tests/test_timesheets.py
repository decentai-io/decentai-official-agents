"""The Timesheets agent, its program started and spoken to in the standard's messages. Lina's projects at Sidra
Office Supplies, and her calendar for the week of 7 September 2026 as
the calendar agents list it (Dubai time).
"""

import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from tests.harness import Chat, ChatSinks, Records, probe, run


TZ = "+04:00"
WEEK = "2026-W37"


def make():
    provider = Records()
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["timesheets"], name, inputs, chat_level=chat_level))


def ok(agents, ex, name, inputs, chat_level=1):
    result, status = invoke(agents, ex, name, inputs, chat_level)
    assert status == "success", result
    return result


def refused(agents, ex, name, inputs, chat_level=1):
    result, status = invoke(agents, ex, name, inputs, chat_level)
    assert status == "error", result
    return result["error"]


def projects(agents, ex):
    ok(agents, ex, "projects_add", {
        "code": "show", "name": "Harbourline showroom fit-out", "client": "Harbourline",
        "budget_hours": 60})
    ok(agents, ex, "projects_add", {
        "code": "MOVE", "name": "Riverside office move", "billable": False})
    ok(agents, ex, "projects_add", {
        "code": "ADMIN", "name": "Admin and internal", "billable": False})
    for rule in ({"match": "Harbourline", "project_code": "SHOW"},
                 {"match": "harbourline.example", "field": "attendee", "project_code": "SHOW"},
                 {"match": "stand-up", "project_code": "ADMIN"},
                 {"match": "Riverside", "project_code": "MOVE"}):
        ok(agents, ex, "projects_add_rule", rule)


def meeting(event_id, title, day, start, end, **extra):
    return {"event_id": event_id, "summary": title,
            "start": f"2026-09-{day:02d}T{start}:00{TZ}", "end": f"2026-09-{day:02d}T{end}:00{TZ}",
            "all_day": False, "attendees": "", "location": "", "status": "confirmed", **extra}


CALENDAR = [
    meeting("ev1", "Ops stand-up", 7, "09:00", "09:30"),
    meeting("ev2", "Showroom layout", 7, "10:00", "11:50", attendees="dana@harbourline.example"),
    meeting("ev3", "Riverside movers — quotes", 8, "14:00", "15:00"),
    meeting("ev4", "Lunch with Omar", 8, "12:00", "13:00"),
    meeting("ev5", "Harbourline stand-up", 9, "09:00", "09:15"),
    {"event_id": "ev6", "summary": "Public holiday", "start": "2026-09-10",
     "end": "2026-09-11", "all_day": True},
    meeting("ev7", "Board call", 10, "16:00", "17:00", status="cancelled"),
]


def log(agents, ex, code, day, hours, note=""):
    return ok(agents, ex, "entries_log", {
        "project_code": code, "date": f"2026-09-{day:02d}", "hours": hours, "note": note})


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        agent = agents["timesheets"]
        assert probe(agent) == []

    def test_its_levels_are_honest(self, agents):
        """Nothing here leaves the platform. The workbook only hands a file to the chat: a read (1.0.0 said 2). A
        function that deletes a record is at least record.delete's level, 2 (1.0.0 said 1)."""
        levels = {f["id"]: f["level"] for f in agents["timesheets"].manifest["functions"]}
        assert {name for name, level in levels.items() if level >= 2} == {"entries_remove", "projects_remove_rule"}
        assert levels["weeks_workbook"] == 0
        for function in agents["timesheets"].manifest["functions"]:
            if any(c.startswith("record.delete:") for c in function.get("calls") or []):
                assert function["level"] >= 2, function["id"]


class TestProjects:
    def test_a_code_is_one_project(self, agents):
        ex, _ = make()
        projects(agents, ex)
        assert "already" in refused(agents, ex, "projects_add",
                                    {"code": "SHOW", "name": "Another"})
        listed = ok(agents, ex, "projects_list", {})["projects"]
        show = next(p for p in listed if p["code"] == "SHOW")
        assert (show["billable"], show["budget_hours"], show["logged_hours"]) == (True, "60.00", "0.00")
        assert next(p for p in listed if p["code"] == "MOVE")["billable"] is False

    def test_a_rule_needs_an_active_project(self, agents):
        ex, _ = make()
        projects(agents, ex)
        assert "NOPE" in refused(agents, ex, "projects_add_rule",
                                 {"match": "x", "project_code": "NOPE"})
        ok(agents, ex, "projects_update", {"code": "MOVE", "status": "archived"})
        assert "MOVE" in refused(agents, ex, "projects_add_rule",
                                 {"match": "lease", "project_code": "MOVE"})
        codes = [p["code"] for p in ok(agents, ex, "projects_list", {})["projects"]]
        assert codes == ["ADMIN", "SHOW"]
        every = ok(agents, ex, "projects_list", {"include_archived": True})["projects"]
        assert [p["code"] for p in every] == ["ADMIN", "MOVE", "SHOW"]


class TestLogging:
    def test_hours_by_number_or_by_the_clock(self, agents):
        ex, _ = make()
        projects(agents, ex)
        first = log(agents, ex, "SHOW", 7, 2.5, "Layout review")
        assert (first["entry"]["hours"], first["entry"]["week"]) == ("2.50", WEEK)
        second = ok(agents, ex, "entries_log", {
            "project_code": "show", "date": "2026-09-07", "start": "13:00", "end": "14:40"})
        assert second["entry"]["hours"] == "1.75" and second["day_total"] == "4.25"

    def test_a_day_holds_at_most_24_hours(self, agents):
        ex, _ = make()
        projects(agents, ex)
        log(agents, ex, "SHOW", 7, 20)
        assert "25.00" in refused(agents, ex, "entries_log", {
            "project_code": "ADMIN", "date": "2026-09-07", "hours": 5})

    def test_an_unknown_project_names_the_known_ones(self, agents):
        ex, _ = make()
        projects(agents, ex)
        error = refused(agents, ex, "entries_log", {"project_code": "XYZ", "hours": 1})
        assert "XYZ" in error and "SHOW" in error


class TestFromTheCalendar:
    def test_rules_place_meetings_and_the_rest_come_back(self, agents):
        ex, _ = make()
        projects(agents, ex)
        result = ok(agents, ex, "entries_from_events", {"events": CALENDAR})
        assert [(c["event_id"], c["project_code"], c["hours"], c["date"]) for c in result["created"]] == [
            ("ev1", "ADMIN", "0.50", "2026-09-07"),
            ("ev2", "SHOW", "1.75", "2026-09-07"),
            ("ev3", "MOVE", "1.00", "2026-09-08")]
        unassigned = {u["event_id"]: u["reason"] for u in result["unassigned"]}
        assert unassigned["ev4"] == "no rule places it"
        assert "more than one project" in unassigned["ev5"]
        assert [s["event_id"] for s in result["skipped"]] == ["ev6", "ev7"]

    def test_a_meeting_is_never_logged_twice_and_answers_place_the_rest(self, agents):
        ex, _ = make()
        projects(agents, ex)
        ok(agents, ex, "entries_from_events", {"events": CALENDAR})
        again = ok(agents, ex, "entries_from_events", {
            "events": CALENDAR, "assign": [{"event_id": "ev4", "project_code": "admin"},
                                           {"event_id": "ev5", "project_code": "SHOW"}]})
        assert [c["event_id"] for c in again["created"]] == ["ev4", "ev5"]
        assert again["unassigned"] == []
        already = [s["event_id"] for s in again["skipped"] if s["reason"] == "already on the timesheet"]
        assert already == ["ev1", "ev2", "ev3"]
        listed = ok(agents, ex, "entries_list", {"week": WEEK})
        assert listed["total"] == "4.50"


class TestTheWeek:
    def full_week(self, agents, ex):
        projects(agents, ex)
        log(agents, ex, "SHOW", 7, 6)
        log(agents, ex, "ADMIN", 7, 2)
        log(agents, ex, "SHOW", 8, 8)
        log(agents, ex, "MOVE", 9, 3)
        log(agents, ex, "SHOW", 10, 8)

    def test_the_summary_adds_up_and_finds_the_gaps_that_have_happened(self, agents):
        ex, _ = make()
        self.full_week(agents, ex)
        week = ok(agents, ex, "weeks_summary", {"week": WEEK, "as_of": "2026-09-10"})
        assert (week["from"], week["to"], week["status"]) == ("2026-09-07", "2026-09-13", "open")
        assert [p["project_code"] for p in week["projects"]] == ["ADMIN", "MOVE", "SHOW"]
        show = next(p for p in week["projects"] if p["project_code"] == "SHOW")
        assert show["days"] == ["6.00", "8.00", "0.00", "8.00", "0.00", "0.00", "0.00"] and show["total"] == "22.00"
        assert week["day_totals"] == ["8.00", "8.00", "3.00", "8.00", "0.00", "0.00", "0.00"]
        assert (week["total"], week["billable_hours"]) == ("27.00", "22.00")
        assert week["gaps"] == [{"date": "2026-09-09", "day": "Wed", "logged": "3.00", "short": "5.00"}]
        later = ok(agents, ex, "weeks_summary", {"week": WEEK, "as_of": "2026-09-13"})
        assert [g["date"] for g in later["gaps"]] == ["2026-09-09", "2026-09-11"]
        budget = next(p for p in ok(agents, ex, "projects_list", {})["projects"]
                      if p["code"] == "SHOW")
        assert (budget["logged_hours"], budget["budget_left"]) == ("22.00", "38.00")

    def test_a_submitted_week_is_locked_until_reopened(self, agents):
        ex, provider = make()
        self.full_week(agents, ex)
        entry_ref = ok(agents, ex, "entries_list", {"week": WEEK})["entries"][0]["entry_ref"]
        submitted = ok(agents, ex, "weeks_submit", {"week": WEEK, "as_of": "2026-09-11"})
        assert (submitted["status"], submitted["total"], submitted["entries"]) == ("submitted", "27.00", 5)
        assert [g["date"] for g in submitted["gaps"]] == ["2026-09-09", "2026-09-11"]
        assert "submitted" in refused(agents, ex, "entries_log", {
            "project_code": "SHOW", "date": "2026-09-11", "hours": 4})
        assert "submitted" in refused(agents, ex, "entries_update",
                                      {"entry_ref": entry_ref, "hours": 1})
        assert "submitted" in refused(agents, ex, "entries_remove", {"entry_ref": entry_ref}, chat_level=2)
        late = ok(agents, ex, "entries_from_events", {
            "events": [meeting("ev9", "Harbourline follow-up", 11, "10:00", "11:00")]})
        assert late["created"] == [] and "submitted" in late["skipped"][0]["reason"]
        assert "already" in refused(agents, ex, "weeks_submit", {"week": WEEK})
        ok(agents, ex, "weeks_reopen", {"week": WEEK})
        log(agents, ex, "SHOW", 11, 4)
        statuses = {e["status"] for e in ok(agents, ex, "entries_list", {"week": WEEK})["entries"]}
        assert statuses == {"draft"}

    def test_the_workbook_reads_back(self, agents):
        ex, _ = make()
        self.full_week(agents, ex)
        made = ok(agents, ex, "weeks_workbook", {"week": WEEK}, chat_level=0)
        assert (made["filename"], made["total"], made["verified"]) == ("timesheet-2026-W37.xlsx", "27.00", True)
        assert made["cells_checked"] > 40
        raw = ex.files[made["file_ref"]]["content"]
        wb = load_workbook(io.BytesIO(raw))
        assert wb.sheetnames == ["Timesheet", "Entries"]
        grid = list(wb["Timesheet"].iter_rows(values_only=True))
        assert grid[2][:5] == ("Project", "Name", "Billable", "Mon 07", "Tue 08")
        assert grid[-1][0] == "Total" and grid[-1][-1] == 27
        assert len(list(wb["Entries"].iter_rows(values_only=True))) == 1 + 5

    def test_an_empty_week_has_nothing_to_hand_in(self, agents):
        ex, _ = make()
        projects(agents, ex)
        assert "Nothing" in refused(agents, ex, "weeks_submit", {"week": WEEK})
        assert "ISO week" in refused(agents, ex, "weeks_summary", {"week": "37"})


# -- 1.1.0: the review's findings, a timer, and a gaps check for a schedule ------------------------------------------

def zone_today(zone):
    return datetime.now(ZoneInfo(zone)).date()


class TestTheDayIsThePersons:
    def test_today_and_this_week_come_from_the_timezone(self, agents):
        """1.0.0 took today from UTC: before 4 am in Dubai, an entry landed on yesterday."""
        ex, _ = make()
        projects(agents, ex)
        for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago"):
            made = ok(agents, ex, "entries_log", {"project_code": "SHOW", "hours": "1", "timezone": zone})
            assert made["entry"]["date"] == zone_today(zone).isoformat()
        listed = ok(agents, ex, "entries_list", {"timezone": "Pacific/Kiritimati"})
        year, week, _ = zone_today("Pacific/Kiritimati").isocalendar()
        assert listed["entries"] and all(e["week"] == f"{year}-W{week:02d}" for e in listed["entries"])
        summary = ok(agents, ex, "weeks_summary", {"timezone": "Pacific/Kiritimati"})
        assert summary["as_of"] == zone_today("Pacific/Kiritimati").isoformat()
        assert "IANA" in refused(agents, ex, "entries_log", {"project_code": "SHOW", "hours": 1,
                                                              "timezone": "Mars/Olympus"})


class TestExactHours:
    def test_the_budget_is_exact(self, agents):
        """1.0.0 took the budget's remainder in float: 0.3 - 0.1 - 0.2 is 5.55e-17, not 0."""
        ex, _ = make()
        ok(agents, ex, "projects_add", {"code": "TINY", "name": "Tiny", "budget_hours": "0.30"})
        ok(agents, ex, "entries_log", {"project_code": "TINY", "date": "2026-09-07", "hours": 0.1})
        ok(agents, ex, "entries_log", {"project_code": "TINY", "date": "2026-09-07", "hours": "0.2"})
        tiny = ok(agents, ex, "projects_list", {})["projects"][0]
        assert (tiny["budget_hours"], tiny["logged_hours"], tiny["budget_left"]) == ("0.30", "0.30", "0.00")
        ok(agents, ex, "entries_log", {"project_code": "TINY", "date": "2026-09-08", "hours": 1.25})
        over = ok(agents, ex, "projects_list", {})["projects"][0]
        assert (over["budget_left"], over["over_budget"]) == ("-1.25", "1.25")
        _, status = invoke(agents, ex, "entries_log", {"project_code": "TINY", "date": "2026-09-08", "hours": "1,5"})
        assert status == "error"                                    # a decimal comma is refused, never guessed


class TestTheWorkbookIsCheckedByItsCells:
    def test_a_formula_from_a_meeting_title_stays_text_and_arabic_survives(self, agents):
        ex, _ = make()
        projects(agents, ex)
        title = '=HYPERLINK("http://evil.example","Harbourline")'
        ok(agents, ex, "entries_from_events", {"events": [
            meeting("evx", title, 7, "09:00", "10:00"),
            meeting("eva", "Harbourline — مراجعة التصميم", 8, "09:00", "10:30")]})
        made = ok(agents, ex, "weeks_workbook", {"week": WEEK}, chat_level=0)
        assert made["verified"] is True
        wb = load_workbook(io.BytesIO(ex.files[made["file_ref"]]["content"]))
        sheet = wb["Entries"]
        notes = {sheet.cell(row=n, column=4).value: sheet.cell(row=n, column=4).data_type
                 for n in range(2, sheet.max_row + 1)}
        assert notes[title] == "s"
        assert "Harbourline — مراجعة التصميم" in notes

    def test_a_cell_that_does_not_hold_what_was_written_is_caught(self):
        from datetime import date, timedelta
        from decimal import Decimal
        from agents.timesheets.tools.weeks_tool import _read_back, _workbook
        days = [date(2026, 9, 7) + timedelta(days=n) for n in range(7)]
        summary = {"status": "open", "projects": [{"project_code": "SHOW", "name": "Showroom", "billable": True,
                                                    "days": ["1.50"] + ["0.00"] * 6, "total": "1.50"}],
                   "day_totals": ["1.50"] + ["0.00"] * 6, "total": "1.50"}
        rows = [{"date": "2026-09-07", "project_code": "SHOW", "hours": "1.50", "note": "x", "source": "manual",
                 "status": "draft"}]
        raw, written = _workbook(WEEK, days, summary, rows)
        assert _read_back(raw, written) == []
        tampered = dict(written)
        tampered[("Timesheet", 4, 4)] = Decimal("1.75")
        assert [(sheet, cell) for sheet, cell, _, _ in _read_back(raw, tampered)] == [("Timesheet", "D4")]


class TestTimer:
    def test_start_stop_logs_on_the_project_once(self, agents):
        ex, provider = make()
        projects(agents, ex)
        started = ok(agents, ex, "timer_start", {"project_code": "show", "note": "Layout drawings",
                                                 "timezone": "Asia/Dubai"})
        assert "already running" in refused(agents, ex, "timer_start", {"project_code": "ADMIN"})
        status = ok(agents, ex, "timer_status", {}, chat_level=0)
        assert status["running"] is True and status["project_code"] == "SHOW"
        stopped = ok(agents, ex, "timer_stop", {})
        assert stopped["entry"]["hours"] == "0.25" and stopped["entry"]["source"] == "timer"
        assert stopped["entry"]["date"] == zone_today("Asia/Dubai").isoformat()
        assert provider.data["timer"][started["timer_ref"]]["keys"]["entry_ref"] == stopped["entry"]["entry_ref"]
        assert "No timer" in refused(agents, ex, "timer_stop", {})
        assert len(provider.data["entry"]) == 1

    def test_a_stop_retried_after_a_lost_answer_finishes_the_same_timer(self, agents):
        ex, provider = make()
        projects(agents, ex)
        started = ok(agents, ex, "timer_start", {"project_code": "SHOW"})
        timer = provider.data["timer"][started["timer_ref"]]["keys"]
        # The first stop marked the timer stopped, then the process ended before the entry was made.
        timer.update({"status": "stopped", "stopped_at": timer["started_at"]})
        finished = ok(agents, ex, "timer_stop", {})
        assert finished["timer_ref"] == started["timer_ref"] and len(provider.data["entry"]) == 1
        # And once more, after the entry was made but before the timer named it.
        timer.pop("entry_ref")
        again = ok(agents, ex, "timer_stop", {})
        assert again["already"] is True and len(provider.data["entry"]) == 1

    def test_a_forgotten_timer_is_logged_only_when_the_person_says_so(self, agents):
        from datetime import timedelta, timezone as utc_zone
        answers = []
        provider = Records()
        ex = Chat(provider, sinks=ChatSinks(ask=lambda text, choices, expects: answers.pop(0)))
        projects(agents, ex)
        started = ok(agents, ex, "timer_start", {"project_code": "SHOW", "timezone": "Asia/Dubai"})
        eleven_hours_ago = datetime.now(utc_zone.utc) - timedelta(hours=11)
        provider.data["timer"][started["timer_ref"]]["keys"]["started_at"] = eleven_hours_ago.isoformat(
            timespec="seconds")
        answers.append(None)                                        # nobody answered: nothing is logged
        assert "Not logged" in refused(agents, ex, "timer_stop", {})
        assert provider.data.get("entry", {}) == {}
        assert provider.data["timer"][started["timer_ref"]]["keys"]["status"] == "running"
        answers.append("Log 11.00 hours")
        logged = ok(agents, ex, "timer_stop", {})
        assert logged["entry"]["hours"] == "11.00"
        assert logged["entry"]["date"] == eleven_hours_ago.astimezone(ZoneInfo("Asia/Dubai")).date().isoformat()

    def test_cancel_logs_nothing(self, agents):
        ex, provider = make()
        projects(agents, ex)
        ok(agents, ex, "timer_start", {"project_code": "SHOW"})
        assert ok(agents, ex, "timer_cancel", {})["logged"] is False
        assert ok(agents, ex, "timer_status", {}, chat_level=0) == {"running": False}
        assert provider.data.get("entry", {}) == {}


class TestGapsCheck:
    def test_it_is_a_schedulable_read_with_no_model(self, agents):
        function = agents["timesheets"].function("weeks_gaps")
        assert function["schedulable"] is True and function["level"] <= 1
        assert "model" not in function["calls"]

    def test_gaps_follow_the_working_week(self, agents):
        ex, _ = make()
        projects(agents, ex)
        for day in (7, 8, 9, 10):                                   # Monday 7 to Thursday 10 September
            log(agents, ex, "SHOW", day, 8)
        western = ok(agents, ex, "weeks_summary", {"week": WEEK, "as_of": "2026-09-13"}, chat_level=0)
        assert [g["date"] for g in western["gaps"]] == ["2026-09-11"]                 # Friday
        gulf = ok(agents, ex, "weeks_gaps", {"week": WEEK, "work_week": "sun_thu", "include_today": True,
                                             "timezone": "Asia/Dubai"}, chat_level=0)
        assert [(g["date"], g["day"], g["short"]) for g in gulf["gaps"]] == [("2026-09-13", "Sun", "8.00")]
        assert gulf["short_total"] == "8.00" and "Sun 2026-09-13" in gulf["said"]

    def test_the_check_says_what_is_short_up_to_yesterday(self, agents):
        ex, _ = make()
        projects(agents, ex)
        today_there = zone_today("Asia/Dubai")
        year, week, _ = today_there.isocalendar()
        found = ok(agents, ex, "weeks_gaps", {"timezone": "Asia/Dubai"}, chat_level=0)
        assert found["week"] == f"{year}-W{week:02d}"
        assert found["as_of"] == today_there.fromordinal(today_there.toordinal() - 1).isoformat()
        assert all(g["date"] < today_there.isoformat() for g in found["gaps"])
        assert found["said"].startswith("No gaps") == (found["gaps"] == [])
        with_today = ok(agents, ex, "weeks_gaps", {"timezone": "Asia/Dubai", "include_today": True}, chat_level=0)
        assert with_today["as_of"] == today_there.isoformat()
