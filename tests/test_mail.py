"""The Mail agent's program, spoken to in the standard's messages, against a loopback mail server holding fictional
mail for Sidra Office Supplies, reached at the servers its account names with their ports.
"""

import asyncio
import email
import email.policy
from datetime import datetime, timedelta, timezone

import pytest
import yaml


from tests.harness import Chat, Records, probe, requirements, run, way_out
from tests.conftest import ROOT, declaring
from tests.mail_stub import MailStub

ME = MailStub.ACCOUNT
SUPPLIER = "quotes@northlight-seating.example"
CUSTOMER = "dana@harbourline.example"
QUOTE_PDF = b"%PDF-1.4 fictional quotation " + bytes(range(256))


def fill(stub):
    """Two conversations, in the order they arrived: a customer's
    request nobody has answered; and a quotation with its attachment,
    which Sara answered and the supplier answered back."""
    stub.add(
        "INBOX", f"Dana Haddad <{CUSTOMER}>",
        "New office — furniture for 20 desks",
        "We are moving in November and need desks and chairs for 20 people.",
        date="2026-09-02T15:40:00+04:00", message_id="office@harbourline.example")
    quote = stub.add(
        "INBOX", f"Northlight Seating <{SUPPLIER}>",
        "Quotation Q-2041 — 20 office chairs",
        "Please find our quotation attached. Delivery in 10 working days.",
        date="2026-09-03T10:12:00+04:00", message_id="q2041@northlight.example",
        attachments=[("Q-2041.pdf", "application/pdf", QUOTE_PDF)], seen=True)
    asked = stub.add(
        "Sent Items", ME, "Re: Quotation Q-2041 — 20 office chairs",
        "Thank you. Can you deliver in 7 working days instead?",
        date="2026-09-03T11:30:00+04:00", to=SUPPLIER,
        message_id="asked@sidra.example", in_reply_to=quote, seen=True)
    stub.add(
        "INBOX", f"Northlight Seating <{SUPPLIER}>",
        "Re: Quotation Q-2041 — 20 office chairs",
        "Seven working days is possible for an order placed this week.",
        date="2026-09-04T09:05:00+04:00", message_id="seven@northlight.example",
        in_reply_to=asked, references=f"{quote} {asked}")
    return stub


@pytest.fixture
def mail():
    """The stub's servers, under the names its credential gives out,
    each with its port: the way out opens what the account names."""
    stub = fill(MailStub().start())
    yield stub
    stub.stop()


def executor(stub, password=""):
    provider = Records(
        secrets={"account": stub.secret(password)})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs=None, chat_level=1):
    return run(ex.invoke(agents["mail"], name, inputs or {},
                         chat_level=chat_level))


