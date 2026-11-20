"""Google Calendar 2.0.0 (with Google Tasks) as its own program, against a loopback Google as strict as the real one
where its rules are known (tests/gcal_stub.py). The week is 7-11 September 2026, Dubai time; Dana's calendar is shared,
Omar's is not. Each class pins the bugs of 1.x it fixes.
"""

import json
from datetime import timedelta

import pytest
import yaml

from tests.gcal_stub import GoogleStub
from tests.harness import ROOT, Chat, Records, probe, run

DANA = "dana@harbourline.example"
OMAR = "omar@harbourline.example"
ME = "demo@sidra.example"
WORK = "ops@sidra-works.example"
TZ = "+04:00"
WEEK = {"from_date": "2026-09-07", "to_date": "2026-09-11", "not_before": f"2026-09-06T08:00:00{TZ}"}


@pytest.fixture
def google():
    stub = GoogleStub().start()
    me = stub.me = stub.account(ME, "at-1")
    me.standup = me.add_event("Ops stand-up", f"2026-09-07T09:00:00{TZ}", f"2026-09-07T09:30:00{TZ}")
    me.supplier = me.add_event("Supplier call", f"2026-09-08T14:00:00{TZ}", f"2026-09-08T15:00:00{TZ}",
                               attendees=["quotes@northlight-seating.example"])
    stub.share(DANA, [{"start": f"2026-09-07T10:00:00{TZ}", "end": f"2026-09-07T12:00:00{TZ}"},
                      {"start": f"2026-09-08T09:00:00{TZ}", "end": f"2026-09-08T17:00:00{TZ}"}])
    me.default = me.add_list("My Tasks")
    me.office = me.add_list("Office move")
    me.quote = me.add_task(me.default, "Send the chair quotation to Dana", due="2026-09-17",
                           notes="Quotation Q-2041 from Northlight, 20 chairs.")
    me.pdf = me.add_task(me.default, "Attach the PDF", parent=me.quote)
    me.desks = me.add_task(me.office, "Order desks for 20 people", due="2026-10-01")
    me.done = me.add_task(me.default, "Book the movers", status="completed")
    stub.set_clock("2026-09-10T09:00:00Z")
    yield stub
    stub.stop()


def chat(google, *accounts, token=""):
    accounts = accounts or (google.me,)
    provider = Records(secrets={"google": [google.secret(a, token) for a in accounts]})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs=None, level=1):
    return run(ex.invoke(agents["google_calendar"], name, inputs or {}, chat_level=level))


def ok(result):
    output, status = result
    assert status == "success", output
    return output


# -- the package ---------------------------------------------------------------------------------------------------

class TestThePackage:
    def test_ready_names_every_function(self, agents):
        assert probe(agents["google_calendar"]) == []

    def test_levels_and_the_one_schedulable_function(self, agents):
        manifest = agents["google_calendar"].manifest
        levels = {f["id"]: f["level"] for f in manifest["functions"]}
        assert levels == {
            "account_status": 0, "calendars_list": 0, "events_list": 0, "events_get": 0, "events_availability": 0,
            "events_find_times": 1, "events_create": 2, "events_book": 3, "events_update": 3, "events_attendees": 3,
            "events_respond": 3, "events_cancel": 3, "lists_list": 0, "lists_create": 2, "tasks_list": 0,
            "tasks_get": 0, "tasks_create": 2, "tasks_update": 2, "tasks_complete": 2, "tasks_reopen": 2,
            "tasks_move": 2, "tasks_delete": 3, "watches_create": 1, "watches_check": 1, "watches_list": 0,
            "watches_close": 1}
        schedulable = [f for f in manifest["functions"] if f.get("schedulable")]
        assert [f["id"] for f in schedulable] == ["watches_check"]
        assert "model" not in schedulable[0]["calls"]
        assert manifest["agent"]["version"] == "2.0.0"
        assert len(manifest["agent"]["instructions"]) <= 4000 and len(manifest["agent"]["examples"]) <= 6
        for function in manifest["functions"]:
            assert "account" in function["inputs"]["properties"], function["id"]

    def test_the_shared_google_secret_for_several_accounts(self, agents):
        manifest = agents["google_calendar"].manifest
        secret = manifest["resources"]["secrets"][0]
        assert secret["id"] == "google" and secret["binding"] == {"cardinality": "many", "required": True}
        assert {"https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/tasks"} <= set(
            secret["oauth"]["scopes"])
        assert set(manifest["network"]["hosts"]) == {"www.googleapis.com", "tasks.googleapis.com"}

    def test_each_api_has_its_own_host_and_a_test_root_serves_both(self):
        from agents.google_calendar.tools.common import over

        real = {"account": ME, "access_token": "x"}
        assert over(real, "calendar").base == "https://www.googleapis.com/calendar/v3"
        assert over(real, "tasks").base == "https://tasks.googleapis.com/tasks/v1"
        assert over({**real, "api_base_url": "https://stand-in.example"}, "tasks").base == \
            "https://stand-in.example/tasks/v1"

    def test_an_event_id_is_base32hex_and_the_same_for_the_same_request(self):
        from agents.google_calendar.tools.events_tool import event_id_for

        made = event_id_for("seed")
        assert made == event_id_for("seed") != event_id_for("other")
        assert set(made) <= set("0123456789abcdefghijklmnopqrstuv") and 5 <= len(made) <= 1024

    def test_an_id_naming_another_path_names_nothing_else(self):
        from agents.google_calendar.tools.events_tool import EventsTool

        assert EventsTool.path("primary", "x/../../calendars/team") == \
            "calendars/primary/events/x%2F..%2F..%2Fcalendars%2Fteam"

    def test_google_tasks_is_absorbed(self):
        assert not (ROOT / "google_tasks").exists()


