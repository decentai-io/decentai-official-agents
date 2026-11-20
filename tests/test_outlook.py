"""The Outlook agent (mail, calendar and To Do), its program started and spoken to in the standard's messages, against
a loopback Microsoft Graph as strict as the real one (tests/graph_stub.py) holding fictional data of Sidra Office
Supplies. Each bug seen with real accounts has a test that would have caught it."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from tests.graph_stub import FOUR_MB, GraphStub
from tests.harness import Chat, Records, probe, run

SUPPLIER = "quotes@northlight-seating.example"
CUSTOMER = "dana@harbourline.example"
COLLEAGUE = "omar@sidra.example"
QUOTE_PDF = b"%PDF-1.4 fictional quotation " + bytes(range(256))


@pytest.fixture
def graph():
    stub = GraphStub().start()
    stub.add_message("t-quote", SUPPLIER, GraphStub.WORK, "Quotation Q-2041 — 20 office chairs",
                     "<p>Please find our quotation attached.</p><p>Delivery in 10 working days.</p>",
                     "Thu, 03 Sep 2026 10:12:00 +0400", html_body=True,
                     attachments=[("Q-2041.pdf", "application/pdf", QUOTE_PDF),
                                  ("logo.png", "image/png", b"\x89PNG logo", True)])
    stub.add_message("t-office", CUSTOMER, f"{GraphStub.WORK}, {COLLEAGUE}", "New office — furniture for 20 desks",
                     "We are moving in November and need desks and chairs for 20 people.",
                     "Wed, 02 Sep 2026 15:40:00 +0400", cc="facilities@harbourline.example")
    yield stub
    stub.stop()


def chat(graph, kind="work", token="", sinks=None):
    provider = Records(secrets={"microsoft": graph.secret(kind, token)})
    return Chat(provider, sinks), provider


def invoke(agents, ex, name, inputs=None, chat_level=3):
    return run(ex.invoke(agents["outlook"], name, inputs or {}, chat_level=chat_level))


def ok(agents, ex, name, inputs=None, chat_level=3):
    result, status = invoke(agents, ex, name, inputs, chat_level)
    assert status == "success", result
    return result


def asked(graph, method, needle):
    return [r for r in graph.requests if r["method"] == method and needle in r["path"]]


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        assert probe(agents["outlook"]) == []

    def test_the_manifest_keeps_the_briefs_bounds(self, agents):
        manifest = agents["outlook"].manifest
        assert manifest["agent"]["version"] == "2.0.1"
        assert 30 <= len(manifest["functions"]) <= 45
        assert len(manifest["agent"]["instructions"]) <= 4000 and len(manifest["agent"]["examples"]) <= 6
        for start in ("mail_list", "mail_find", "mail_thread", "calendar_list", "tasks_list"):
            assert start in manifest["agent"]["instructions"]

    def test_the_secret_is_the_shared_microsoft_block(self, agents):
        [secret] = agents["outlook"].manifest["resources"]["secrets"]
        assert "MailboxSettings.Read" in secret["oauth"]["scopes"]
        assert secret["binding"] == {"cardinality": "many", "required": True}

    def test_levels_follow_the_agreed_rule(self, agents):
        levels = {f["id"]: f["level"] for f in agents["outlook"].manifest["functions"]}
        assert {k for k, v in levels.items() if v == 3} == {
            "draft_send", "calendar_invite", "calendar_update", "calendar_cancel", "calendar_respond", "tasks_delete"}
        assert levels["mail_save_attachment"] == 0 and levels["calendar_create"] == 2
        assert all(levels[k] == 2 for k in ("draft_compose", "draft_reply", "mail_mark", "mail_archive",
                                            "tasks_create", "tasks_complete"))

    def test_schedulable_checks_are_quiet_and_low(self, agents):
        for function in agents["outlook"].manifest["functions"]:
            if function.get("schedulable"):
                assert function["level"] <= 1 and "model" not in function["calls"], function["id"]
                assert not any(c.startswith("record.create:microsoft") for c in function["calls"])
        assert {f["id"] for f in agents["outlook"].manifest["functions"] if f.get("schedulable")} == {
            "watch_check", "watch_new_mail", "watch_task_changes"}

    def test_mail_content_is_kept_in_values(self, agents):
        draft = next(d for d in agents["outlook"].manifest["resources"]["data"] if d["id"] == "draft")
        keys = {f["name"] for f in draft["fields"] if f["storage"] == "keys"}
        assert not keys & {"to", "subject", "body", "content"}


class TestTheAccount:
    def test_a_work_account_names_every_address_and_its_zone(self, agents, graph):
        ex, _ = chat(graph)
        result = ok(agents, ex, "account_status")
        assert result["connected"] and result["email"] == GraphStub.WORK
        assert {GraphStub.WORK, GraphStub.WORK_UPN, GraphStub.WORK_ALIAS} <= set(result["addresses"])
        assert (result["kind"], result["timezone"]) == ("work or school", "Asia/Dubai")
        assert result["today"] == datetime.now(timezone(timedelta(hours=4))).date().isoformat()

    def test_a_personal_account_says_so(self, agents, graph):
        ex, _ = chat(graph, "personal")
        assert ok(agents, ex, "account_status")["kind"] == "personal"

    def test_several_accounts_one_is_named(self, agents, graph):
        provider = Records(secrets={"microsoft": [{"ref": "sec_w", **graph.secret("work")},
                                                  {"ref": "sec_p", **graph.secret("personal")}]})
        ex = Chat(provider)
        assert [a["account"] for a in ok(agents, ex, "account_list")["accounts"]] == [GraphStub.WORK,
                                                                                       GraphStub.PERSONAL]
        result, status = invoke(agents, ex, "mail_list", {})
        assert status == "error" and GraphStub.PERSONAL in result["error"]
        assert ok(agents, ex, "account_status", {"account": "outlook.example"})["kind"] == "personal"

    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, graph):
        ex, provider = chat(graph, token="expired")
        result, status = invoke(agents, ex, "mail_find", {"query": "chairs"})
        assert status == "error" and result["kind"] == "auth" and "reconnect" in result["error"]
        result, status = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "x", "body": "y"})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        result, status = invoke(agents, Chat(Records()), "account_status")
        assert status == "success" and result["connected"] is False and "No Microsoft account" in result["problem"]


class TestReading:
    def test_a_thread_is_read_without_the_order_graph_refuses(self, agents, graph):
        """Seen live: "The restriction or sort order is too complex for this operation" (filter + orderby)."""
        ex, _ = chat(graph)
        result = ok(agents, ex, "mail_thread", {"thread_id": "t-quote"})
        assert result["complete"] and result["message_count"] == 1
        conversation = [r for r in graph.requests if r["query"].get("$filter", "").startswith("conversationId")]
        assert conversation and all("$orderby" not in r["query"] for r in conversation)

    def test_the_stub_refuses_what_graph_refuses(self, graph):
        auth = {"Authorization": f"Bearer {graph.work_token}"}
        refused = requests.get(f"{graph.local}/api/me/messages", headers=auth, params={
            "$filter": "conversationId eq 't-quote'", "$orderby": "receivedDateTime asc"})
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "InefficientFilter"
        refused = requests.get(f"{graph.local}/api/me/messages", headers=auth, params={
            "$search": '"chairs"', "$orderby": "receivedDateTime desc"})
        assert refused.status_code == 400
        assert requests.post(f"{graph.local}/api/me/calendar/getSchedule", json={},
                             headers={"Authorization": f"Bearer {graph.personal_token}"}).status_code == 403
        narrow = graph.issue("work", scopes={"Mail.ReadWrite"})
        assert requests.get(f"{graph.local}/api/me/mailboxSettings",
                            headers={"Authorization": f"Bearer {narrow}"}).status_code == 403

    def test_a_thread_gives_each_message_whole_html_as_text(self, agents, graph):
        ex, _ = chat(graph)
        graph.add_message("t-quote", GraphStub.WORK_ALIAS, SUPPLIER, "RE: Quotation Q-2041",
                          "<div>تم استلام العرض، شكراً.</div><blockquote>old</blockquote>",
                          "Fri, 04 Sep 2026 09:00:00 +0400",
                          html_body=True, unique="<div>تم استلام العرض، شكراً.</div>")
        result = ok(agents, ex, "mail_thread", {"thread_id": "t-quote"})
        first, second = result["messages"]
        assert "Delivery in 10 working days." in first["body"] and "<p>" not in first["body"]
        assert second["body"] == "تم استلام العرض، شكراً." and second["mine"] is True
        assert first["date"].endswith("+04:00")

    def test_a_long_conversation_is_paged_never_cut(self, agents, graph):
        ex, _ = chat(graph)
        long_text = "".join(f"Line {n}: the schedule of deliveries for the Harbourline office.\n" for n in range(900))
        graph.add_message("t-long", CUSTOMER, GraphStub.WORK, "Schedule", long_text, "Mon, 07 Sep 2026 08:00:00 +0400")
        graph.add_message("t-long", CUSTOMER, GraphStub.WORK, "Schedule 2", "And one more thing.",
                          "Mon, 07 Sep 2026 09:00:00 +0400")
        first = ok(agents, ex, "mail_thread", {"thread_id": "t-long", "max_chars": 20000})
        assert first["complete"] is False and first["next"] == {"start": 0, "offset": 20000}
        read, step = first["messages"][0]["body"], first["next"]
        while step:
            more = ok(agents, ex, "mail_thread", {"thread_id": "t-long", "max_chars": 20000, **step})
            read += more["messages"][0]["body"] if more["messages"][0]["index"] == 0 else ""
            last = more
            step = more.get("next")
        assert read == long_text.strip()
        assert last["messages"][-1]["body"] == "And one more thing."

    def test_one_message_is_read_whole_in_parts(self, agents, graph):
        ex, _ = chat(graph)
        body = "<p>" + "Ü" * 30000 + "</p><p>END</p>"
        message_id = graph.add_message("t-big", CUSTOMER, GraphStub.WORK, "Big", body,
                                       "Mon, 07 Sep 2026 10:00:00 +0400",
                                       html_body=True)
        first = ok(agents, ex, "mail_read", {"message_id": message_id, "max_chars": 20000})
        assert first["next_offset"] == 20000 and first["body_chars"] > 30000
        rest = ok(agents, ex, "mail_read", {"message_id": message_id, "offset": first["next_offset"]})
        assert rest["body"].endswith("END") and "next_offset" not in rest

    def test_attachments_are_listed_by_kind_inline_pictures_left_out(self, agents, graph):
        ex, _ = chat(graph)
        message_id = graph.add_message("t-kinds", SUPPLIER, GraphStub.WORK, "Files", "See attached.",
                                       "Tue, 08 Sep 2026 10:00:00 +0400")
        graph.add_attachment(message_id, "Spec.docx", "application/vnd.openxmlformats", b"PK docx")
        graph.add_attachment(message_id, "Earlier mail", "message/rfc822", b"Subject: earlier\r\n\r\nHi", "item")
        graph.add_attachment(message_id, "Big plan.pdf", "application/pdf", b"", "reference")
        graph.add_attachment(message_id, "sig.png", "image/png", b"\x89PNG", True)
        result = ok(agents, ex, "mail_read", {"message_id": message_id})
        assert [(a["filename"], a["kind"]) for a in result["attachments"]] == [
            ("Spec.docx", "file"), ("Earlier mail", "item"), ("Big plan.pdf", "reference")]

    def test_an_attachment_is_saved_into_the_chat_byte_for_byte(self, agents, graph):
        ex, _ = chat(graph)
        thread = ok(agents, ex, "mail_thread", {"thread_id": "t-quote"})
        [pdf] = thread["attachments"]
        result = ok(agents, ex, "mail_save_attachment", {"message_id": pdf["message_id"],
                                                         "attachment_id": pdf["attachment_id"]}, chat_level=0)
        assert ex.files[result["file_ref"]]["content"] == QUOTE_PDF and result["filename"] == "Q-2041.pdf"

    def test_an_attached_message_comes_as_eml_and_a_link_is_named(self, agents, graph):
        ex, _ = chat(graph)
        message_id = graph.add_message("t-x", SUPPLIER, GraphStub.WORK, "Fwd", "x", "Tue, 08 Sep 2026 10:00:00 +0400")
        item = graph.add_attachment(message_id, "Earlier mail", "message/rfc822", b"Subject: earlier\r\n\r\nHi", "item")
        link = graph.add_attachment(message_id, "Plan.pdf", "application/pdf", b"", "reference")
        saved = ok(agents, ex, "mail_save_attachment", {"message_id": message_id, "attachment_id": item})
        assert saved["filename"] == "Earlier mail.eml" and saved["kind"] == "item"
        result, status = invoke(agents, ex, "mail_save_attachment", {"message_id": message_id, "attachment_id": link})
        assert status == "error" and result["kind"] == "unsupported" and "outlook.office365.com" in result["error"]

    def test_an_attachment_over_the_chat_limit_is_refused_before_download(self, agents, graph):
        ex, _ = chat(graph)
        message_id = graph.add_message("t-y", SUPPLIER, GraphStub.WORK, "Video", "x", "Tue, 08 Sep 2026 10:00:00 +0400")
        big = graph.add_attachment(message_id, "walkthrough.mp4", "video/mp4", b"x", size=9 * 1024 * 1024)
        result, status = invoke(agents, ex, "mail_save_attachment", {"message_id": message_id, "attachment_id": big})
        assert status == "error" and result["kind"] == "too_large" and "9.0 MB" in result["error"]
        assert not [r for r in graph.requests if r["path"].endswith("$value")]

    def test_find_searches_alone_and_waits_out_a_busy_service(self, agents, graph):
        """Seen live: search timed out at 20 s. A search has its own longer timeout and is tried again when busy."""
        from agents.outlook.tools.mail_tool import SEARCH_TIMEOUT
        assert SEARCH_TIMEOUT > 40
        ex, _ = chat(graph)
        graph.throttle(1, "1", where="search")
        result = ok(agents, ex, "mail_find", {"query": "from:northlight quotation"})
        assert [m["thread_id"] for m in result["messages"]] == ["t-quote"] and result["more"] is False
        searches = [r for r in graph.requests if "$search" in r["query"]]
        assert len(searches) == 2 and all("$filter" not in r["query"] for r in searches)

    def test_list_filters_unread_from_a_sender_newest_first(self, agents, graph):
        ex, _ = chat(graph)
        graph.add_message("t-z", CUSTOMER, GraphStub.WORK, "Read already", "x", "Thu, 10 Sep 2026 10:00:00 +0400",
                          read=True)
        graph.add_message("t-z", CUSTOMER, GraphStub.WORK, "Unread newer", "x", "Fri, 11 Sep 2026 10:00:00 +0400")
        result = ok(agents, ex, "mail_list", {"unread_only": True, "from": CUSTOMER.upper()})
        assert [m["subject"] for m in result["messages"]] == ["Unread newer", "New office — furniture for 20 desks"]

    def test_folders_are_listed_with_paths_in_any_script(self, agents, graph):
        ex, _ = chat(graph)
        clients = graph.add_folder("عملاء")
        graph.add_folder("Harbourline", clients)
        paths = {f["path"] for f in ok(agents, ex, "mail_folders")["folders"]}
        assert {"Inbox", "Archive", "عملاء", "عملاء/Harbourline"} <= paths

    def test_mail_is_marked_moved_and_archived(self, agents, graph):
        ex, _ = chat(graph)
        clients = graph.add_folder("Clients")
        target = graph.add_folder("Harbourline", clients)
        ids = list(graph.messages)
        ok(agents, ex, "mail_mark", {"message_ids": ids[:1], "read": True, "flag": "flagged"})
        assert graph.messages[ids[0]]["isRead"] and graph.messages[ids[0]]["flag"]["flagStatus"] == "flagged"
        moved = ok(agents, ex, "mail_move", {"message_ids": ids[1:], "folder": "Clients/Harbourline"})
        assert graph.messages[ids[1]]["parentFolderId"] == target and moved["folder"] == "Clients/Harbourline"
        ok(agents, ex, "mail_archive", {"message_ids": ids[:1]})
        assert graph.messages[ids[0]]["parentFolderId"] == "FOLDER-archive="
        result, status = invoke(agents, ex, "mail_move", {"message_ids": ids[:1], "folder": "Nowhere"})
        assert status == "error" and result["kind"] == "not_found" and "Clients/Harbourline" in result["error"]


class TestDrafting:
    def test_a_reply_keeps_the_quoted_history(self, agents, graph):
        """Seen: the reply text replaced the whole body, losing what it answered."""
        ex, provider = chat(graph)
        result = ok(agents, ex, "draft_reply", {"thread_id": "t-quote", "body": "We accept the quotation."})
        draft = graph.messages[result["outlook_draft_id"]]
        assert "We accept the quotation." in draft["body"]["content"]
        assert "Please find our quotation attached." in draft["body"]["content"]
        assert asked(graph, "POST", "createReply") and not asked(graph, "PATCH", "me/messages")
        assert result["to"] == SUPPLIER
        record = provider.data["draft"][result["draft_ref"]]
        assert record["keys"]["kind"] == "reply" and "subject" not in record["keys"]
        assert record["values"]["content"]["body"] == "We accept the quotation."

    def test_reply_all_reaches_everyone_but_the_account(self, agents, graph):
        ex, _ = chat(graph)
        result = ok(agents, ex, "draft_reply", {"thread_id": "t-office", "body": "Noted.", "all": True})
        assert result["to"] == CUSTOMER
        assert "facilities@harbourline.example" in result["cc"] and COLLEAGUE in result["cc"]
        assert GraphStub.WORK not in result["cc"]

    def test_a_reply_to_our_own_last_message_goes_to_whom_we_wrote(self, agents, graph):
        """Seen: "me" was the sign-in name, so mail sent from the account's other address was not its own."""
        ex, _ = chat(graph)
        graph.add_message("t-quote", GraphStub.WORK_ALIAS, SUPPLIER, "RE: Quotation", "Any update?",
                          "Fri, 04 Sep 2026 09:00:00 +0400", folder="sentitems")
        result = ok(agents, ex, "draft_reply", {"thread_id": "t-quote", "body": "Following up."})
        assert result["to"] == SUPPLIER

    def test_a_forward_carries_the_attachments(self, agents, graph):
        ex, _ = chat(graph)
        thread = ok(agents, ex, "mail_thread", {"thread_id": "t-quote"})
        result = ok(agents, ex, "draft_forward", {"message_id": thread["messages"][0]["message_id"],
                                                  "to": "finance@sidra.example", "body": "For approval."})
        names = [a["name"] for a in graph.attachments_of(result["outlook_draft_id"])]
        assert "Q-2041.pdf" in names and result["subject"].startswith("FW:")

    def test_chat_files_are_attached_small_whole_large_in_pieces(self, agents, graph):
        """Seen (Gmail, the same need here): files could not be attached at all."""
        ex, _ = chat(graph)
        small = ex.file("عرض السعر.pdf", b"%PDF small")
        large_bytes = bytes(range(256)) * (5 * 1024 * 1024 // 256)
        large = ex.file("drawings.pdf", large_bytes)
        result = ok(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Drawings", "body": "Attached.",
                                                  "files": [small, large]})
        assert [(a["filename"], a["how"]) for a in result["attached"]] == [("عرض السعر.pdf", "attached"),
                                                                            ("drawings.pdf", "uploaded")]
        kept = {a["name"]: a["raw"] for a in graph.attachments_of(result["outlook_draft_id"])}
        assert kept["drawings.pdf"] == large_bytes and kept["عرض السعر.pdf"] == b"%PDF small"
        puts = [r for r in graph.requests if r["method"] == "PUT"]
        assert len(puts) == 3 and all(r["auth"] is None and r["size"] <= FOUR_MB for r in puts)

    def test_files_are_attached_to_a_prepared_draft(self, agents, graph):
        ex, provider = chat(graph)
        drafted = ok(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Hi", "body": "Body."})
        ok(agents, ex, "draft_attach", {"draft_ref": drafted["draft_ref"], "files": [ex.file("a.txt", "hello")]})
        assert [a["name"] for a in graph.attachments_of(drafted["outlook_draft_id"])] == ["a.txt"]
        assert provider.data["draft"][drafted["draft_ref"]]["values"]["content"]["files"] == ["a.txt"]

    def test_sending_needs_approval_and_is_confirmed_by_outlook(self, agents, graph):
        ex, provider = chat(graph)
        drafted = ok(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Your office", "body": "We can."})
        refused, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=2)
        assert status == "error" and refused.get("denied") is True and graph.sends == []
        sent = ok(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]})
        assert sent["sent"] is True and graph.messages[sent["message_id"]]["isDraft"] is False
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["status"] == "sent"
        again, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]})
        assert status == "error" and "already sent" in again["error"]

    def test_an_unanswered_send_is_unknown_and_never_retried(self, agents, graph):
        ex, provider = chat(graph)
        drafted = ok(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Hello", "body": "Body."})
        graph.drop_send = True
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]})
        assert status == "error" and result["kind"] == "unknown"
        assert graph.sends == [drafted["outlook_draft_id"]]
        graph.drop_send = False
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]})
        assert status == "error" and result["kind"] == "unknown" and len(graph.sends) == 1

    def test_a_discarded_draft_is_gone(self, agents, graph):
        ex, _ = chat(graph)
        drafted = ok(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Hello", "body": "Body."})
        assert ok(agents, ex, "draft_discard", {"draft_ref": drafted["draft_ref"]}) == {"discarded": True}
        assert drafted["outlook_draft_id"] not in graph.messages


@pytest.fixture
def calendar(graph):
    graph.add_event("Supplier visit", "2026-11-22T06:00:00Z", "2026-11-22T07:00:00Z", attendees=[SUPPLIER])
    graph.add_event("National Day", "2026-12-02", "2026-12-04", all_day=True)
    graph.add_event("Weekly sync", "2026-11-23T05:00:00Z", "2026-11-23T05:30:00Z", attendees=[COLLEAGUE],
                    weekly_on=["Monday", "Wednesday"], until="2026-12-31")
    graph.add_event("Board review", "2026-11-24T10:00:00Z", "2026-11-24T11:00:00Z", organizer=COLLEAGUE,
                    attendees=[GraphStub.WORK])
    graph.add_event("Away", "2026-11-25", "2026-11-26", all_day=True, calendar="CAL-team=")
    return graph


class TestCalendar:
    def test_the_week_in_the_mailboxs_zone_series_expanded(self, agents, calendar):
        ex, _ = chat(calendar)
        result = ok(agents, ex, "calendar_list", {"start": "2026-11-22", "end": "2026-11-28"})
        assert result["timezone"] == "Asia/Dubai"
        by = {(e["subject"], e["start"]) for e in result["events"]}
        assert ("Supplier visit", "2026-11-22T10:00+04:00") in by
        syncs = [e for e in result["events"] if e["subject"] == "Weekly sync"]
        assert [e["start"][:10] for e in syncs] == ["2026-11-23", "2026-11-25"]
        assert all(e["recurring"] and e["series_id"] for e in syncs)
        assert "Away" not in {e["subject"] for e in result["events"]}

    def test_all_day_events_are_days_and_other_calendars_answer(self, agents, calendar):
        ex, _ = chat(calendar)
        result = ok(agents, ex, "calendar_list", {"start": "2026-12-01", "end": "2026-12-05"})
        day = next(e for e in result["events"] if e["subject"] == "National Day")
        assert (day["start"], day["end"], day["all_day"]) == ("2026-12-02", "2026-12-03", True)
        names = {c["name"] for c in ok(agents, ex, "calendar_calendars")["calendars"]}
        assert names == {"Calendar", "Team Holidays"}
        away = ok(agents, ex, "calendar_list", {"start": "2026-11-25", "calendar": "team holidays"})
        assert [e["subject"] for e in away["events"]] == ["Away"]

    def test_without_the_settings_permission_times_are_utc_and_said(self, agents, calendar):
        """Seen: every call read mailboxSettings, which needs a scope the account was never granted."""
        ex, _ = chat(calendar, token=calendar.issue("work", scopes={"Calendars.ReadWrite", "Mail.ReadWrite"}))
        result = ok(agents, ex, "calendar_list", {"start": "2026-11-22"})
        assert result["timezone"] == "UTC" and "MailboxSettings.Read" in result["timezone_note"]
        assert result["events"][0]["start"] == "2026-11-22T06:00+00:00"
        given = ok(agents, ex, "calendar_list", {"start": "2026-11-22", "timezone": "Arabian Standard Time"})
        assert given["timezone"] == "Asia/Dubai" and "timezone_note" not in given

    def test_an_event_whole_with_answers_and_its_repeat(self, agents, calendar):
        ex, _ = chat(calendar)
        week = ok(agents, ex, "calendar_list", {"start": "2026-11-23"})
        sync = next(e for e in week["events"] if e["subject"] == "Weekly sync")
        result = ok(agents, ex, "calendar_get", {"event_id": sync["event_id"]})
        assert result["repeats"] == "every week on Monday, Wednesday, from 2026-11-23, until 2026-12-31"
        assert result["attendee_list"][0]["email"] == COLLEAGUE and result["description"] == "Agenda"

    def test_find_times_on_a_work_account_uses_free_busy_and_the_mailboxs_week(self, agents, calendar):
        ex, provider = chat(calendar)
        calendar.colleagues[CUSTOMER] = [("2026-11-22T09:00:00Z", "2026-11-22T13:00:00Z", "busy")]
        result = ok(agents, ex, "calendar_find_times", {
            "duration_minutes": 60, "from_date": "2026-11-20", "to_date": "2026-11-22",
            "attendees": [CUSTOMER, "someone@elsewhere.example"], "not_before": "2026-11-01T00:00:00Z",
            "max_candidates": 4})
        assert result["source"] == "free/busy" and result["working_days"][0] == "Monday"
        assert [u["email"] for u in result["unchecked"]] == ["someone@elsewhere.example"]
        # Friday and Saturday are not this mailbox's working days; Sunday is.
        assert [c["start"] for c in result["candidates"]] == ["2026-11-22T09:00+04:00", "2026-11-22T11:00+04:00"]
        assert len(provider.data["proposal"]) == len(result["candidates"])

    def test_find_times_on_a_personal_account_reads_its_own_calendar(self, agents, calendar):
        """getSchedule does not exist for personal accounts: the account's own calendar instead."""
        ex, _ = chat(calendar, "personal")
        result = ok(agents, ex, "calendar_find_times", {
            "duration_minutes": 60, "from_date": "2026-11-22", "to_date": "2026-11-22", "attendees": [CUSTOMER],
            "working_days": ["sunday"], "not_before": "2026-11-01T00:00:00Z"})
        assert result["source"] == "own calendar" and not asked(calendar, "POST", "getSchedule")
        assert "work or school" in result["unchecked"][0]["reason"]
        assert all(not c["start"].startswith("2026-11-22T10:00") for c in result["candidates"])

    def test_an_own_event_and_an_all_day_one(self, agents, calendar):
        ex, _ = chat(calendar)
        made = ok(agents, ex, "calendar_create", {"subject": "Focus", "start": "2026-11-26T09:00",
                                                  "duration_minutes": 90}, chat_level=2)
        assert (made["start"], made["end"]) == ("2026-11-26T09:00+04:00", "2026-11-26T10:30+04:00")
        assert calendar.events[made["event_id"]]["transactionId"]
        leave = ok(agents, ex, "calendar_create", {"subject": "Leave", "start": "2026-12-07", "end": "2026-12-08"},
                   chat_level=2)
        assert (leave["start"], leave["end"], leave["all_day"]) == ("2026-12-07", "2026-12-08", True)

    def test_an_invitation_from_a_proposal_once(self, agents, calendar):
        ex, _ = chat(calendar)
        calendar.colleagues[COLLEAGUE] = []
        found = ok(agents, ex, "calendar_find_times", {
            "duration_minutes": 30, "from_date": "2026-11-29", "to_date": "2026-11-29", "attendees": [COLLEAGUE],
            "not_before": "2026-11-01T00:00:00Z"})
        proposal = found["candidates"][0]["proposal_ref"]
        refused, status = invoke(agents, ex, "calendar_invite", {"subject": "Chairs", "proposal_ref": proposal},
                                 chat_level=2)
        assert status == "error" and refused.get("denied")
        made = ok(agents, ex, "calendar_invite", {"subject": "Chairs", "proposal_ref": proposal, "teams": True})
        assert made["invited"] == [COLLEAGUE] and made["online_meeting_url"].startswith("https://teams.")
        again, status = invoke(agents, ex, "calendar_invite", {"subject": "Chairs", "proposal_ref": proposal})
        assert status == "error" and "already booked" in again["error"]

    def test_a_teams_link_on_a_personal_account_is_refused_before_writing(self, agents, calendar):
        ex, _ = chat(calendar, "personal")
        before = len(calendar.events)
        result, status = invoke(agents, ex, "calendar_invite", {
            "subject": "Call", "start": "2026-11-29T10:00", "duration_minutes": 30, "attendees": [CUSTOMER],
            "teams": True})
        assert status == "error" and result["kind"] == "unsupported" and "work or school" in result["error"]
        assert len(calendar.events) == before

    def test_a_new_start_moves_the_end_with_it(self, agents, calendar):
        ex, _ = chat(calendar)
        visit = next(k for k, e in calendar.events.items() if e["subject"] == "Supplier visit")
        moved = ok(agents, ex, "calendar_update", {"event_id": visit, "start": "2026-11-22T15:00"})
        assert (moved["start"], moved["end"]) == ("2026-11-22T15:00+04:00", "2026-11-22T16:00+04:00")

    def test_only_the_organizer_changes_or_cancels_others_decline(self, agents, calendar):
        """Seen: cancel deleted without checking, so the attendees were never told."""
        ex, _ = chat(calendar)
        board = next(k for k, e in calendar.events.items() if e["subject"] == "Board review")
        result, status = invoke(agents, ex, "calendar_update", {"event_id": board, "start": "2026-11-24T16:00"})
        assert status == "error" and COLLEAGUE in result["error"]
        declined = ok(agents, ex, "calendar_cancel", {"event_id": board, "comment": "Travelling."})
        assert declined["declined"] and not declined["cancelled"] and board in calendar.events
        assert calendar.responses[-1]["action"] == "decline" and calendar.responses[-1]["sendResponse"] is True
        visit = next(k for k, e in calendar.events.items() if e["subject"] == "Supplier visit")
        cancelled = ok(agents, ex, "calendar_cancel", {"event_id": visit})
        assert cancelled["cancelled"] and calendar.cancellations == [visit]

    def test_an_invitation_is_answered_but_not_ones_own(self, agents, calendar):
        ex, _ = chat(calendar)
        board = next(k for k, e in calendar.events.items() if e["subject"] == "Board review")
        assert ok(agents, ex, "calendar_respond", {"event_id": board, "response": "tentative"})["response"] == \
            "tentative"
        visit = next(k for k, e in calendar.events.items() if e["subject"] == "Supplier visit")
        result, status = invoke(agents, ex, "calendar_respond", {"event_id": visit, "response": "accept"})
        assert status == "error" and "organize" in result["error"]


class TestTasks:
    def test_open_tasks_by_due_date_without_an_order_graph_refuses(self, agents, graph):
        ex, _ = chat(graph)
        graph.add_task("Later", due={"dateTime": "2026-12-01T00:00:00.0000000", "timeZone": "UTC"})
        # To Do keeps a day set in Dubai as that day's midnight there, in UTC: the evening before.
        graph.add_task("Sooner", due={"dateTime": "2026-11-24T20:00:00.0000000", "timeZone": "UTC"})
        graph.add_task("Done", status="completed")
        result = ok(agents, ex, "tasks_list")
        assert [(t["title"], t.get("due")) for t in result["tasks"]] == [("Sooner", "2026-11-25"),
                                                                         ("Later", "2026-12-01")]
        listed = [r for r in graph.requests if r["path"].endswith("/tasks") and r["method"] == "GET"]
        assert listed and all("$orderby" not in r["query"] for r in listed)

    def test_a_due_date_is_written_in_the_persons_zone(self, agents, graph):
        """Seen: due dates written as midnight UTC showed a day off."""
        ex, _ = chat(graph)
        made = ok(agents, ex, "tasks_create", {"title": "Send the chair quotation", "due": "2026-11-26",
                                               "importance": "high"}, chat_level=2)
        task = graph.tasks[made["task_id"]]
        assert task["dueDateTime"] == {"dateTime": "2026-11-26T00:00:00", "timeZone": "Arabian Standard Time"}
        assert made["due"] == "2026-11-26"

    def test_a_completion_is_dated_by_the_persons_day(self, agents, graph):
        """Seen: [:10] of a UTC time put a completion at 02:30 in Dubai on the day before."""
        ex, _ = chat(graph)
        graph.add_task("Late night", status="completed",
                       completed={"dateTime": "2026-11-19T22:30:00.0000000", "timeZone": "UTC"})
        [row] = ok(agents, ex, "tasks_list", {"status": "completed"})["tasks"]
        assert row["completed"] == "2026-11-20"

    def test_tasks_are_completed_reopened_and_deleted_on_approval(self, agents, graph):
        ex, _ = chat(graph)
        task_id = graph.add_task("Order chairs")
        assert ok(agents, ex, "tasks_complete", {"task_id": task_id}, chat_level=2)["status"] == "completed"
        assert ok(agents, ex, "tasks_reopen", {"task_id": task_id}, chat_level=2)["status"] == "notStarted"
        refused, status = invoke(agents, ex, "tasks_delete", {"task_id": task_id}, chat_level=2)
        assert status == "error" and refused.get("denied")
        assert ok(agents, ex, "tasks_delete", {"task_id": task_id})["deleted"] and task_id not in graph.tasks

    def test_lists_are_named_and_made(self, agents, graph):
        ex, _ = chat(graph)
        made = ok(agents, ex, "tasks_create_list", {"name": "مكتب جديد"})
        ok(agents, ex, "tasks_create", {"list": "مكتب جديد", "title": "Desks"})
        assert [t["title"] for t in ok(agents, ex, "tasks_list", {"list": made["list_id"]})["tasks"]] == ["Desks"]
        result, status = invoke(agents, ex, "tasks_list", {"list": "Nope"})
        assert status == "error" and "Tasks" in result["error"]


class TestWatches:
    def test_a_reply_watch_counts_every_address_of_the_account_as_its_own(self, agents, graph):
        ex, _ = chat(graph)
        watch = ok(agents, ex, "watch_reply", {"thread_id": "t-quote", "note": "chase Thursday"})
        graph.add_message("t-quote", GraphStub.WORK_ALIAS, SUPPLIER, "RE: Quotation", "Any update?",
                          "Fri, 04 Sep 2030 09:00:00 +0400", folder="sentitems")
        result = ok(agents, ex, "watch_check")
        assert [w["watch_ref"] for w in result["unanswered"]] == [watch["watch_ref"]] and not result["answered"]
        # The message the watch began at is deleted; a reply still counts.
        first = graph.conversation("t-quote")[0]["id"]
        del graph.messages[first]
        graph.add_message("t-quote", SUPPLIER, GraphStub.WORK, "RE: Quotation", "Yes, confirmed.",
                          "Sat, 05 Sep 2030 09:00:00 +0400")
        result = ok(agents, ex, "watch_check")
        assert result["answered"][0]["reply_from"].endswith(SUPPLIER + ">") or \
            result["answered"][0]["reply_from"] == SUPPLIER
        assert result["closed"][0]["watch_ref"] == watch["watch_ref"]
        assert ok(agents, ex, "watch_check") == {**ok(agents, ex, "watch_check"), "checked": 0}

    def test_a_gone_conversation_closes_its_watch(self, agents, graph):
        ex, provider = chat(graph)
        watch = ok(agents, ex, "watch_reply", {"thread_id": "t-office"})
        for key in [k for k, m in graph.messages.items() if m["conversationId"] == "t-office"]:
            del graph.messages[key]
        result = ok(agents, ex, "watch_check")
        assert result["closed"][0]["watch_ref"] == watch["watch_ref"]
        assert provider.data["watch"][watch["watch_ref"]]["keys"]["status"] == "closed"

    def test_the_inbox_watch_hands_on_only_new_mail_not_ours(self, agents, graph):
        ex, _ = chat(graph)
        ok(agents, ex, "watch_inbox", {"skip_from": "@newsletter.example"})
        graph.add_message("t-n1", CUSTOMER, GraphStub.WORK, "Desks", "x", "Mon, 01 Jan 2035 10:00:00 +0000")
        graph.add_message("t-n2", GraphStub.WORK_ALIAS, GraphStub.WORK, "Note to self", "x",
                          "Mon, 01 Jan 2035 10:00:00 +0000")
        graph.add_message("t-n3", "news@newsletter.example", GraphStub.WORK, "Offers", "x",
                          "Mon, 01 Jan 2035 10:01:00 +0000")
        first = ok(agents, ex, "watch_new_mail")
        assert [m["subject"] for m in first["messages"]] == ["Desks"]
        graph.add_message("t-n4", CUSTOMER, GraphStub.WORK, "Chairs too", "x", "Mon, 01 Jan 2035 10:01:00 +0000")
        assert [m["subject"] for m in ok(agents, ex, "watch_new_mail")["messages"]] == ["Chairs too"]
        assert ok(agents, ex, "watch_new_mail")["messages"] == []

    def test_one_failing_watch_never_loses_anothers_mail(self, agents, graph):
        provider = Records(secrets={"microsoft": [
            {"ref": "sec_w", **graph.secret("work")},
            {"ref": "sec_x", **graph.secret("work", token="revoked", account="other@sidra.example")}]})
        ex = Chat(provider)
        ok(agents, ex, "watch_inbox", {"account": GraphStub.WORK})
        provider.create_data("inbox", {"since": "2000-01-01T00:00:00Z", "status": "active",
                                       "account": "other@sidra.example", "folder": "inbox"})
        graph.add_message("t-n1", CUSTOMER, GraphStub.WORK, "Desks", "x", "Mon, 01 Jan 2035 10:00:00 +0000")
        result = ok(agents, ex, "watch_new_mail")
        assert [m["subject"] for m in result["messages"]] == ["Desks"]
        assert [p["kind"] for p in result["problems"]] == ["auth"]
        broken = next(r for r in provider.data["inbox"].values() if r["keys"]["account"] == "other@sidra.example")
        assert broken["keys"]["since"] == "2000-01-01T00:00:00Z"

    def test_a_deleted_folder_closes_its_watch(self, agents, graph):
        ex, provider = chat(graph)
        folder = graph.add_folder("Quotes")
        watch = ok(agents, ex, "watch_inbox", {"folder": "Quotes"})
        del graph.folders[folder]
        result = ok(agents, ex, "watch_new_mail")
        assert result["closed"][0]["watch_ref"] == watch["watch_ref"]

    def test_a_completion_is_handed_on_once_and_an_edit_is_not_another(self, agents, graph):
        """Seen: the watch read lastModified, so an edit to a done task reported it done again."""
        ex, _ = chat(graph)
        task_id = graph.add_task("Order chairs")
        ok(agents, ex, "watch_tasks")
        ok(agents, ex, "tasks_complete", {"task_id": task_id}, chat_level=2)
        first = ok(agents, ex, "watch_task_changes")
        assert [(c["title"], c["change"]) for c in first["changes"]] == [("Order chairs", "completed")]
        assert first["changes"][0]["completed"] == datetime.now(timezone(timedelta(hours=4))).date().isoformat()
        ok(agents, ex, "tasks_update", {"task_id": task_id, "title": "Order chairs (20)"}, chat_level=2)
        assert ok(agents, ex, "watch_task_changes")["changes"] == []

    def test_report_all_and_an_expired_feed_starts_again(self, agents, graph):
        ex, _ = chat(graph)
        office = graph.add_list("Office move")
        ok(agents, ex, "watch_tasks", {"list": "Office move", "report": "all"})
        added = graph.add_task("Book movers", office)
        changes = ok(agents, ex, "watch_task_changes")["changes"]
        assert [(c["title"], c["change"]) for c in changes] == [("Book movers", "added")]
        graph.delta_expired = True
        result = ok(agents, ex, "watch_task_changes")
        assert result["problems"] == [] and result["changes"] == []
        graph.delta_expired = False
        del graph.tasks[added]
        graph.sequence += 1
        graph.removed.append({"id": added, "list": office, "seq": graph.sequence})
        assert [c["change"] for c in ok(agents, ex, "watch_task_changes")["changes"]] == ["removed"]

    def test_watches_are_listed_and_closed(self, agents, graph):
        ex, _ = chat(graph)
        reply = ok(agents, ex, "watch_reply", {"thread_id": "t-quote"})
        ok(agents, ex, "watch_inbox")
        ok(agents, ex, "watch_tasks")
        kinds = sorted(w["kind"] for w in ok(agents, ex, "watches_list")["watches"])
        assert kinds == ["inbox", "reply", "tasks"]
        closed = ok(agents, ex, "watches_close", {"watch_ref": reply["watch_ref"]})
        assert closed["closed"] and closed["kind"] == "reply"
        assert len(ok(agents, ex, "watches_list")["watches"]) == 2
        assert ok(agents, ex, "watch_check")["checked"] == 0
        result, status = invoke(agents, ex, "watches_close", {"watch_ref": "data_999"})
        assert status == "error" and result["kind"] == "not_found"


class TestBusyService:
    def test_a_429_is_waited_out_as_microsoft_asks(self, agents, graph):
        ex, _ = chat(graph)
        graph.throttle(2, "1")
        assert ok(agents, ex, "tasks_lists")["lists"]
        assert sum(1 for r in graph.requests if r["path"].endswith("todo/lists")) == 3

    def test_a_page_token_from_elsewhere_is_refused_before_it_is_sent(self, agents, graph):
        ex, _ = chat(graph)
        result, status = invoke(agents, ex, "mail_find", {"query": "x", "page_token": "https://evil.example/next"})
        assert status == "error" and "page token" in result["error"]
        assert not any("evil" in r["path"] for r in graph.requests)