def subjects(result):
    return [row["subject"] for row in result["messages"]]


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["mail"]
        assert probe(agent) == []

    def test_it_needs_only_requests_and_declares_every_server_with_its_port(self):
        manifest = yaml.safe_load(
            (ROOT / "mail" / "manifest.yaml").read_text(encoding="utf-8"))
        # standard.watch (and its HttpError) import requests: the one package carried, pinned.
        assert [r.split("==")[0] for r in requirements(ROOT / "mail")] == ["certifi", "charset-normalizer", "idna",
                                                                         "requests", "urllib3"]
        hosts = manifest["network"]["hosts"]
        named = [host for host in hosts if isinstance(host, str)]
        assert all(host.rsplit(":", 1)[1] in ("993", "465", "587") for host in named)
        # The standard's from_secret carries no port: the account's field does.
        assert [host for host in hosts if not isinstance(host, str)] == [
            {"from_secret": "account.imap_host"}, {"from_secret": "account.smtp_host"}]

    def test_the_servers_the_code_knows_are_the_ones_the_manifest_declares(self):
        """A provider known by name and not declared would be refused
        where agents are confined; one declared and not known is a
        host nobody asked for."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mail_servers", ROOT / "mail" / "tools" / "servers.py")
        servers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(servers)
        known = set()
        for imap, smtp, ports in servers.PROVIDERS.values():
            known.add(f"{imap}:{servers.IMAP_PORT}")
            known.update(f"{smtp}:{port}" for port in ports)
        manifest = yaml.safe_load(
            (ROOT / "mail" / "manifest.yaml").read_text(encoding="utf-8"))
        assert {host for host in manifest["network"]["hosts"]
                if isinstance(host, str)} == known

    def test_a_connection_encrypted_after_the_greeting_checks_the_servers_name(self):
        """STARTTLS checks the certificate against the name the
        connection was made for, which it has to be told."""
        import socket

        from agents.mail.tools.sender import _Connection

        here, there = socket.socketpair()
        try:
            connection = _Connection(here, "smtp.mail.me.com")
            assert connection._host == "smtp.mail.me.com"
            assert connection._get_socket("ignored", 0, 5) is here
        finally:
            here.close()
            there.close()

    def test_sending_is_the_one_external_act(self, agents):
        levels = {name: agents["mail"].function(name)["level"]
                  for name in ("search_find", "search_thread", "draft_reply",
                               "draft_send", "search_save_attachment")}
        assert levels == {"search_find": 0, "search_thread": 0, "draft_reply": 1,
                          "draft_send": 3, "search_save_attachment": 0}


class TestTheAccount:
    def test_it_reports_its_address(self, agents, mail):
        ex, _ = executor(mail)
        assert invoke(agents, ex, "account_status") == (
            {"connected": True, "email": ME}, "success")

    def test_a_password_the_server_refuses_says_what_to_make(self, agents, mail):
        ex, _ = executor(mail, password="the-accounts-own-password")
        result, status = invoke(agents, ex, "account_status")
        assert status == "success" and result["connected"] is False
        assert "refused the sign-in" in result["problem"]
        assert "app password" in result["problem"]
        assert "the-accounts-own-password" not in result["problem"]

    def test_a_provider_known_by_name_is_told_where(self, agents, mail):
        provider = Records(secrets={"account": {
            **mail.secret("wrong"), "provider": "gmail"}})
        result, _ = invoke(agents, Chat(provider),
                           "account_status")
        assert "myaccount.google.com/apppasswords" in result["problem"]

    def test_an_account_that_names_no_server(self, agents):
        provider = Records(secrets={"account": {
            "account": ME, "password": "x", "provider": "other"}})
        result, _ = invoke(agents, Chat(provider),
                           "account_status")
        assert result["connected"] is False
        assert "names no mail server" in result["problem"]

    def test_the_accounts_are_listed_without_a_password(self, agents, mail):
        provider = Records(secrets={"account": [
            {"ref": "sec_first", **mail.secret()},
            {"ref": "sec_second", **mail.secret(), "account": "work@sidra.example", "provider": "fastmail"},
        ]})
        ex = Chat(provider)
        result, status = invoke(agents, ex, "account_list")
        assert status == "success", result
        # Of several, none answers unnamed: none is the default.
        assert [(a["account"], a["provider"], a["default"])
                for a in result["accounts"]] == [
            (ME, "other", False), ("work@sidra.example", "fastmail", False)]
        assert MailStub.PASSWORD not in str(result)
        # Named wrongly: told which are connected.
        result, status = invoke(agents, ex, "search_find",
                                {"account": "nobody@x.example"})
        assert status == "error" and "work@sidra.example" in result["error"]
        # Named by a part of its address, it answers.
        found, status = invoke(agents, ex, "account_status", {"account": "work"})
        assert status == "success" and found["email"] == "work@sidra.example"


class TestFinding:
    def test_nothing_asked_for_is_the_newest_mail(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search_find")
        assert status == "success", result
        assert result["folder"] == "INBOX" and result["total_estimate"] == 3
        assert subjects(result) == [
            "Re: Quotation Q-2041 — 20 office chairs",
            "Quotation Q-2041 — 20 office chairs",
            "New office — furniture for 20 desks"]
        newest = result["messages"][0]
        assert newest["from"] == f"Northlight Seating <{SUPPLIER}>"
        assert newest["message_id"] == "seven@northlight.example"
        assert newest["thread_id"] == "q2041@northlight.example"
        assert newest["date"] == "2026-09-04T09:05:00+04:00"
        assert newest["snippet"].startswith("Seven working days is possible")
        assert newest["unread"] is True
        assert result["messages"][1]["unread"] is False

    def test_by_sender_subject_and_words(self, agents, mail):
        ex, _ = executor(mail)
        assert subjects(invoke(agents, ex, "search_find",
                               {"from": "harbourline"})[0]) == [
            "New office — furniture for 20 desks"]
        assert len(invoke(agents, ex, "search_find",
                          {"subject": "Q-2041"})[0]["messages"]) == 2
        found, _ = invoke(agents, ex, "search_find",
                          {"query": "moving in November"})
        assert [row["message_id"] for row in found["messages"]] == [
            "office@harbourline.example"]
        assert invoke(agents, ex, "search_find",
                      {"from": "harbourline", "subject": "Q-2041"})[0]["messages"] == []

    def test_by_date_and_unread(self, agents, mail):
        ex, _ = executor(mail)
        assert subjects(invoke(agents, ex, "search_find",
                               {"since": "2026-09-04"})[0]) == [
            "Re: Quotation Q-2041 — 20 office chairs"]
        assert subjects(invoke(agents, ex, "search_find",
                               {"before": "2026-09-03T00:00:00+04:00"})[0]) == [
            "New office — furniture for 20 desks"]
        assert len(invoke(agents, ex, "search_find",
                          {"unread_only": True})[0]["messages"]) == 2
        result, status = invoke(agents, ex, "search_find", {"since": "last week"})
        assert status == "error" and "is not a date" in result["error"]

    def test_words_in_another_alphabet(self, agents, mail):
        mail.add("INBOX", "مكتب النور <office@alnoor.example>", "عرض سعر الكراسي",
                 "نرفق لكم عرض السعر المطلوب.", date="2026-09-05T08:00:00+04:00",
                 message_id="arabic@alnoor.example")
        ex, _ = executor(mail)
        by_subject, status = invoke(agents, ex, "search_find", {"subject": "الكراسي"})
        assert status == "success", by_subject
        assert [row["message_id"] for row in by_subject["messages"]] == [
            "arabic@alnoor.example"]
        assert by_subject["messages"][0]["from"] == "مكتب النور <office@alnoor.example>"
        by_words, status = invoke(agents, ex, "search_find", {"query": "عرض السعر"})
        assert status == "success", by_words
        assert [row["message_id"] for row in by_words["messages"]] == [
            "arabic@alnoor.example"]

    def test_what_was_sent_is_in_its_own_folder(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search_find", {"folder": "sent"})
        assert status == "success", result
        assert result["folder"] == "Sent Items"
        assert [row["message_id"] for row in result["messages"]] == [
            "asked@sidra.example"]

    def test_a_long_result_continues(self, agents, mail):
        ex, _ = executor(mail)
        first, _ = invoke(agents, ex, "search_find", {"max_results": 2})
        assert len(first["messages"]) == 2 and first["next_page_token"] == "2"
        rest, _ = invoke(agents, ex, "search_find",
                         {"max_results": 2, "page_token": first["next_page_token"]})
        assert subjects(rest) == ["New office — furniture for 20 desks"]
        assert "next_page_token" not in rest

    def test_an_account_with_an_archive_is_searched_there(self, agents):
        stub = fill(MailStub(archive=True).start())
        try:
            ex, _ = executor(stub)
            result, status = invoke(agents, ex, "search_find", {"subject": "Q-2041"})
            assert status == "success", result
            assert result["folder"] == "[Mail]/All Mail"
            # What Sara sent is among it: the archive holds everything.
            assert len(result["messages"]) == 3
        finally:
            stub.stop()


class TestAConversation:
    def test_it_is_read_whole_from_the_inbox_and_what_was_sent(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search_thread",
                                {"thread_id": "q2041@northlight.example"})
        assert status == "success", result
        assert result["thread_id"] == "q2041@northlight.example"
        assert result["subject"] == "Quotation Q-2041 — 20 office chairs"
        assert [(m["message_id"], m["from"]) for m in result["messages"]] == [
            ("q2041@northlight.example", f"Northlight Seating <{SUPPLIER}>"),
            ("asked@sidra.example", ME),
            ("seven@northlight.example", f"Northlight Seating <{SUPPLIER}>")]
        assert result["messages"][1]["body"] == (
            "Thank you. Can you deliver in 7 working days instead?")
        assert result["attachments"] == [{
            "message_id": "q2041@northlight.example", "attachment_id": "a1",
            "filename": "Q-2041.pdf", "mime_type": "application/pdf",
            "size": len(QUOTE_PDF)}]

    def test_any_message_of_it_finds_it(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search_thread",
                                {"thread_id": "seven@northlight.example"})
        assert status == "success", result
        assert result["thread_id"] == "q2041@northlight.example"
        assert len(result["messages"]) == 3

    def test_a_long_body_is_cut_and_said_to_be(self, agents, mail):
        mail.add("INBOX", CUSTOMER, "Terms", "term " * 2000,
                 message_id="terms@harbourline.example")
        ex, _ = executor(mail)
        result, _ = invoke(agents, ex, "search_thread", {
            "thread_id": "terms@harbourline.example", "max_body_chars": 200})
        [message] = result["messages"]
        assert len(message["body"]) == 200 and message["truncated"] is True

    def test_a_message_written_only_in_html_is_read_as_its_words(self, agents, mail):
        mail.add("INBOX", CUSTOMER, "Floor plan", "",
                 html="<html><head><style>p{color:red}</style></head><body>"
                      "<p>The plan has <b>20</b> desks.</p><p>Two rooms.</p>"
                      "<script>track()</script></body></html>",
                 message_id="plan@harbourline.example")
        ex, _ = executor(mail)
        result, _ = invoke(agents, ex, "search_thread",
                           {"thread_id": "plan@harbourline.example"})
        assert result["messages"][0]["body"] == "The plan has 20 desks.\nTwo rooms."

    def test_a_conversation_nobody_has(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search_thread",
                                {"thread_id": "nothing@nowhere.example"})
        assert status == "error" and result["kind"] == "not_found"

    def test_an_attachment_is_handed_to_the_chat_as_a_file(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": "q2041@northlight.example", "attachment_id": "a1",
            "filename": "Q-2041.pdf"}, chat_level=2)
        assert status == "success", result
        assert result["size"] == len(QUOTE_PDF)
        assert ex.files[result["file_ref"]] == {"name": "Q-2041.pdf", "content": QUOTE_PDF}

    def test_an_attachment_the_message_does_not_have(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": "q2041@northlight.example", "attachment_id": "a2",
            "filename": "x.pdf"}, chat_level=2)
        assert status == "error" and result["kind"] == "not_found"
        assert not ex.files


class TestDrafts:
    def drafted(self, agents, ex, **extra):
        result, status = invoke(agents, ex, "draft_reply", {
            "thread_id": "q2041@northlight.example",
            "body": "Please go ahead with seven working days.", **extra})
        assert status == "success", result
        return result

    def test_a_reply_goes_to_the_last_person_who_wrote_who_is_not_us(
            self, agents, mail):
        ex, provider = executor(mail)
        result = self.drafted(agents, ex)
        assert result["to"] == SUPPLIER
        assert result["subject"] == "Re: Quotation Q-2041 — 20 office chairs"
        record = provider.data["draft"][result["draft_ref"]]
        keys, values = record["keys"], record["values"]
        assert keys["status"] == "prepared" and keys["kind"] == "reply"
        assert keys["in_reply_to"] == "seven@northlight.example"
        # The words and the recipients are kept encrypted, never in plain keys.
        assert values["body"] == "Please go ahead with seven working days."
        assert values["to"] == SUPPLIER and not {"body", "to", "cc", "subject"} & set(keys)
        assert mail.sent == []

    def test_a_new_message_is_prepared_and_nothing_is_sent(self, agents, mail):
        ex, provider = executor(mail)
        result, status = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "Your furniture",
            "body": "We can furnish twenty desks by November."})
        assert status == "success", result
        assert provider.data["draft"][result["draft_ref"]]["keys"]["kind"] == "new"
        assert mail.sent == []

    def test_sending_hands_the_server_what_the_person_was_shown(self, agents, mail):
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex, cc="sales@sidra.example")
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success", result
        assert (result["sent"], result["status"]) == (True, "sent")
        assert result["thread_id"] == "q2041@northlight.example"

        [(sender, recipients, raw)] = mail.sent
        assert recipients == [SUPPLIER, "sales@sidra.example"]
        message = email.message_from_bytes(raw)
        assert message["From"] == ME and message["To"] == SUPPLIER
        assert message["In-Reply-To"] == "<seven@northlight.example>"
        assert message["References"].split() == [
            "<q2041@northlight.example>", "<asked@sidra.example>",
            "<seven@northlight.example>"]
        assert message["Message-ID"] == f"<{result['message_id']}>"
        assert message.get_payload().strip() == "Please go ahead with seven working days."

        keys = provider.data["draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "sent"
        assert keys["sent_message_id"] == result["message_id"]

    def test_a_copy_is_kept_where_the_server_kept_none(self, agents, mail):
        ex, _ = executor(mail)
        drafted = self.drafted(agents, ex)
        before = len(mail.folders["Sent Items"].messages)
        result, _ = invoke(agents, ex, "draft_send",
                           {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert result["kept_in_sent"] is True
        kept = mail.folders["Sent Items"].messages
        assert len(kept) == before + 1
        assert kept[-1].message["Message-ID"] == f"<{result['message_id']}>"
        assert "\\Seen" in kept[-1].flags
        # And the conversation now ends with it.
        thread, _ = invoke(agents, ex, "search_thread",
                           {"thread_id": "q2041@northlight.example"})
        assert thread["messages"][-1]["message_id"] == result["message_id"]

    def test_a_server_that_keeps_what_it_sends_is_not_given_a_second_copy(
            self, agents, mail):
        mail.keeps_sent = True
        ex, _ = executor(mail)
        drafted = self.drafted(agents, ex)
        before = len(mail.folders["Sent Items"].messages)
        result, _ = invoke(agents, ex, "draft_send",
                           {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert result["kept_in_sent"] is True
        assert len(mail.folders["Sent Items"].messages) == before + 1

    def test_a_recipient_the_server_refuses_sends_nothing(self, agents, mail):
        mail.refused_recipients.add(SUPPLIER)
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "server"
        assert SUPPLIER in result["error"] and "Nothing was sent" in result["error"]
        assert mail.sent == []
        keys = provider.data["draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "prepared"

    def test_an_answer_that_never_comes_is_unknown_and_not_tried_again(
            self, agents, mail):
        mail.never_answers = True
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success", result
        assert (result["sent"], result["status"]) == (False, "unknown")
        assert "is not known" in result["problem"]
        assert len(mail.sent) == 1
        keys = provider.data["draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "unknown"
        # Asked again, it is not sent again.
        mail.never_answers = False
        again, status = invoke(agents, ex, "draft_send",
                               {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and "not sent again" in again["error"]
        assert len(mail.sent) == 1

    def test_a_password_the_server_refuses_sends_nothing(self, agents, mail):
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        # The password was changed at the provider since the draft was made.
        mail.PASSWORD = "app-pass-2"
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert "app password" in result["error"]
        assert mail.sent == []
        keys = provider.data["draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "prepared"

    def test_a_draft_is_discarded_and_never_sent(self, agents, mail):
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        assert invoke(agents, ex, "draft_discard",
                      {"draft_ref": drafted["draft_ref"]}) == (
            {"discarded": True}, "success")
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and "discarded" in result["error"]
        assert mail.sent == []

    def test_sending_needs_the_persons_go_ahead(self, agents, mail):
        ex, _ = executor(mail)
        drafted = self.drafted(agents, ex)
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=2)
        assert status != "success", result
        assert mail.sent == []


class TestWaitingForAReply:
    def test_a_conversation_is_unanswered_until_somebody_else_writes(
            self, agents, mail):
        ex, provider = executor(mail)
        watch, status = invoke(agents, ex, "watch_await_reply", {
            "thread_id": "office@harbourline.example", "note": "desks for Dana"})
        assert status == "success", watch
        assert watch["since_message_id"] == "office@harbourline.example"

        checked, _ = invoke(agents, ex, "watch_check")
        assert checked["answered"] == []
        assert checked["unanswered"] == [{
            "watch_ref": watch["watch_ref"],
            "thread_id": "office@harbourline.example",
            "subject": "New office — furniture for 20 desks",
            "note": "desks for Dana"}]

        # Sara writes again: that is no reply.
        mail.add("Sent Items", ME, "Re: New office — furniture for 20 desks",
                 "Any news?", to=CUSTOMER, message_id="news@sidra.example",
                 in_reply_to="office@harbourline.example")
        assert len(invoke(agents, ex, "watch_check")[0]["unanswered"]) == 1

        mail.add("INBOX", f"Dana Haddad <{CUSTOMER}>",
                 "Re: New office — furniture for 20 desks", "Yes: twenty of each.",
                 date="2026-09-06T10:00:00+04:00",
                 message_id="yes@harbourline.example",
                 in_reply_to="news@sidra.example",
                 references="office@harbourline.example news@sidra.example")
        checked, _ = invoke(agents, ex, "watch_check")
        assert checked["unanswered"] == []
        [answered] = checked["answered"]
        assert answered["message_id"] == "yes@harbourline.example"
        assert answered["reply_from"] == f"Dana Haddad <{CUSTOMER}>"
        assert provider.data["watch"][watch["watch_ref"]]["keys"]["status"] == "replied"
        # Answered once, it is not checked again.
        assert invoke(agents, ex, "watch_check")[0]["checked"] == 0

    def test_the_check_can_run_on_the_clock(self, agents):
        for name in ("watch_check", "watch_new_mail"):
            assert agents["mail"].function(name)["schedulable"] is True


class TestWatchingTheInbox:
    def started(self, agents, ex, **inputs):
        result, status = invoke(agents, ex, "watch_inbox", inputs)
        assert status == "success", result
        return result

    def test_what_is_there_already_is_not_news(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex)
        assert invoke(agents, ex, "watch_new_mail") == (
            {"checked": 1, "messages": [], "more": False, "problems": [], "closed": []}, "success")

    def test_what_arrives_is_handed_on_once_oldest_first(self, agents, mail):
        ex, _ = executor(mail)
        watch = self.started(agents, ex)
        mail.add("INBOX", CUSTOMER, "First", "one", message_id="first@h.example")
        mail.add("INBOX", SUPPLIER, "Second", "two", message_id="second@n.example")
        result, status = invoke(agents, ex, "watch_new_mail")
        assert status == "success", result
        assert [(row["message_id"], row["inbox_ref"]) for row in result["messages"]] == [
            ("first@h.example", watch["inbox_ref"]),
            ("second@n.example", watch["inbox_ref"])]
        assert result["messages"][0]["thread_id"] == "first@h.example"
        assert invoke(agents, ex, "watch_new_mail")[0]["messages"] == []

    def test_senders_are_narrowed_and_our_own_mail_is_not_news(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex, only_from="@harbourline.example",
                     skip_from="noreply@harbourline.example")
        mail.add("INBOX", CUSTOMER, "Wanted", "x", message_id="wanted@h.example")
        mail.add("INBOX", SUPPLIER, "Another sender", "x", message_id="other@n.example")
        mail.add("INBOX", "noreply@harbourline.example", "Newsletter", "x",
                 message_id="news@h.example")
        mail.add("INBOX", ME, "Note to self", "x", message_id="self@sidra.example")
        result, _ = invoke(agents, ex, "watch_new_mail")
        assert [row["message_id"] for row in result["messages"]] == ["wanted@h.example"]
        # What was left out is not shown later either.
        assert invoke(agents, ex, "watch_new_mail")[0]["messages"] == []

    def test_more_than_asked_for_continues_at_the_next_check(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex)
        for number in range(5):
            mail.add("INBOX", CUSTOMER, f"Mail {number}", "x",
                     message_id=f"m{number}@h.example")
        first, _ = invoke(agents, ex, "watch_new_mail", {"max_results": 3})
        assert [row["subject"] for row in first["messages"]] == [
            "Mail 0", "Mail 1", "Mail 2"] and first["more"] is True
        rest, _ = invoke(agents, ex, "watch_new_mail", {"max_results": 3})
        assert [row["subject"] for row in rest["messages"]] == ["Mail 3", "Mail 4"]
        assert rest["more"] is False

    def test_a_watch_may_start_in_the_past(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex, since="2026-09-03T12:00:00+04:00")
        result, _ = invoke(agents, ex, "watch_new_mail")
        assert [row["message_id"] for row in result["messages"]] == [
            "seven@northlight.example"]

    def test_a_start_in_the_future_is_from_now_and_said(self, agents, mail):
        ex, _ = executor(mail)
        watch = self.started(agents, ex, since="2099-01-01T00:00:00")
        assert "starts from now" in watch["note"]
        mail.add("INBOX", CUSTOMER, "Arrived", "x", message_id="arrived@h.example")
        assert len(invoke(agents, ex, "watch_new_mail")[0]["messages"]) == 1

    def test_a_time_that_is_not_one(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "watch_inbox", {"since": "yesterday"})
        assert status == "error" and "ISO 8601" in result["error"]

    def test_an_inbox_the_server_renumbered_loses_nothing_and_says_so(
            self, agents, mail):
        ex, provider = executor(mail)
        watch = self.started(agents, ex)
        later = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        mail.add("INBOX", CUSTOMER, "During", "x", message_id="during@h.example", date=later)
        mail.renumber()
        result, status = invoke(agents, ex, "watch_new_mail")
        assert status == "success", result
        # Found by its arrival time, though its number means nothing now; and the run says what happened.
        assert [row["message_id"] for row in result["messages"]] == ["during@h.example"]
        assert "renumbered" in result["notices"][0]["notice"]
        assert "renumbered" in provider.data["inbox"][watch["inbox_ref"]]["values"]["note"]
        assert invoke(agents, ex, "watch_new_mail")[0]["messages"] == []
        mail.add("INBOX", CUSTOMER, "After", "x", message_id="after@h.example")
        result, _ = invoke(agents, ex, "watch_new_mail")
        assert [row["message_id"] for row in result["messages"]] == ["after@h.example"]


class TestBehindThePlatformsProxy:
    """In the sandbox a program's one way out opens the hosts and ports
    a manifest declared, and a server an account names on the port
    written with it. Mail is not the web's protocol: the connection is
    a tunnel, and what passes through it is the agent's and the mail
    server's."""

    @pytest.fixture
    def proxy(self, agents, mail, monkeypatch):
        """The test's own way out; ``admitted`` runs the agent as if it
        declared these hosts and no others."""
        found = way_out()
        found.admitted = lambda hosts: monkeypatch.setitem(
            agents, "mail", declaring(agents["mail"], hosts))
        return found

    def by_name(self, mail, **fields):
        provider = Records(secrets={"account": {**mail.secret(), **fields}})
        return Chat(provider), provider

    def test_mail_is_read_and_sent_through_the_servers_the_account_names(self, agents, mail, proxy):
        ex, _ = self.by_name(mail)
        found, status = invoke(agents, ex, "search_find", {"subject": "Q-2041"})
        assert status == "success", found
        assert len(found["messages"]) == 2
        drafted, _ = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "Through", "body": "the proxy"})
        sent, status = invoke(agents, ex, "draft_send",
                              {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success" and sent["sent"] is True, sent
        assert len(mail.sent) == 1 and proxy.refusals == 0

    def test_a_server_named_without_its_port_is_refused_and_told_how(
            self, agents, mail, proxy):
        """The way out opens a server read from the account on the web's
        ports only, unless its port is written with it."""
        ex, _ = self.by_name(mail, imap_host="imap.sidra.example")
        result, status = invoke(agents, ex, "search_find")
        assert status == "error" and result["kind"] == "refused"
        assert "imap.sidra.example:993 is not a host this agent declared" in result["error"]
        assert "give it as imap.sidra.example:993" in result["error"]
        assert mail.logins == 0 and proxy.refusals == 1

    def test_a_port_it_did_not_declare_is_refused(self, agents, mail, proxy):
        proxy.admitted([f"imap.sidra.example:{mail.imap_port}",
                        "smtp.sidra.example:465"])
        ex, _ = self.by_name(mail)
        drafted, _ = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "Refused", "body": "on this port"})
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "refused"
        assert f"smtp.sidra.example:{mail.smtp_port} is not a host this agent declared" in result["error"]
        assert "give it as" not in result["error"]
        assert mail.sent == []