# -- accounts --------------------------------------------------------------------------------------------------------

class TestAccounts:
    def test_status_names_each_account_its_zone_and_today_there(self, agents, google):
        work = google.account(WORK, "at-2", zone="Europe/London")
        ex, _ = chat(google, google.me, work)
        found = ok(invoke(agents, ex, "account_status"))["accounts"]
        assert [(a["account"], a["connected"], a["timezone"]) for a in found] == [
            (ME, True, "Asia/Dubai"), (WORK, True, "Europe/London")]
        assert all(len(a["today"]) == 10 and "problem" not in a for a in found)

    def test_several_accounts_are_chosen_by_address(self, agents, google):
        work = google.account(WORK, "at-2")
        work.add_event("Works meeting", "2026-09-07T05:00:00Z", "2026-09-07T06:00:00Z")
        ex, _ = chat(google, google.me, work)
        result, status = invoke(agents, ex, "events_list", {"time_min": "2026-09-07", "time_max": "2026-09-08"})
        assert status == "error" and ME in result["error"] and WORK in result["error"]
        mine = ok(invoke(agents, ex, "events_list", {"account": ME, "time_min": "2026-09-07",
                                                     "time_max": "2026-09-08"}))
        theirs = ok(invoke(agents, ex, "events_list", {"account": "works", "time_min": "2026-09-07",
                                                       "time_max": "2026-09-08"}))
        assert [e["summary"] for e in mine["events"]] == ["Ops stand-up"]
        assert [e["summary"] for e in theirs["events"]] == ["Works meeting"] and theirs["account"] == WORK

    def test_no_account_is_a_plain_answer(self, agents):
        found = ok(run(Chat(Records()).invoke(agents["google_calendar"], "account_status", {})))
        assert found["accounts"] == [] and "No Google account" in found["problem"]

    def test_an_expired_token_says_reconnect(self, agents, google):
        ex, _ = chat(google, token="expired")
        result, status = invoke(agents, ex, "events_list", {"time_min": "2026-09-07"})
        assert status == "error" and result["kind"] == "auth" and "reconnect" in result["error"]


# -- reading the calendar --------------------------------------------------------------------------------------------