# -- 1.1.0: the findings of the review, and what was added ------------------------------------------------------------

def sent_message(mail, index=-1):
    return email.message_from_bytes(mail.sent[index][2], policy=email.policy.default)


def drafted_new(agents, ex, **inputs):
    result, status = invoke(agents, ex, "draft_compose", {
        "to": CUSTOMER, "subject": "Your furniture", "body": "We can furnish twenty desks.", **inputs})
    assert status == "success", result
    return result


class TestSendingRefusalsAndRaces:
    def test_a_refusal_at_data_sent_nothing_and_the_draft_can_go_later(self, agents, mail):
        mail.refuse_data = True
        ex, provider = executor(mail)
        drafted = drafted_new(agents, ex)
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "server", result
        assert "Nothing was sent" in result["error"] and mail.received_data == []
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["status"] == "prepared"
        mail.refuse_data = False
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success" and result["sent"] is True and len(mail.sent) == 1

    def test_a_message_refused_after_its_bytes_is_a_refusal_not_unknown(self, agents, mail):
        mail.reject_after_data = True
        ex, provider = executor(mail)
        drafted = drafted_new(agents, ex)
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "server" and "Nothing was sent" in result["error"]
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["status"] == "prepared"

    def test_two_sends_of_one_draft_at_once_send_it_once(self, agents, mail):
        ex, provider = executor(mail)
        drafted = drafted_new(agents, ex)

        def call():
            return ex.invoke(agents["mail"], "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=3)

        async def both():
            return await asyncio.gather(call(), call())

        results = run(both())
        assert sorted(status for _, status in results) == ["error", "success"], results
        assert len(mail.sent) == 1
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["status"] == "sent"
        again, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and len(mail.sent) == 1

    def test_the_message_id_is_chosen_when_drafted(self, agents, mail):
        ex, provider = executor(mail)
        drafted = drafted_new(agents, ex)
        planned = provider.data["draft"][drafted["draft_ref"]]["keys"]["message_id"]
        result, _ = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert result["message_id"] == planned and sent_message(mail)["Message-ID"] == f"<{planned}>"


class TestOffTheLoop:
    def test_a_slow_mail_server_does_not_hold_up_the_chats_other_calls(self, agents, mail):
        import time

        ex, _ = executor(mail)
        assert invoke(agents, ex, "account_status")[1] == "success"  # the program started
        mail.slow = 1.5

        async def three():
            return await asyncio.gather(*(ex.invoke(agents["mail"], "account_status", {}) for _ in range(3)))

        started = time.monotonic()
        results = run(three())
        assert all(r[0]["connected"] for r in results)
        # Three sign-ins of 1.5 s each, together: well under the 4.5 s they take one after another.
        assert time.monotonic() - started < 3.5


class TestThreadsAndHeaders:
    def test_a_reply_naming_only_in_reply_to_is_in_the_conversation(self, agents, mail):
        mail.add("INBOX", CUSTOMER, "Re: New office — furniture for 20 desks", "Any update?",
                 date="2026-09-05T09:00:00+04:00", message_id="update@harbourline.example",
                 in_reply_to="office@harbourline.example", only_in_reply_to=True)
        mail.add("Sent Items", ME, "Re: New office — furniture for 20 desks", "Tomorrow.", to=CUSTOMER,
                 date="2026-09-05T10:00:00+04:00", message_id="tomorrow@sidra.example",
                 in_reply_to="update@harbourline.example", only_in_reply_to=True)
        ex, _ = executor(mail)
        for asked in ("office@harbourline.example", "tomorrow@sidra.example"):
            result, status = invoke(agents, ex, "search_thread", {"thread_id": asked})
            assert status == "success", result
            assert [m["message_id"] for m in result["messages"]] == [
                "office@harbourline.example", "update@harbourline.example", "tomorrow@sidra.example"]
            assert result["thread_id"] == "office@harbourline.example"

    def test_8_bit_headers_are_read_as_written(self, agents, mail):
        raw = ("From: =?utf-8?b?2YXZg9iq2Kgg2KfZhNmG2YjYsQ==?= <office@alnoor.example>\r\n"
               "To: sara@sidra.example\r\nSubject: عرض سعر الكراسي\r\n"
               "Date: Sat, 05 Sep 2026 08:00:00 +0400\r\nMessage-ID: <raw8@alnoor.example>\r\n"
               "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n"
               "Content-Transfer-Encoding: 8bit\r\n\r\nنرفق لكم عرض السعر.\r\n").encode("utf-8")
        mail.put("INBOX", raw, datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc))
        ex, _ = executor(mail)
        found, status = invoke(agents, ex, "search_find", {"subject": "الكراسي"})
        assert status == "success", found
        [row] = found["messages"]
        assert (row["subject"], row["from"]) == ("عرض سعر الكراسي", "مكتب النور <office@alnoor.example>")
        assert row["snippet"] == "نرفق لكم عرض السعر."

    def test_a_snippet_is_the_words_not_the_css(self, agents, mail):
        style = "<style>" + ".x{color:red}" * 600 + "</style>"
        mail.add("INBOX", CUSTOMER, "Newsletter", "", html=f"<html><head>{style}</head><body><p>Twenty desks "
                 f"are ready.</p></body></html>", message_id="css@harbourline.example")
        mail.add("INBOX", CUSTOMER, "Both", "Plain words first.", html="<p>HTML words</p>",
                 message_id="both@harbourline.example")
        mail.add("INBOX", CUSTOMER, "Arabic", "الطاولات جاهزة " * 50, message_id="ar@harbourline.example")
        ex, _ = executor(mail)
        rows = {r["message_id"]: r["snippet"] for r in invoke(agents, ex, "search_find", {"max_results": 6})[0]
                ["messages"]}
        assert rows["css@harbourline.example"] == "Twenty desks are ready."
        assert rows["both@harbourline.example"] == "Plain words first."
        assert rows["ar@harbourline.example"].startswith("الطاولات جاهزة الطاولات")


class TestFolders:
    def test_folders_other_than_ascii_both_ways(self, agents, mail):
        from tests.mail_stub import Folder

        for name in ("الأرشيف", "Входящие"):
            mail.folders[name] = Folder(name)
        mail.add("الأرشيف", CUSTOMER, "Old order", "From last year.", message_id="old@harbourline.example")
        ex, _ = executor(mail)
        listed, status = invoke(agents, ex, "folders_list", {"counts": True})
        assert status == "success", listed
        by_name = {f["name"]: f for f in listed["folders"]}
        assert by_name["الأرشيف"]["messages"] == 1 and by_name["Sent Items"]["kind"] == "sent"
        assert by_name["INBOX"]["kind"] == "inbox" and "Входящие" in by_name
        found, status = invoke(agents, ex, "search_find", {"folder": "الأرشيف"})
        assert status == "success", found
        assert found["folder"] == "الأرشيف" and [r["message_id"] for r in found["messages"]] == [
            "old@harbourline.example"]
        missing, status = invoke(agents, ex, "search_find", {"folder": "Архив"})
        assert status == "error" and missing["kind"] == "not_found"
        assert mail.bad == []


class TestReplyAllForwardAndAttachments:
    def test_reply_all_copies_everyone_else(self, agents, mail):
        mail.add("INBOX", f"Dana Haddad <{CUSTOMER}>", "Fit-out", "Who is joining?",
                 to=f"{ME}, omar@harbourline.example", cc="Lina <lina@harbourline.example>, sales@sidra.example",
                 message_id="fitout@harbourline.example")
        ex, provider = executor(mail)
        result, status = invoke(agents, ex, "draft_reply", {"thread_id": "fitout@harbourline.example",
                                                            "body": "All of us.", "reply_all": True})
        assert status == "success", result
        assert result["to"] == CUSTOMER
        assert result["cc"] == "omar@harbourline.example, Lina <lina@harbourline.example>, sales@sidra.example"
        invoke(agents, ex, "draft_send", {"draft_ref": result["draft_ref"]}, chat_level=3)
        assert mail.sent[-1][1] == [CUSTOMER, "omar@harbourline.example", "lina@harbourline.example",
                                    "sales@sidra.example"]

    def test_a_forward_carries_the_message_and_its_attachments(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "draft_forward", {
            "message_id": "q2041@northlight.example", "to": "omar@sidra.example", "body": "Check the prices?"})
        assert status == "success", result
        assert result["subject"] == "Fwd: Quotation Q-2041 — 20 office chairs" and result["attachments"] == [
            "Q-2041.pdf"]
        sent, status = invoke(agents, ex, "draft_send", {"draft_ref": result["draft_ref"]}, chat_level=3)
        assert status == "success", sent
        message = sent_message(mail)
        text = message.get_body(("plain",)).get_content()
        assert text.startswith("Check the prices?") and "---------- Forwarded message ----------" in text
        assert "Please find our quotation attached." in text
        [attached] = list(message.iter_attachments())
        assert attached.get_filename() == "Q-2041.pdf" and attached.get_content() == QUOTE_PDF

    def test_chat_files_are_attached_with_arabic_names(self, agents, mail):
        ex, provider = executor(mail)
        content = b"%PDF-1.4 " + bytes(range(256)) * 10
        ref = ex.file("عرض السعر.pdf", content)
        drafted = drafted_new(agents, ex, subject="العرض المطلوب", body="مرفق عرض السعر.", attachments=[ref])
        assert drafted["attachments"] == ["عرض السعر.pdf"]
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success", result
        raw = mail.sent[-1][2]
        assert raw.isascii() and b"filename*=utf-8''" in raw
        message = sent_message(mail)
        assert message["Subject"] == "العرض المطلوب"
        assert message.get_body(("plain",)).get_content().strip() == "مرفق عرض السعر."
        [attached] = list(message.iter_attachments())
        assert attached.get_filename() == "عرض السعر.pdf" and attached.get_content() == content

    def test_files_over_what_a_message_carries_are_refused_with_the_numbers(self, agents, mail):
        ex, provider = executor(mail)
        refs = [ex.file(f"plan-{n}.pdf", b"x" * (7 * 1024 * 1024)) for n in range(3)]
        result, status = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Plans", "body": "Here.",
                                                              "attachments": refs})
        assert status == "error" and result["kind"] == "too_large" and "21.0 MB" in result["error"]
        assert not provider.data.get("draft") and not provider.files.get("attachment")