class TestReading:
    def test_events_are_shown_in_the_calendars_zone(self, agents, google):
        google.me.add_event("Visit", "2026-09-09T11:00:00Z", "2026-09-09T12:00:00Z")
        ex, _ = chat(google)
        found = ok(invoke(agents, ex, "events_list", {"time_min": "2026-09-09", "time_max": "2026-09-10"}))
        assert found["timezone"] == "Asia/Dubai"
        assert (found["events"][0]["start"], found["events"][0]["end"]) == (
            f"2026-09-09T15:00:00{TZ}", f"2026-09-09T16:00:00{TZ}")
        listed = google.sent("GET", "/events")[-1][1]
        assert listed["singleEvents"] == "true" and listed["timeMin"] == f"2026-09-09T00:00:00{TZ}"

    def test_or_in_the_zone_asked_for(self, agents, google):
        ex, _ = chat(google)
        found = ok(invoke(agents, ex, "events_list", {"time_min": "2026-09-07", "time_max": "2026-09-08",
                                                      "timezone": "Europe/London"}))
        assert found["events"][0]["start"] == "2026-09-07T06:00:00+01:00"

    def test_all_day_and_recurring_events_read_right(self, agents, google):
        me = google.me
        me.add_event("Eid holiday", "2026-09-09", "2026-09-11", all_day=True)
        series = me.add_event("Weekly review", f"2026-09-01T16:00:00{TZ}", f"2026-09-01T16:30:00{TZ}",
                              recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"])
        ex, _ = chat(google)
        found = ok(invoke(agents, ex, "events_list", {"time_min": "2026-09-07", "time_max": "2026-09-12"}))
        rows = {e["summary"]: e for e in found["events"]}
        holiday = rows["Eid holiday"]
        assert (holiday["all_day"], holiday["start"], holiday["end"]) == (True, "2026-09-09", "2026-09-10")
        review = rows["Weekly review"]
        assert review["series_id"] == series and review["start"] == f"2026-09-08T16:00:00{TZ}"
        assert review["event_id"] != series

    def test_every_calendar_is_listed_and_usable(self, agents, google):
        team = google.me.add_calendar("team@group.calendar.google.com", "Team", "Asia/Dubai", role="writer")
        google.me.add_event("Team lunch", f"2026-09-07T13:00:00{TZ}", f"2026-09-07T14:00:00{TZ}", calendar_id=team)
        ex, _ = chat(google)
        calendars = ok(invoke(agents, ex, "calendars_list"))["calendars"]
        assert [(c["calendar_id"], c["primary"]) for c in calendars] == [(ME, True), (team, False)]
        found = ok(invoke(agents, ex, "events_list", {"calendar_id": team, "time_min": "2026-09-07",
                                                      "time_max": "2026-09-08"}))
        assert [e["summary"] for e in found["events"]] == ["Team lunch"]

    def test_pages_follow_googles_token_and_a_foreign_one_is_refused(self, agents, google):
        ex, _ = chat(google)
        window = {"time_min": "2026-09-07", "time_max": "2026-09-12", "max_results": 1}
        first = ok(invoke(agents, ex, "events_list", window))
        second = ok(invoke(agents, ex, "events_list", {**window, "page_token": first["next_page_token"]}))
        assert [e["summary"] for e in first["events"] + second["events"]] == ["Ops stand-up", "Supplier call"]
        assert "next_page_token" not in second
        result, status = invoke(agents, ex, "events_list", {**window, "page_token": "made-up"})
        assert status == "error" and result["kind"] == "invalid"

    def test_get_shows_real_responses(self, agents, google):
        ex, _ = chat(google)
        found = ok(invoke(agents, ex, "events_get", {"event_id": google.me.supplier}))
        assert found["responses"] == [{"email": "quotes@northlight-seating.example", "response": "needsAction",
                                       "organizer": False, "optional": False}]


# -- free/busy and finding a time --------------------------------------------------------------------------------

class TestFindingTimes:
    def test_free_busy_asks_for_primary_and_names_who_could_not_be_checked(self, agents, google):
        """1.x asked free/busy for the account's address; the primary calendar is "primary"."""
        ex, _ = chat(google)
        found = ok(invoke(agents, ex, "events_availability", {
            "time_min": "2026-09-07", "time_max": "2026-09-09", "attendees": [DANA, OMAR]}))
        assert found["unchecked"] == [{"who": OMAR, "reason": "notFound"}]
        assert {b["who"] for b in found["busy"]} == {ME, DANA}
        asked = google.sent("POST", "freeBusy")[-1][2]
        assert [i["id"] for i in asked["items"]] == ["primary", DANA, OMAR]

    def test_slots_respect_hours_lunch_buffers_and_every_checked_calendar(self, agents, google):
        ex, provider = chat(google)
        found = ok(invoke(agents, ex, "events_find_times", {
            **WEEK, "duration_minutes": 30, "attendees": [DANA, OMAR], "avoid": [{"start": "12:00", "end": "13:00"}],
            "buffer_minutes": 15, "max_candidates": 4}))
        assert found["unchecked"] == [{"who": OMAR, "reason": "notFound"}]
        assert [(c["weekday"], c["start"][11:16]) for c in found["candidates"]] == [
            ("Monday", "13:00"), ("Monday", "13:45"), ("Wednesday", "09:00"), ("Wednesday", "09:45")]
        first = provider.data["proposal"][found["candidates"][0]["proposal_ref"]]
        assert first["keys"]["status"] == "offered" and first["keys"]["account"] == ME
        assert first["values"]["attendees"] == f"{DANA}, {OMAR}"     # personal data is encrypted

    def test_working_days_are_an_input(self, agents, google):
        """1.x fixed Monday to Friday; in the Gulf the week is Sunday to Thursday."""
        ex, _ = chat(google)
        gulf = ok(invoke(agents, ex, "events_find_times", {
            "from_date": "2026-09-11", "to_date": "2026-09-13", "not_before": f"2026-09-06T08:00:00{TZ}",
            "duration_minutes": 60, "working_days": ["sunday", "monday", "tuesday", "wednesday", "thursday"]}))
        assert {c["weekday"] for c in gulf["candidates"]} == {"Sunday"}
        default = ok(invoke(agents, ex, "events_find_times", {
            "from_date": "2026-09-11", "to_date": "2026-09-13", "not_before": f"2026-09-06T08:00:00{TZ}",
            "duration_minutes": 60}))
        assert {c["weekday"] for c in default["candidates"]} == {"Friday"}

    def test_refusals_say_why_with_the_numbers(self, agents, google):
        ex, _ = chat(google)
        result, status = invoke(agents, ex, "events_find_times", {
            "from_date": "2026-09-01", "to_date": "2026-10-15", "duration_minutes": 30})
        assert status == "error" and "45 days" in result["error"]
        result, status = invoke(agents, ex, "events_find_times", {**WEEK, "duration_minutes": 30,
                                                                  "timezone": "Mars/Olympus"})
        assert status == "error" and "IANA" in result["error"]


# -- creating and booking ----------------------------------------------------------------------------------------

class TestBooking:
    def test_a_proposal_is_booked_with_meet_only_with_the_chats_trust(self, agents, google):
        ex, provider = chat(google)
        found = ok(invoke(agents, ex, "events_find_times", {**WEEK, "duration_minutes": 30, "attendees": [DANA]}))
        pick = found["candidates"][0]["proposal_ref"]
        refused, status = invoke(agents, ex, "events_book", {"summary": "Office furniture", "proposal_ref": pick})
        assert status == "error" and refused.get("denied") is True and google.sent("POST", "/events") == []

        booked = ok(invoke(agents, ex, "events_book", {"summary": "Office furniture", "proposal_ref": pick,
                                                        "meet": True, "send_updates": "externalOnly"}, level=3))
        assert booked["invited"] == [DANA] and booked["start"] == found["candidates"][0]["start"]
        assert booked["meet"] == "success" and booked["meet_link"].startswith("https://meet.google.com/")
        path, query, body = google.sent("POST", "/events")[-1]
        assert query == {"sendUpdates": "externalOnly", "conferenceDataVersion": "1"}
        assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
        assert provider.data["proposal"][pick]["keys"]["status"] == "booked"
        again, status = invoke(agents, ex, "events_book", {"summary": "Again", "proposal_ref": pick}, level=3)
        assert status == "error" and "already booked" in again["error"]

    def test_meet_needs_conference_data_version_one(self, agents, google):
        """Google ignores conferenceData without conferenceDataVersion=1, and so does the stub."""
        ex, _ = chat(google)
        ok(invoke(agents, ex, "events_book", {"summary": "No Meet", "start": "2026-09-10T15:00",
                                              "duration_minutes": 30, "attendees": [DANA]}, level=3))
        stored = [e for e in google.me.events[ME].values() if e["summary"] == "No Meet"][0]
        assert "conferenceData" not in stored and "hangoutLink" not in stored

    def test_the_same_booking_twice_is_one_event(self, agents, google):
        """1.x sent no client id, so a retry made a second meeting."""
        ex, _ = chat(google)
        asked = {"summary": "Site visit", "start": "2026-09-10T15:00", "end": "2026-09-10T16:00",
                 "attendees": [DANA]}
        first = ok(invoke(agents, ex, "events_book", asked, level=3))
        second = ok(invoke(agents, ex, "events_book", asked, level=3))
        assert first["already"] is False and second["already"] is True
        assert first["event_id"] == second["event_id"] and len(google.sent("POST", "/events")) == 1

    def test_a_lost_answer_is_found_by_its_id_not_sent_twice(self, agents, google):
        ex, provider = chat(google)
        google.drop_next_insert = True
        booked = ok(invoke(agents, ex, "events_book", {"summary": "Maybe", "start": "2026-09-10T15:00",
                                                        "end": "2026-09-10T16:00", "attendees": [DANA]}, level=3))
        assert booked["event_id"] in google.me.events[ME]
        assert len(google.sent("POST", "/events")) == 1
        assert len(provider.data["booking"]) == 1

    def test_the_slot_is_checked_again_just_before_booking(self, agents, google):
        """1.x booked a proposal blindly, even after the time was taken."""
        ex, _ = chat(google)
        found = ok(invoke(agents, ex, "events_find_times", {**WEEK, "duration_minutes": 30, "attendees": [DANA]}))
        pick = found["candidates"][0]
        google.me.add_event("Booked meanwhile", pick["start"], pick["end"])
        result, status = invoke(agents, ex, "events_book", {"summary": "Too late", "proposal_ref":
                                                            pick["proposal_ref"]}, level=3)
        assert status == "error" and result["kind"] == "conflict" and "no longer free" in result["error"]
        assert google.sent("POST", "/events") == []
        booked = ok(invoke(agents, ex, "events_book", {"summary": "Anyway", "proposal_ref": pick["proposal_ref"],
                                                        "book_anyway": True}, level=3))
        assert booked["event_id"]

    def test_my_own_event_and_an_all_day_one(self, agents, google):
        ex, _ = chat(google)
        result, status = invoke(agents, ex, "events_create", {"summary": "Focus", "start": "2026-09-10T15:00"},
                                level=2)
        assert status == "error" and "duration_minutes" in result["error"]
        made = ok(invoke(agents, ex, "events_create", {"summary": "Focus", "start": "2026-09-10T15:00",
                                                       "duration_minutes": 90}, level=2))
        assert made["end"] == f"2026-09-10T16:30:00{TZ}"
        assert google.sent("POST", "/events")[-1][1] == {"sendUpdates": "none"}
        off = ok(invoke(agents, ex, "events_create", {"summary": "إجازة", "start": "2026-09-13", "end": "2026-09-14",
                                                      "all_day": True}, level=2))
        body = google.sent("POST", "/events")[-1][2]
        assert body["start"] == {"date": "2026-09-13"} and body["end"] == {"date": "2026-09-15"}
        assert (off["start"], off["end"]) == ("2026-09-13", "2026-09-14")
        assert google.me.events[ME][off["event_id"]]["summary"] == "إجازة"


# -- changing an event -------------------------------------------------------------------------------------------

class TestChanging:
    def test_a_move_keeps_the_length_and_sends_start_and_end_together(self, agents, google):
        """1.x patched start alone: the meeting changed length, or Google refused it."""
        ex, provider = chat(google)
        moved = ok(invoke(agents, ex, "events_update", {"event_id": google.me.supplier,
                                                        "start": "2026-09-08T16:00"}, level=3))
        assert (moved["start"], moved["end"]) == (f"2026-09-08T16:00:00{TZ}", f"2026-09-08T17:00:00{TZ}")
        path, query, body = google.sent("PATCH")[-1]
        assert set(body) == {"start", "end"} and query == {"sendUpdates": "all"}
        longer = ok(invoke(agents, ex, "events_update", {"event_id": google.me.supplier, "duration_minutes": 90,
                                                         "send_updates": "none"}, level=3))
        assert longer["end"] == f"2026-09-08T17:30:00{TZ}"
        result, status = invoke(agents, ex, "events_update", {"event_id": google.me.supplier,
                                                              "start": "2026-09-08T16:00", "end": "2026-09-08T15:00"},
                                level=3)
        assert status == "error" and "before it starts" in result["error"] and len(google.sent("PATCH")) == 2
        assert [b["keys"]["status"] for b in provider.data["booking"].values()] == ["updated", "updated"]

    def test_google_refuses_an_end_before_the_start(self, agents, google):
        """The stub is as strict as Google: a time range that is empty is 400."""
        ex, _ = chat(google)
        google.me.events[ME][google.me.standup]["end"]["dateTime"] = f"2026-09-07T08:00:00{TZ}"
        result, status = invoke(agents, ex, "events_update", {"event_id": google.me.standup, "summary": "x"},
                                level=3)
        assert status == "error" and result["kind"] == "invalid" and "time range is empty" in result["error"]

    def test_one_occurrence_of_a_series_moves_alone(self, agents, google):
        series = google.me.add_event("Weekly review", f"2026-09-01T16:00:00{TZ}", f"2026-09-01T16:30:00{TZ}",
                                     recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"])
        ex, _ = chat(google)
        week = ok(invoke(agents, ex, "events_list", {"time_min": "2026-09-08", "time_max": "2026-09-09"}))
        one = next(e for e in week["events"] if e.get("series_id") == series)
        moved = ok(invoke(agents, ex, "events_update", {"event_id": one["event_id"], "start": "2026-09-08T17:00"},
                          level=3))
        assert moved["end"] == f"2026-09-08T17:30:00{TZ}" and moved["whole_series"] is False
        later = ok(invoke(agents, ex, "events_list", {"time_min": "2026-09-15", "time_max": "2026-09-16"}))
        assert [e["start"] for e in later["events"]] == [f"2026-09-15T16:00:00{TZ}"]

    def test_attendees_are_added_and_removed(self, agents, google):
        ex, _ = chat(google)
        found = ok(invoke(agents, ex, "events_attendees", {"event_id": google.me.supplier, "add": [DANA],
                                                           "remove": ["quotes@northlight-seating.example", OMAR]},
                          level=3))
        assert found["attendees"] == [DANA] and found["not_attending"] == [OMAR]

    def test_someone_elses_meeting_is_answered_not_moved(self, agents, google):
        invite = google.me.add_event("Their kickoff", f"2026-09-09T10:00:00{TZ}", f"2026-09-09T11:00:00{TZ}",
                                     organizer=DANA)
        ex, _ = chat(google)
        result, status = invoke(agents, ex, "events_update", {"event_id": invite, "start": "2026-09-09T12:00"},
                                level=3)
        assert status == "error" and result["kind"] == "denied" and DANA in result["error"]
        assert google.sent("PATCH") == []
        answered = ok(invoke(agents, ex, "events_respond", {"event_id": invite, "response": "tentative"}, level=3))
        assert answered["response"] == "tentative" and answered["organizer"] == DANA

    def test_cancel_deletes_only_as_organiser_and_says_which_happened(self, agents, google):
        """1.x deleted any event and said the attendees were told, even when it only left it."""
        invite = google.me.add_event("Their kickoff", f"2026-09-09T10:00:00{TZ}", f"2026-09-09T11:00:00{TZ}",
                                     organizer=DANA)
        ex, provider = chat(google)
        mine = ok(invoke(agents, ex, "events_cancel", {"event_id": google.me.supplier}, level=3))
        assert mine["done"] == "cancelled" and mine["told"] == ["quotes@northlight-seating.example"]
        assert google.sent("DELETE")[-1][1] == {"sendUpdates": "all"}
        theirs = ok(invoke(agents, ex, "events_cancel", {"event_id": invite}, level=3))
        assert theirs["done"] == "declined" and theirs["told"] == [DANA] and DANA in theirs["why"]
        stored = google.me.events[ME][invite]
        assert stored["status"] == "confirmed"
        assert next(a for a in stored["attendees"] if a.get("self"))["responseStatus"] == "declined"
        assert len(google.sent("DELETE")) == 1
        assert [b["keys"]["status"] for b in provider.data["booking"].values()] == ["cancelled", "declined"]


# -- rate limits -------------------------------------------------------------------------------------------------

class TestBusyGoogle:
    def test_429_and_403_rate_limit_are_waited_out(self, agents, google):
        ex, _ = chat(google)
        google.throttle = [(429, "0"), (403, "rateLimitExceeded")]
        found = ok(invoke(agents, ex, "events_list", {"time_min": "2026-09-07", "time_max": "2026-09-08"}))
        assert [e["summary"] for e in found["events"]] == ["Ops stand-up"]

    def test_a_write_is_retried_on_429_because_google_did_nothing(self, agents, google):
        ex, _ = chat(google)
        google.throttle = [(429, "0")]
        made = ok(invoke(agents, ex, "tasks_create", {"title": "After a pause"}, level=2))
        assert google.me.task(made["task_id"])["title"] == "After a pause"

    def test_a_lasting_limit_is_reported_as_rate(self, agents, google):
        ex, _ = chat(google)
        google.throttle = [(403, "userRateLimitExceeded")] * 8
        result, status = invoke(agents, ex, "lists_list")
        assert status == "error" and result["kind"] == "rate"


# -- tasks -------------------------------------------------------------------------------------------------------

class TestTasks:
    def test_lists_and_open_tasks(self, agents, google):
        me = google.me
        ex, _ = chat(google)
        lists = ok(invoke(agents, ex, "lists_list"))["lists"]
        assert [(r["list_id"], r["default"]) for r in lists] == [(me.default, True), (me.office, False)]
        found = ok(invoke(agents, ex, "tasks_list"))
        assert found["list_id"] == me.default
        assert [t["title"] for t in found["tasks"]] == ["Send the chair quotation to Dana", "Attach the PDF"]
        assert found["tasks"][1]["parent"] == me.quote and found["tasks"][0]["due"] == "2026-09-17"
        done = ok(invoke(agents, ex, "tasks_list", {"status": "completed"}))
        assert [t["title"] for t in done["tasks"]] == ["Book the movers"]     # hidden, ticked off in the app
        soon = ok(invoke(agents, ex, "tasks_list", {"due_before": "2026-09-20"}))
        assert [t["task_id"] for t in soon["tasks"]] == [me.quote]

    def test_more_than_a_thousand_tasks_are_paged(self, agents, google):
        """1.x read ten pages of a hundred and stopped there, silently."""
        me = google.me
        for n in range(1050):
            me.add_task(me.office, f"Label box {n}")
        ex, _ = chat(google)
        seen, token, pages = [], "", 0
        while True:
            found = ok(invoke(agents, ex, "tasks_list", {"list_id": me.office, "max_results": 100,
                                                         **({"page_token": token} if token else {})}))
            seen += [t["task_id"] for t in found["tasks"]]
            pages += 1
            token = found.get("next_page_token", "")
            if not token:
                break
        assert len(seen) == len(set(seen)) == 1051 and pages == 11

    def test_create_update_complete_reopen_with_dates_only(self, agents, google):
        me = google.me
        ex, provider = chat(google)
        made = ok(invoke(agents, ex, "tasks_create", {"list_id": me.office, "title": "قياس غرفة الاجتماعات",
                                                      "due": "2026-09-18", "note": "Both walls."}, level=2))
        assert me.task(made["task_id"])["due"] == "2026-09-18T00:00:00.000Z"
        assert made["title"] == "قياس غرفة الاجتماعات" and made["due"] == "2026-09-18"
        updated = ok(invoke(agents, ex, "tasks_update", {"list_id": me.office, "task_id": made["task_id"],
                                                         "title": "Measure both rooms", "due": ""}, level=2))
        assert "due" not in updated and "due" not in me.task(made["task_id"])
        done = ok(invoke(agents, ex, "tasks_complete", {"list_id": me.office, "task_id": made["task_id"]}, level=2))
        assert done["status"] == "completed" and done["completed"].startswith("2026-09-10T09:00:00.")
        again = ok(invoke(agents, ex, "tasks_reopen", {"list_id": me.office, "task_id": made["task_id"]}, level=2))
        assert again["status"] == "needsAction" and "completed" not in again
        result, status = invoke(agents, ex, "tasks_create", {"title": "x", "due": "2026-02-30"}, level=2)
        assert status == "error" and result["kind"] == "invalid"

    def test_subtasks_and_moves(self, agents, google):
        me = google.me
        ex, _ = chat(google)
        sub = ok(invoke(agents, ex, "tasks_create", {"title": "Check the delivery date", "parent": me.quote},
                        level=2))
        assert (sub["list_id"], sub["parent"]) == (me.default, me.quote)
        whole = ok(invoke(agents, ex, "tasks_get", {"task_id": me.quote}))
        assert [s["title"] for s in whole["subtasks"]] == ["Attach the PDF", "Check the delivery date"]
        moved = ok(invoke(agents, ex, "tasks_move", {"task_id": sub["task_id"], "to_list_id": me.office,
                                                     "to_top": True}, level=2))
        assert moved["list_id"] == me.office and "parent" not in moved
        assert google.sent("POST", "/move")[-1][1] == {"destinationTasklist": me.office}
        result, status = invoke(agents, ex, "tasks_move", {"task_id": me.done, "parent": me.quote}, level=2)
        assert status == "error" and result["kind"] == "invalid"

    def test_deleting_needs_the_chats_trust(self, agents, google):
        me = google.me
        ex, _ = chat(google)
        refused, status = invoke(agents, ex, "tasks_delete", {"task_id": me.quote}, level=2)
        assert status == "error" and refused.get("denied") is True
        gone = ok(invoke(agents, ex, "tasks_delete", {"task_id": me.quote}, level=3))
        assert gone == {"deleted": True, "task_id": me.quote, "title": "Send the chair quotation to Dana"}
        assert me.task(me.quote)["deleted"] is True and me.task(me.pdf)["deleted"] is True


# -- watches -----------------------------------------------------------------------------------------------------

class TestWatches:
    def test_a_completion_is_handed_on_once_from_googles_clock(self, agents, google):
        me = google.me
        ex, provider = chat(google)
        watch = ok(invoke(agents, ex, "watches_create", {"kind": "completed"}))
        assert watch["since"] == "2026-09-10T09:00:00.000Z" and watch["list_name"] == "(every list)"
        assert ok(invoke(agents, ex, "watches_check"))["rows"] == []
        me.edit_in_app(me.desks, title="Order desks for 22 people")     # an edit is not a completion
        me.complete_in_app(me.quote)
        news = ok(invoke(agents, ex, "watches_check"))
        assert [(r["task_id"], r["list_name"], r["kind"]) for r in news["rows"]] == [
            (me.quote, "My Tasks", "completed")]
        assert ok(invoke(agents, ex, "watches_check"))["rows"] == []
        me.edit_in_app(me.quote, notes="Sent on Thursday.")              # done already: not again
        assert ok(invoke(agents, ex, "watches_check"))["rows"] == []

    def test_its_own_edits_are_not_news(self, agents, google):
        """1.x reported its own edits back, so a two-way copy with Tasks looped."""
        me = google.me
        ex, provider = chat(google)
        changed = ok(invoke(agents, ex, "watches_create", {"kind": "changed"}))
        completed = ok(invoke(agents, ex, "watches_create", {"kind": "completed", "list_id": me.office}))
        ok(invoke(agents, ex, "tasks_complete", {"list_id": me.office, "task_id": me.desks}, level=2))
        ok(invoke(agents, ex, "tasks_create", {"title": "Mirror of a Tasks item"}, level=2))
        assert ok(invoke(agents, ex, "watches_check"))["rows"] == []
        me.edit_in_app(me.desks, notes="Delivered.")                      # the person's edit afterwards is news
        news = ok(invoke(agents, ex, "watches_check"))["rows"]
        assert [(r["watch_ref"], r["task_id"]) for r in news] == [(changed["watch_ref"], me.desks)]
        own = json.loads(provider.data["watch"][changed["watch_ref"]]["keys"]["own"])
        assert own == {k: v for k, v in own.items() if k != me.desks}
        assert completed["watch_ref"] in provider.data["watch"]

    def test_the_cursor_keeps_googles_milliseconds(self, agents, google):
        """1.x cut cursors to the second, so a second edit of a task within that second was lost."""
        me = google.me
        ex, provider = chat(google)
        watch = ok(invoke(agents, ex, "watches_create", {"kind": "changed", "list_id": me.office}))
        me.edit_in_app(me.desks, title="Desks: 21")
        assert [r["title"] for r in ok(invoke(agents, ex, "watches_check"))["rows"]] == ["Desks: 21"]
        me.edit_in_app(me.desks, title="Desks: 22")                      # the same second, a millisecond later
        assert [r["title"] for r in ok(invoke(agents, ex, "watches_check"))["rows"]] == ["Desks: 22"]
        cursor = provider.data["watch"][watch["watch_ref"]]["keys"]["cursor"]
        assert cursor == me.task(me.desks)["updated"] and cursor.startswith("2026-09-10T09:00:00.")
        # The second check asked from the first edit's own millisecond.
        assert google.sent("GET", "/tasks")[-1][1]["updatedMin"] == "2026-09-10T09:00:00.001Z"

    def test_two_tasks_in_one_instant_are_neither_lost_nor_repeated(self, agents, google):
        me = google.me
        ex, _ = chat(google)
        ok(invoke(agents, ex, "watches_create", {"kind": "completed"}))
        me.complete_in_app(me.quote)
        me.complete_in_app(me.desks)
        me.task(me.desks)["completed"] = me.task(me.quote)["completed"]
        first = ok(invoke(agents, ex, "watches_check", {"max_results": 1}))
        second = ok(invoke(agents, ex, "watches_check", {"max_results": 1}))
        third = ok(invoke(agents, ex, "watches_check"))
        assert first["more"] is True and second["more"] is False
        assert sorted(r["task_id"] for r in first["rows"] + second["rows"]) == sorted([me.quote, me.desks])
        assert third["rows"] == []

    def test_a_long_change_list_is_paged_across_checks(self, agents, google):
        me = google.me
        ex, _ = chat(google)
        ok(invoke(agents, ex, "watches_create", {"kind": "changed", "list_id": me.office}))
        for n in range(250):
            me.add_task(me.office, f"Box {n}")
        runs = [ok(invoke(agents, ex, "watches_check", {"max_results": 100})) for _ in range(3)]
        assert [len(r["rows"]) for r in runs] == [100, 100, 50]
        assert [r["more"] for r in runs] == [True, True, False]
        assert len({row["task_id"] for r in runs for row in r["rows"]}) == 250

    def test_one_failing_watch_keeps_its_place_and_the_others_go_on(self, agents, google):
        """1.x moved earlier watches' cursors, then failed the whole run on a later one: rows were lost."""
        me = google.me
        work = google.account(WORK, "at-2")
        work.inbox = work.add_list("Works")
        work.job = work.add_task(work.inbox, "Ship order")
        ex, provider = chat(google, me, work)
        mine = ok(invoke(agents, ex, "watches_create", {"account": ME, "kind": "changed"}))
        theirs = ok(invoke(agents, ex, "watches_create", {"account": WORK, "kind": "changed"}))
        held = dict(provider.data["watch"][theirs["watch_ref"]]["keys"])
        work.edit_in_app(work.job, title="Ship order 7")
        me.edit_in_app(me.desks, title="Desks: 22")
        google.accounts.pop("at-2")                                       # that account's token stops working
        found = ok(invoke(agents, ex, "watches_check"))
        assert [r["task_id"] for r in found["rows"]] == [me.desks]
        assert [(p["watch_ref"], p["kind"]) for p in found["problems"]] == [(theirs["watch_ref"], "auth")]
        assert provider.data["watch"][theirs["watch_ref"]]["keys"]["cursor"] == held["cursor"]
        assert provider.data["watch"][mine["watch_ref"]]["keys"]["cursor"] != held["cursor"]

    def test_a_gone_list_closes_its_watch(self, agents, google):
        me = google.me
        ex, provider = chat(google)
        watch = ok(invoke(agents, ex, "watches_create", {"list_id": me.office}))
        me.delete_list(me.office)
        found = ok(invoke(agents, ex, "watches_check"))
        assert [c["watch_ref"] for c in found["closed"]] == [watch["watch_ref"]] and found["problems"] == []
        assert provider.data["watch"][watch["watch_ref"]]["keys"]["status"] == "closed"
        assert ok(invoke(agents, ex, "watches_check"))["checked"] == 0

    def test_watches_are_listed_and_closed(self, agents, google):
        ex, _ = chat(google)
        watch = ok(invoke(agents, ex, "watches_create", {"kind": "changed"}))
        listed = ok(invoke(agents, ex, "watches_list"))["watches"]
        assert [(w["watch_ref"], w["kind"], w["account"], w["status"]) for w in listed] == [
            (watch["watch_ref"], "changed", ME, "active")]
        assert ok(invoke(agents, ex, "watches_close", {"watch_ref": watch["watch_ref"]})) == {
            "watch_ref": watch["watch_ref"], "closed": True, "already": False}
        assert ok(invoke(agents, ex, "watches_list"))["watches"] == []
        assert ok(invoke(agents, ex, "watches_list", {"include_closed": True}))["watches"][0]["status"] == "closed"

    def test_a_bad_since_is_refused_and_records_nothing(self, agents, google):
        ex, provider = chat(google)
        result, status = invoke(agents, ex, "watches_create", {"since": "yesterday"})
        assert status == "error" and result["kind"] == "invalid" and "watch" not in provider.data


def test_the_manifest_is_valid_yaml_with_unique_function_ids():
    manifest = yaml.safe_load((ROOT / "google_calendar" / "manifest.yaml").read_text(encoding="utf-8"))
    ids = [f["id"] for f in manifest["functions"]]
    assert len(ids) == len(set(ids)) <= 100
    assert timedelta(seconds=max(f.get("timeout_seconds", 60) for f in manifest["functions"])) <= timedelta(
        hours=1)