class TestWatchesThatNeverLoseMail:
    def test_a_reply_watch_whose_anchor_is_gone_still_sees_the_reply(self, agents, mail):
        ex, _ = executor(mail)
        invoke(agents, ex, "watch_await_reply", {"thread_id": "office@harbourline.example"})
        inbox = mail.folders["INBOX"]
        inbox.messages = [m for m in inbox.messages if m.message["Message-ID"] != "<office@harbourline.example>"]
        mail.add("INBOX", f"Dana Haddad <{CUSTOMER}>", "Re: New office — furniture for 20 desks", "Yes please.",
                 date="2026-09-07T10:00:00+04:00", message_id="yesplease@harbourline.example",
                 in_reply_to="office@harbourline.example")
        checked, status = invoke(agents, ex, "watch_check")
        assert status == "success", checked
        assert [a["message_id"] for a in checked["answered"]] == ["yesplease@harbourline.example"]

    def test_a_watch_that_fails_loses_nothing_and_a_gone_conversation_closes(self, agents, mail):
        provider = Records(secrets={"account": [
            {"ref": "sec_sara", **mail.secret()},
            {"ref": "sec_work", **mail.secret("a-password-since-changed"), "account": "work@sidra.example"}]})
        ex = Chat(provider)
        invoke(agents, ex, "watch_inbox", {"account": ME})
        # A watch on the other account, whose password was changed since: it fails, alone.
        stranger = provider.create_data("inbox", {"since": "2026-09-01T00:00:00+00:00", "folder_validity": "1",
                                                  "last_number": "0", "status": "watching",
                                                  "account": "work@sidra.example"})
        mail.add("INBOX", CUSTOMER, "Arrived", "x", message_id="arrived@h.example")
        result, status = invoke(agents, ex, "watch_new_mail")
        assert status == "success", result
        assert [r["message_id"] for r in result["messages"]] == ["arrived@h.example"]
        assert [p["watch_ref"] for p in result["problems"]] == [stranger]
        assert provider.data["inbox"][stranger]["keys"]["last_number"] == "0"
        assert invoke(agents, ex, "watch_new_mail")[0]["messages"] == []
        # A conversation that is gone closes its reply watch rather than failing every run.
        watch, _ = invoke(agents, ex, "watch_await_reply", {"thread_id": "office@harbourline.example",
                                                            "account": ME})
        mail.folders["INBOX"].messages = [m for m in mail.folders["INBOX"].messages
                                          if "harbourline" not in str(m.message["From"])]
        checked, _ = invoke(agents, ex, "watch_check")
        assert [c["watch_ref"] for c in checked["closed"]] == [watch["watch_ref"]]
        assert provider.data["watch"][watch["watch_ref"]]["keys"]["status"] == "closed"

    def test_watches_are_listed_and_closed(self, agents, mail):
        ex, _ = executor(mail)
        reply, _ = invoke(agents, ex, "watch_await_reply", {"thread_id": "office@harbourline.example"})
        inbox, _ = invoke(agents, ex, "watch_inbox")
        listed = invoke(agents, ex, "watches_list")[0]["watches"]
        assert [(w["watch_ref"], w["kind"]) for w in listed] == [(reply["watch_ref"], "reply"),
                                                                 (inbox["inbox_ref"], "inbox")]
        for ref in (reply["watch_ref"], inbox["inbox_ref"]):
            assert invoke(agents, ex, "watches_close", {"watch_ref": ref})[0]["closed"] is True
        assert invoke(agents, ex, "watches_list")[0]["watches"] == []
        assert invoke(agents, ex, "watch_new_mail")[0]["checked"] == 0
        assert invoke(agents, ex, "watch_check")[0]["checked"] == 0
        result, status = invoke(agents, ex, "watches_close", {"watch_ref": "nothing_1"})
        assert status == "error" and result["kind"] == "not_found"
