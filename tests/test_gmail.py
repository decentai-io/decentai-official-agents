"""The Gmail agent, its program started and spoken to in the standard's messages, against a loopback Gmail as strict
as the real one, holding fictional mail for Sidra Office Supplies."""

import time

import pytest

from standard.http import FILE_MOST
from tests.gmail_stub import GmailStub, compose, rfc_date
from tests.harness import Chat, Records, probe, run

SUPPLIER = "quotes@northlight-seating.example"
CUSTOMER = "dana@harbourline.example"
COLLEAGUE = "omar@harbourline.example"
QUOTE_PDF = b"%PDF-1.4 fictional quotation " + bytes(range(256))


@pytest.fixture
def gmail():
    stub = GmailStub().start()
    stub.add_message("t-quote", SUPPLIER, GmailStub.ACCOUNT, "Quotation Q-2041 — 20 office chairs",
                     "Please find our quotation attached. Delivery in 10 working days.",
                     "Thu, 03 Sep 2026 10:12:00 +0400", attachments=[("Q-2041.pdf", "application/pdf", QUOTE_PDF)],
                     message_id="<q2041@northlight.example>")
    stub.add_message("t-office", CUSTOMER, GmailStub.ALIAS, "New office — furniture for 20 desks",
                     "We are moving in November and need desks and chairs for 20 people.",
                     "Wed, 02 Sep 2026 15:40:00 +0400", cc=f"{COLLEAGUE}, {GmailStub.ACCOUNT}",
                     message_id="<office@harbourline.example>")
    yield stub
    stub.stop()


def executor(gmail, access_token=None, sinks=None):
    provider = Records(secrets={"google": gmail.secret(access_token)})
    return Chat(provider, sinks), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["gmail"], name, inputs, chat_level=chat_level))


def soon(seconds=0):
    """A Date a little after now: mail arriving while a watch runs."""
    return rfc_date(int(time.time()) + 5 + seconds)


def first_message(gmail, thread_id):
    return gmail.thread(thread_id)[0]["id"]


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        assert probe(agents["gmail"]) == []

    def test_the_manifest_keeps_the_standards_rules(self, agents):
        manifest = agents["gmail"].manifest
        assert manifest["agent"]["version"] == "2.0.0"
        assert len(manifest["functions"]) <= 100 and len(manifest["agent"]["instructions"]) <= 4000
        assert len(manifest["agent"]["examples"]) <= 6
        [secret] = manifest["resources"]["secrets"]
        assert secret["binding"] == {"cardinality": "many", "required": True}
        assert "https://www.googleapis.com/auth/gmail.modify" in secret["oauth"]["scopes"]
        for function in manifest["functions"]:
            if function.get("schedulable"):
                assert function["level"] <= 1 and "model" not in function.get("calls", [])
            if function["id"] not in ("account_list", "watch_check", "watch_new_mail", "watches_list",
                                      "watches_close", "draft_send", "draft_discard"):
                assert "account" in function["inputs"]["properties"], function["id"]
        levels = {f["id"]: f["level"] for f in manifest["functions"]}
        assert levels["draft_send"] == 3 and levels["mail_trash"] == 3
        assert levels["search_save_attachment"] == 0 and levels["draft_reply"] == 2 and levels["mail_archive"] == 2
        # Mail content is kept in values, never in plain keys.
        draft = next(r for r in manifest["resources"]["data"] if r["id"] == "draft")
        assert {f["name"]: f["storage"] for f in draft["fields"]}["content"] == "values"
        assert {"watches_list", "watches_close"} <= set(levels)

    def test_the_account_reports_its_address_and_its_aliases(self, agents, gmail):
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success" and result["connected"] is True and result["email"] == GmailStub.ACCOUNT
        assert [a["address"] for a in result["send_as"]] == [GmailStub.ACCOUNT, GmailStub.ALIAS]


class TestSeveralAccounts:
    OTHER = "other@sidra.example"

    def executor(self, gmail):
        # The second account's token is one the loopback service refuses: reaching it proves which was used.
        provider = Records(secrets={"google": [
            {"ref": "sec_first", **gmail.secret()},
            {"ref": "sec_second", **gmail.secret("at-2"), "account": self.OTHER}]})
        return Chat(provider), provider

    def test_the_accounts_are_listed_without_a_token(self, agents, gmail):
        ex, _ = self.executor(gmail)
        result, status = invoke(agents, ex, "account_list", {})
        assert status == "success"
        assert [a["account"] for a in result["accounts"]] == [GmailStub.ACCOUNT, self.OTHER]
        assert "at-1" not in str(result) and "at-2" not in str(result)

    def test_a_function_takes_the_account_by_address(self, agents, gmail):
        ex, _ = self.executor(gmail)
        result, _ = invoke(agents, ex, "account_status", {})
        assert result["connected"] is False and self.OTHER in result["problem"]
        result, _ = invoke(agents, ex, "account_status", {"account": "demo"})
        assert result["connected"] is True
        result, _ = invoke(agents, ex, "account_status", {"account": self.OTHER})
        assert result["connected"] is False
        # A part that names both is no choice (1.x took the first that contained it).
        result, status = invoke(agents, ex, "search_find", {"query": "chairs", "account": "sidra"})
        assert status == "error" and self.OTHER in result["error"]

    def test_a_draft_is_sent_from_the_account_it_was_made_on(self, agents, gmail):
        ex, provider = self.executor(gmail)
        drafted, status = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "Hello", "body": "From the first.", "account": GmailStub.ACCOUNT}, 2)
        assert status == "success", drafted
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["account"] == GmailStub.ACCOUNT
        sent, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, 3)
        assert status == "success" and sent["sent"] is True, sent


class TestReading:
    def test_find_takes_gmails_operators_and_pages(self, agents, gmail):
        for i in range(3):
            gmail.add_message(f"t-{i}", SUPPLIER, GmailStub.ACCOUNT, f"Update {i}", "chairs",
                              "Fri, 04 Sep 2026 09:00:00 +0400")
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "search_find", {"query": "from:northlight has:attachment quotation"})
        assert status == "success", result
        [row] = result["messages"]
        assert row["thread_id"] == "t-quote" and row["unread"] is True and "INBOX" in row["labels"]
        assert row["link"].endswith(row["message_id"]) and GmailStub.ACCOUNT in row["link"]
        first, _ = invoke(agents, ex, "search_find", {"query": "subject:Update", "max_results": 2})
        assert len(first["messages"]) == 2 and first["next_page_token"]
        second, _ = invoke(agents, ex, "search_find", {"query": "subject:Update", "max_results": 2,
                                                        "page_token": first["next_page_token"]})
        assert len({m["message_id"] for m in first["messages"] + second["messages"]}) == 3

    def test_a_busy_gmail_is_waited_out_429_and_403_rate(self, agents, gmail):
        ex, _ = executor(gmail)
        gmail.busy = [429, "403"]
        result, status = invoke(agents, ex, "search_find", {"query": "quotation"})
        assert status == "success" and len(result["messages"]) == 1, result
        assert gmail.busy == []

    def test_a_missing_permission_says_reconnect(self, agents, gmail):
        ex, _ = executor(gmail)
        gmail.busy = ["scope"]
        result, status = invoke(agents, ex, "search_thread", {"thread_id": "t-quote"})
        assert status == "error" and result["kind"] == "scope" and "reconnect" in result["error"]

    def test_a_thread_is_read_whole_with_its_attachments_by_part(self, agents, gmail):
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "search_thread", {"thread_id": "t-quote"})
        assert status == "success", result
        message = result["messages"][0]
        assert message["from"] == SUPPLIER and "10 working days" in message["body"]
        assert message["truncated"] is False and message["attachments"] == 1
        [attachment] = result["attachments"]
        assert (attachment["filename"], attachment["mime_type"], attachment["size"], attachment["part_id"]) == (
            "Q-2041.pdf", "application/pdf", len(QUOTE_PDF), "1")

    def test_each_part_is_decoded_by_its_own_charset(self, agents, gmail):
        arabic = "نرجو تأكيد موعد التسليم يوم الخميس"
        japanese = "見積書を添付いたします。よろしくお願いいたします。"
        french = "Réunion à l'hôtel — déjà prévue"
        for charset, text, sender in [("iso-8859-6", arabic, "a@one.example"), ("windows-1256", arabic, "b@two.example"),
                                      ("iso-2022-jp", japanese, "c@three.example"),
                                      ("iso-8859-1", french.replace("—", "-"), "d@four.example"),
                                      ("utf-8", arabic, "e@five.example")]:
            gmail.add_message("t-mixed", sender, GmailStub.ACCOUNT, "Mixed", text, "Fri, 04 Sep 2026 09:00:00 +0400",
                              charset=charset)
        # One message whose two parts are in two charsets (text in Windows-1256, an HTML part in UTF-8).
        raw = compose("f@six.example", GmailStub.ACCOUNT, "Two charsets", arabic, "Fri, 04 Sep 2026 09:30:00 +0400",
                      charset="windows-1256", attachments=[("ملاحظات.txt", "text/plain", "ok".encode())])
        gmail.put(raw, "t-mixed", ["INBOX"])
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "search_thread", {"thread_id": "t-mixed"})
        assert status == "success", result
        bodies = [m["body"] for m in result["messages"]]
        assert bodies[0] == arabic and bodies[1] == arabic and bodies[2] == japanese
        assert bodies[3] == french.replace("—", "-") and bodies[4] == arabic and bodies[5] == arabic
        assert result["attachments"][0]["filename"] == "ملاحظات.txt"

    def test_html_only_mail_becomes_text_and_inline_images_are_left_out(self, agents, gmail):
        page = ("<html><head><style>p{color:red}</style></head><body><p>Dear Sidra,</p><p>The <b>new</b> price list "
                "is <a href='https://northlight.example/prices'>here</a>.</p><ul><li>Chairs</li><li>Desks</li></ul>"
                "<img src='cid:logo1'></body></html>")
        raw = compose(SUPPLIER, GmailStub.ACCOUNT, "Prices", None, "Fri, 04 Sep 2026 09:00:00 +0400", rich=page,
                      inline_images=[("logo1", "logo.png", b"\x89PNG fake")],
                      attachments=[("prices.pdf", "application/pdf", b"%PDF-1.4 prices")])
        gmail.put(raw, "t-html", ["INBOX"])
        ex, _ = executor(gmail)
        result, _ = invoke(agents, ex, "search_thread", {"thread_id": "t-html"})
        body = result["messages"][0]["body"]
        assert "Dear Sidra," in body and "The new price list is here (https://northlight.example/prices)." in body
        assert "• Chairs" in body and "color:red" not in body and "<" not in body
        assert [a["filename"] for a in result["attachments"]] == ["prices.pdf"]
        assert result["messages"][0]["inline_images_left_out"] == 1

    def test_a_long_body_is_fetched_whole_and_paged_not_cut(self, agents, gmail):
        """Gmail hands a long text part as an attachmentId, not inline: 1.x read it as empty."""
        lines = [f"Line {n:05d}: يرجى مراجعة البند رقم {n}." for n in range(3000)]
        gmail.add_message("t-long", SUPPLIER, GmailStub.ACCOUNT, "Terms", "\n".join(lines),
                          "Fri, 04 Sep 2026 09:00:00 +0400")
        ex, _ = executor(gmail)
        result, _ = invoke(agents, ex, "search_thread", {"thread_id": "t-long", "max_body_chars": 5000})
        message = result["messages"][0]
        assert message["truncated"] is True and len(message["body"]) == 5000 and "note" in result
        whole, offset = message["body"], message["next_offset"]
        while offset is not None:
            page, status = invoke(agents, ex, "search_message", {"message_id": message["message_id"],
                                                                  "offset": offset, "max_chars": 50000})
            assert status == "success", page
            whole += page["body"]
            offset = page.get("next_offset")
        assert whole == "\n".join(lines) and message["body_chars"] == len(whole)

    def test_encoded_headers_are_decoded(self, agents, gmail):
        gmail.add_message("t-enc", SUPPLIER, GmailStub.ACCOUNT, "عرض سعر جديد", "x", "Fri, 04 Sep 2026 09:00:00 +0400")
        gmail.encoded_headers = True
        ex, _ = executor(gmail)
        result, _ = invoke(agents, ex, "search_thread", {"thread_id": "t-enc"})
        assert result["messages"][0]["subject"] == "عرض سعر جديد"

    def test_an_attachment_is_saved_by_its_part_though_its_id_changed(self, agents, gmail):
        name = "عرض السعر Q-2041.pdf"
        gmail.add_message("t-ar", SUPPLIER, GmailStub.ACCOUNT, "Q", "x", "Fri, 04 Sep 2026 09:00:00 +0400",
                          attachments=[(name, "application/pdf", QUOTE_PDF)])
        ex, _ = executor(gmail)
        thread, _ = invoke(agents, ex, "search_thread", {"thread_id": "t-ar"})
        [attachment] = thread["attachments"]
        assert attachment["filename"] == name
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": attachment["message_id"], "part_id": attachment["part_id"]})
        assert status == "success", result
        assert ex.files[result["file_ref"]] == {"name": name, "content": QUOTE_PDF}
        # The id it was listed with still finds it, as Gmail still accepts an older id.
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": attachment["message_id"], "attachment_id": attachment["attachment_id"]})
        assert status == "success", result

    def test_an_attachment_too_large_for_the_chat_is_refused_before_download(self, agents, gmail):
        big = b"%PDF-1.4 " + b"x" * (FILE_MOST + 1)
        message_id = gmail.add_message("t-big", SUPPLIER, GmailStub.ACCOUNT, "Large", "See attached.",
                                       "Thu, 03 Sep 2026 10:12:00 +0400",
                                       attachments=[("huge.pdf", "application/pdf", big)])
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "search_save_attachment", {"message_id": message_id, "part_id": "1"})
        assert status == "error" and result["kind"] == "too_large", result
        assert f"{len(big):,} bytes" in result["error"] and "https://mail.google.com/" in result["error"]
        assert not [r for r in gmail.requests if "/attachments/" in r[1]]
        assert ex.files == {}

    def test_an_unknown_thread_is_named_not_invented(self, agents, gmail):
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "search_thread", {"thread_id": "t-nope"})
        assert status == "error" and result["kind"] == "not_found"


class TestDrafting:
    def test_a_new_message_carries_chat_files_as_attachments(self, agents, gmail):
        """The owner's "Gmail not attaching files": a draft now takes the chat's files."""
        ex, provider = executor(gmail)
        pdf = ex.file("تقرير الربع الأول.pdf", b"%PDF-1.4 " + b"r" * 5000)
        sheet = ex.file("prices.xlsx", b"PK\x03\x04 sheet")
        result, status = invoke(agents, ex, "draft_compose", {
            "to": f"Dana <{CUSTOMER}>; {COLLEAGUE}", "subject": "التقرير — Q1", "attachments": [pdf, sheet],
            "body": "Dana,\nAttached: the report & <prices>.\nمع التحية"}, 2)
        assert status == "success", result
        assert [a["filename"] for a in result["attachments"]] == ["تقرير الربع الأول.pdf", "prices.xlsx"]
        assert result["signature_added"] is True and gmail.sends == []
        draft = gmail.draft(result["gmail_draft_id"])
        assert draft.get_content_type() == "multipart/mixed" and str(draft["Subject"]) == "التقرير — Q1"
        assert str(draft["From"]) == f"Sidra Office <{GmailStub.ACCOUNT}>"
        files = {p.get_filename(): p.get_content() for p in draft.iter_attachments()}
        assert files == {"تقرير الربع الأول.pdf": b"%PDF-1.4 " + b"r" * 5000, "prices.xlsx": b"PK\x03\x04 sheet"}
        raw = gmail.messages[gmail.drafts[result["gmail_draft_id"]]]["raw"].decode("ascii")
        assert "filename*" in raw and "utf-8''" in raw            # RFC 2231
        plain = draft.get_body(("plain",)).get_content()
        rich = draft.get_body(("html",)).get_content()
        assert "Attached: the report & <prices>." in plain and "مع التحية" in plain and "Sidra Office Supplies" in plain
        assert "the report &amp; &lt;prices&gt;." in rich and "<b>Sidra Office Supplies</b>" in rich
        record = provider.data["draft"][result["draft_ref"]]
        assert "to" not in record["keys"] and CUSTOMER in record["values"]["content"]["to"]

    def test_a_large_draft_is_uploaded_not_put_in_json(self, agents, gmail):
        ex, _ = executor(gmail)
        scan = ex.file("scan.pdf", b"%PDF-1.4 " + bytes(range(256)) * 24000)    # about 6 MB
        result, status = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Scan", "body": "Scan.",
                                                              "attachments": [scan]}, 2)
        assert status == "success", result
        assert gmail.uploads == ["multipart"]
        [attached] = gmail.draft(result["gmail_draft_id"]).iter_attachments()
        assert attached.get_content() == b"%PDF-1.4 " + bytes(range(256)) * 24000

    def test_more_than_gmail_sends_is_refused_with_the_sizes(self, agents, gmail):
        ex, provider = executor(gmail)
        refs = [ex.file(f"part{n}.bin", b"z" * (7 * 1000 * 1000)) for n in range(4)]
        result, status = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Big", "body": "x",
                                                              "attachments": refs}, 2)
        assert status == "error" and result["kind"] == "too_large", result
        assert "25" in result["error"] and gmail.drafts == {} and provider.data.get("draft") in (None, {})

    def test_a_file_too_large_for_the_chat_is_refused(self, agents, gmail):
        ex, _ = executor(gmail)
        ref = ex.file("huge.bin", b"z" * (FILE_MOST + 10))
        result, status = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "x", "body": "x",
                                                              "attachments": [ref]}, 2)
        assert status == "error" and result["kind"] == "too_large" and gmail.drafts == {}

    def test_a_wrong_address_is_refused(self, agents, gmail):
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "draft_compose", {"to": "dana at harbourline", "subject": "x",
                                                              "body": "x"}, 2)
        assert status == "error" and result["kind"] == "invalid" and gmail.drafts == {}

    def test_a_reply_keeps_the_thread_and_quotes_the_original(self, agents, gmail):
        ex, provider = executor(gmail)
        result, status = invoke(agents, ex, "draft_reply", {
            "thread_id": "t-quote", "body": "Thank you. Could delivery happen by Thursday 17 September?"}, 2)
        assert status == "success", result
        assert result["to"] == SUPPLIER and result["subject"] == "Re: Quotation Q-2041 — 20 office chairs"
        assert result["thread_id"] == "t-quote" and "note" not in result and result["quoted"] is True
        draft = gmail.draft(result["gmail_draft_id"])
        assert str(draft["In-Reply-To"]) == "<q2041@northlight.example>"
        assert str(draft["References"]) == "<q2041@northlight.example>"
        plain = draft.get_body(("plain",)).get_content()
        assert "Thursday 17 September" in plain and "> Please find our quotation attached." in plain
        assert provider.data["draft"][result["draft_ref"]]["keys"]["kind"] == "reply"

    def test_reply_all_keeps_the_cc_never_us_and_answers_from_the_alias_it_went_to(self, agents, gmail):
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "draft_reply", {"thread_id": "t-office", "body": "Noted, all.",
                                                            "reply_all": True}, 2)
        assert status == "success", result
        assert result["to"] == CUSTOMER and result["cc"] == COLLEAGUE
        assert GmailStub.ACCOUNT not in result["cc"] and GmailStub.ALIAS not in result["cc"]
        assert result["from"] == f"Sidra Sales <{GmailStub.ALIAS}>"
        # A plain reply goes to the sender alone.
        result, _ = invoke(agents, ex, "draft_reply", {"thread_id": "t-office", "body": "Noted."}, 2)
        assert result["to"] == CUSTOMER and "cc" not in result

    def test_a_reply_honours_reply_to_and_skips_a_draft_in_the_thread(self, agents, gmail):
        gmail.add_message("t-quote", SUPPLIER, GmailStub.ACCOUNT, "Re: Quotation Q-2041 — 20 office chairs",
                          "Updated.", "Thu, 03 Sep 2026 12:00:00 +0400", reply_to="sales@northlight-seating.example",
                          message_id="<update@northlight.example>", references="<q2041@northlight.example>")
        ex, _ = executor(gmail)
        invoke(agents, ex, "draft_reply", {"thread_id": "t-quote", "body": "A draft of ours."}, 2)
        result, status = invoke(agents, ex, "draft_reply", {"thread_id": "t-quote", "body": "Thanks."}, 2)
        assert status == "success" and result["to"] == "sales@northlight-seating.example", result
        draft = gmail.draft(result["gmail_draft_id"])
        assert str(draft["References"]) == "<q2041@northlight.example> <update@northlight.example>"

    def test_a_reply_to_our_own_last_message_goes_on_to_its_recipients(self, agents, gmail):
        gmail.add_message("t-quote", GmailStub.ALIAS, f"{SUPPLIER}, {GmailStub.ACCOUNT}",
                          "Re: Quotation Q-2041 — 20 office chairs", "Noted, thanks.", "Thu, 03 Sep 2026 11:00:00 +0400",
                          labels=("SENT",))
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "draft_reply", {"thread_id": "t-quote", "body": "One more question.",
                                                            "reply_all": True}, 2)
        assert status == "success" and result["to"] == SUPPLIER and "cc" not in result, result

    def test_a_forward_carries_the_originals_attachments_and_a_chat_file(self, agents, gmail):
        ex, _ = executor(gmail)
        note = ex.file("notes.txt", "Our notes".encode())
        result, status = invoke(agents, ex, "draft_forward", {
            "message_id": first_message(gmail, "t-quote"), "to": "procurement@sidra.example",
            "body": "For approval, please.", "attachments": [note]}, 2)
        assert status == "success", result
        assert result["subject"] == "Fwd: Quotation Q-2041 — 20 office chairs"
        assert [a["filename"] for a in result["attachments"]] == ["Q-2041.pdf", "notes.txt"]
        draft = gmail.draft(result["gmail_draft_id"])
        assert {p.get_filename(): p.get_content() for p in draft.iter_attachments()}["Q-2041.pdf"] == QUOTE_PDF
        plain = draft.get_body(("plain",)).get_content()
        assert "---------- Forwarded message ---------" in plain and f"From: {SUPPLIER}" in plain
        assert "Delivery in 10 working days." in plain

    def test_sending_needs_the_chats_trust_is_confirmed_and_never_twice(self, agents, gmail):
        ex, provider = executor(gmail)
        drafted, _ = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Your new office",
                                                          "body": "Dana, we can furnish 20 desks."}, 2)
        refused, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]})
        assert status == "error" and refused.get("denied") is True and gmail.sends == []
        sent, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, 3)
        assert status == "success" and sent["status"] == "sent" and sent["message_id"] in gmail.messages, sent
        record = provider.data["draft"][drafted["draft_ref"]]["keys"]
        assert (record["status"], record["sent_message_id"]) == ("sent", sent["message_id"])
        again, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, 3)
        assert status == "error" and "already sent" in again["error"] and len(gmail.sends) == 1

    def test_an_unanswered_send_is_unknown_and_never_retried(self, agents, gmail):
        ex, provider = executor(gmail)
        drafted, _ = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Hello", "body": "Body."}, 2)
        gmail.drop_send = True
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, 3)
        assert status == "error" and result["kind"] == "unknown" and "Sent" in result["error"]
        assert gmail.sends == [drafted["gmail_draft_id"]]
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["status"] == "unknown"
        gmail.drop_send = False
        result, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, 3)
        assert status == "error" and result["kind"] == "unknown" and len(gmail.sends) == 1

    def test_a_busy_send_is_sent_once_after_the_wait(self, agents, gmail):
        """429 means Gmail did nothing: the send goes again, once, and only once succeeds."""
        ex, _ = executor(gmail)
        drafted, _ = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Hello", "body": "Body."}, 2)
        gmail.busy = [429]
        sent, status = invoke(agents, ex, "draft_send", {"draft_ref": drafted["draft_ref"]}, 3)
        assert status == "success", sent
        assert len([m for m in gmail.messages.values() if "SENT" in m["labelIds"] and m["threadId"] ==
                    sent["thread_id"]]) == 1

    def test_a_discarded_draft_is_gone_from_gmail(self, agents, gmail):
        ex, provider = executor(gmail)
        drafted, _ = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "Hello", "body": "Body."}, 2)
        result, status = invoke(agents, ex, "draft_discard", {"draft_ref": drafted["draft_ref"]}, 2)
        assert (status, result) == ("success", {"discarded": True})
        assert drafted["gmail_draft_id"] not in gmail.drafts
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["status"] == "discarded"


class TestOrganizing:
    def test_labels_are_listed_put_on_and_taken_off(self, agents, gmail):
        gmail.add_label("Suppliers")
        ex, _ = executor(gmail)
        labels, _ = invoke(agents, ex, "mail_labels", {})
        assert labels["labels"][0] == {"label_id": gmail.label_id("Suppliers"), "name": "Suppliers", "type": "user"}
        result, status = invoke(agents, ex, "mail_label", {"thread_ids": ["t-quote"], "add": ["suppliers"]}, 2)
        assert status == "success" and result["done"] == [{"thread_id": "t-quote"}], result
        assert gmail.label_id("Suppliers") in gmail.thread("t-quote")[0]["labelIds"]
        refused, status = invoke(agents, ex, "mail_label", {"thread_ids": ["t-quote"], "add": ["Quotes 2026"]}, 2)
        assert status == "error" and refused["kind"] == "not_found" and "Suppliers" in refused["error"]
        made, status = invoke(agents, ex, "mail_label", {"thread_ids": ["t-quote"], "add": ["Quotes 2026"],
                                                         "create_missing": True}, 2)
        assert status == "success" and gmail.label_id("Quotes 2026") in gmail.thread("t-quote")[0]["labelIds"]
        removed, _ = invoke(agents, ex, "mail_label", {"thread_ids": ["t-quote"], "remove": ["Suppliers"]}, 2)
        assert gmail.label_id("Suppliers") not in gmail.thread("t-quote")[0]["labelIds"]

    def test_archive_mark_star_by_thread_and_by_message(self, agents, gmail):
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "mail_archive", {"thread_ids": ["t-quote", "t-gone"]}, 2)
        assert status == "success" and result["done"] == [{"thread_id": "t-quote"}]
        assert result["failed"][0]["kind"] == "not_found"
        assert "INBOX" not in gmail.thread("t-quote")[0]["labelIds"]
        message_id = first_message(gmail, "t-office")
        invoke(agents, ex, "mail_mark", {"message_ids": [message_id], "as": "read"}, 2)
        invoke(agents, ex, "mail_mark", {"message_ids": [message_id], "as": "starred"}, 2)
        assert gmail.messages[message_id]["labelIds"] == ["INBOX", "STARRED"]
        assert ("POST", "/gmail/v1/users/me/messages/batchModify") in gmail.requests

    def test_trash_needs_level_three_says_thirty_days_and_restores(self, agents, gmail):
        ex, _ = executor(gmail)
        refused, status = invoke(agents, ex, "mail_trash", {"thread_ids": ["t-quote"]}, 2)
        assert status == "error" and refused.get("denied") is True
        result, status = invoke(agents, ex, "mail_trash", {"thread_ids": ["t-quote"]}, 3)
        assert status == "success" and "30 days" in result["note"]
        assert "TRASH" in gmail.thread("t-quote")[0]["labelIds"]
        invoke(agents, ex, "mail_restore", {"thread_ids": ["t-quote"]}, 2)
        assert "TRASH" not in gmail.thread("t-quote")[0]["labelIds"]

    def test_nothing_named_is_refused(self, agents, gmail):
        ex, _ = executor(gmail)
        result, status = invoke(agents, ex, "mail_archive", {}, 2)
        assert status == "error" and result["kind"] == "invalid"


class TestReplyWatches:
    def test_a_watch_reports_only_while_the_reply_is_missing(self, agents, gmail):
        ex, provider = executor(gmail)
        watch, status = invoke(agents, ex, "watch_await_reply", {"thread_id": "t-quote", "note": "by Thursday?"})
        assert status == "success" and watch["since_message_id"] == first_message(gmail, "t-quote")
        assert provider.data["watch"][watch["watch_ref"]]["values"]["subject"].startswith("Quotation")
        # Our own follow-up, from the send-as alias, is not a reply (1.x counted it as one).
        gmail.add_message("t-quote", GmailStub.ALIAS, SUPPLIER, "Re: Quotation Q-2041 — 20 office chairs",
                          "Could delivery happen by Thursday?", "Thu, 03 Sep 2026 12:00:00 +0400", labels=("INBOX",))
        checked, status = invoke(agents, ex, "watch_check", {})
        assert status == "success" and checked["answered"] == [], checked
        assert checked["unanswered"] == [{"watch_ref": watch["watch_ref"], "thread_id": "t-quote",
                                          "subject": "Quotation Q-2041 — 20 office chairs", "note": "by Thursday?"}]
        reply = gmail.add_message("t-quote", SUPPLIER, GmailStub.ACCOUNT, "Re: Quotation", "Yes, Thursday works.",
                                  "Fri, 04 Sep 2026 08:30:00 +0400")
        checked, _ = invoke(agents, ex, "watch_check", {})
        assert checked["unanswered"] == [] and checked["answered"][0]["message_id"] == reply
        checked, _ = invoke(agents, ex, "watch_check", {})
        assert checked["checked"] == 0

    def test_a_reply_after_the_anchor_was_deleted_still_counts(self, agents, gmail):
        ex, _ = executor(gmail)
        watch, _ = invoke(agents, ex, "watch_await_reply", {"thread_id": "t-quote"})
        del gmail.messages[watch["since_message_id"]]
        reply = gmail.add_message("t-quote", SUPPLIER, GmailStub.ACCOUNT, "Re: Quotation", "Here.",
                                  "Fri, 04 Sep 2026 08:30:00 +0400")
        checked, _ = invoke(agents, ex, "watch_check", {})
        assert checked["answered"][0]["message_id"] == reply

    def test_a_gone_thread_closes_its_watch_and_the_others_go_on(self, agents, gmail):
        ex, provider = executor(gmail)
        gone, _ = invoke(agents, ex, "watch_await_reply", {"thread_id": "t-quote"})
        kept, _ = invoke(agents, ex, "watch_await_reply", {"thread_id": "t-office"})
        gmail.delete_thread("t-quote")
        gmail.busy = [429]   # and Gmail busy once on the way: waited out
        checked, status = invoke(agents, ex, "watch_check", {})
        assert status == "success", checked
        assert [c["watch_ref"] for c in checked["closed"]] == [gone["watch_ref"]]
        assert [u["watch_ref"] for u in checked["unanswered"]] == [kept["watch_ref"]]
        assert provider.data["watch"][gone["watch_ref"]]["keys"]["status"] == "closed"
        checked, _ = invoke(agents, ex, "watch_check", {})
        assert checked["closed"] == [] and checked["checked"] == 1

    def test_check_is_declared_schedulable(self, agents):
        function = agents["gmail"].function("watch_check")
        assert function["schedulable"] is True and function["level"] == 1


class TestInboxWatches:
    def test_new_mail_is_handed_on_once_from_history(self, agents, gmail):
        ex, _ = executor(gmail)
        watch, status = invoke(agents, ex, "watch_inbox", {"note": "act on it"})
        assert status == "success", watch
        quiet, _ = invoke(agents, ex, "watch_new_mail", {})
        assert (quiet["checked"], quiet["messages"], quiet["more"]) == (1, [], False)
        # Our own mail, from the account or its alias, and a draft are not news; a customer's is.
        gmail.add_message("t-office", GmailStub.ACCOUNT, CUSTOMER, "Re: New office", "Noted.", soon())
        gmail.add_message("t-office", GmailStub.ALIAS, CUSTOMER, "Re: New office", "Noted.", soon(), labels=("INBOX",))
        arrived = gmail.add_message("t-office", CUSTOMER, GmailStub.ACCOUNT, "Re: New office — dates",
                                    "Can we meet Tuesday?", soon(1))
        news, _ = invoke(agents, ex, "watch_new_mail", {})
        assert [m["message_id"] for m in news["messages"]] == [arrived], news
        assert news["messages"][0]["inbox_ref"] == watch["inbox_ref"] and news["more"] is False
        again, _ = invoke(agents, ex, "watch_new_mail", {})
        assert again["messages"] == []
        assert any(path.endswith("/history") for _, path in gmail.requests)

    def test_a_burst_is_handed_on_oldest_first_across_checks(self, agents, gmail):
        ex, _ = executor(gmail)
        watch, _ = invoke(agents, ex, "watch_inbox", {"only_from": "@harbourline.example"})
        wanted = []
        for n in range(5):
            wanted.append(gmail.add_message(f"t-b{n}", CUSTOMER, GmailStub.ACCOUNT, f"Desks {n}", "x", soon(n)))
            gmail.add_message(f"t-n{n}", SUPPLIER, GmailStub.ACCOUNT, f"Noise {n}", "x", soon(n))
        seen, more = [], True
        while more:
            page, _ = invoke(agents, ex, "watch_new_mail", {"inbox_ref": watch["inbox_ref"], "max_results": 2})
            seen += [m["message_id"] for m in page["messages"]]
            more = page["more"]
        assert seen == wanted

    def test_an_expired_history_id_reads_the_inbox_again_and_skips_nothing(self, agents, gmail):
        """Gmail keeps history for about a week. 1.x read at most 1,000 ids and dropped the rest of a long gap."""
        ex, provider = executor(gmail)
        watch, _ = invoke(agents, ex, "watch_inbox", {})
        arrived = [gmail.add_message(f"t-g{n}", CUSTOMER, GmailStub.ACCOUNT, f"Order {n}", "x", soon(n))
                   for n in range(1030)]
        gmail.expire_history()
        seen, runs, resynced = [], 0, []
        while True:
            runs += 1
            page, status = invoke(agents, ex, "watch_new_mail", {"max_results": 50})
            assert status == "success" and page["problems"] == [], page
            resynced += page.get("resynced", [])
            seen += [m["message_id"] for m in page["messages"]]
            if not page["more"]:
                break
        assert seen == arrived and resynced == [watch["inbox_ref"]]
        # Back on history from where the reading began: what comes next is handed on once.
        later = gmail.add_message("t-later", CUSTOMER, GmailStub.ACCOUNT, "Later", "x", soon(2000))
        page, _ = invoke(agents, ex, "watch_new_mail", {})
        assert [m["message_id"] for m in page["messages"]] == [later] and "resynced" not in page
        assert provider.data["inbox"][watch["inbox_ref"]]["keys"]["history_id"]

    def test_one_failing_watch_keeps_its_place_and_the_others_are_handed_on(self, agents, gmail):
        other = GmailStub(account="other@sidra.example", token="at-9").start()
        try:
            provider = Records(secrets={"google": [{"ref": "s1", **gmail.secret()}, {"ref": "s2", **other.secret()}]})
            ex = Chat(provider)
            first, _ = invoke(agents, ex, "watch_inbox", {"account": GmailStub.ACCOUNT})
            second, _ = invoke(agents, ex, "watch_inbox", {"account": "other@sidra.example"})
            mine = gmail.add_message("t-x", CUSTOMER, GmailStub.ACCOUNT, "To the first", "x", soon())
            theirs = other.add_message("t-y", CUSTOMER, "other@sidra.example", "To the second", "x", soon())
            kept = dict(provider.data["inbox"][second["inbox_ref"]]["keys"])
            other.token = "revoked"      # the second account's connection fails
            page, status = invoke(agents, ex, "watch_new_mail", {})
            assert status == "success", page
            assert [m["message_id"] for m in page["messages"]] == [mine]
            assert [p["watch_ref"] for p in page["problems"]] == [second["inbox_ref"]]
            assert page["problems"][0]["kind"] == "auth"
            assert provider.data["inbox"][second["inbox_ref"]]["keys"] == kept
            other.token = "at-9"
            page, _ = invoke(agents, ex, "watch_new_mail", {})
            assert [m["message_id"] for m in page["messages"]] == [theirs] and page["problems"] == []
        finally:
            other.stop()

    def test_a_1x_inbox_watch_goes_on_from_its_time(self, agents, gmail):
        ex, provider = executor(gmail)
        ref = provider.create_data("inbox", {"since": "2026-09-04T00:00:00Z", "cursor_ids": "", "status": "watching",
                                             "account": GmailStub.ACCOUNT})
        arrived = gmail.add_message("t-o", CUSTOMER, GmailStub.ACCOUNT, "Old style", "x",
                                    "Fri, 04 Sep 2026 09:00:00 +0400")
        page, status = invoke(agents, ex, "watch_new_mail", {})
        assert status == "success" and [m["message_id"] for m in page["messages"]] == [arrived], page
        assert provider.data["inbox"][ref]["keys"]["history_id"]

    def test_a_start_in_the_future_is_taken_as_now_and_a_bad_one_refused(self, agents, gmail):
        ex, provider = executor(gmail)
        watch, status = invoke(agents, ex, "watch_inbox", {"since": "2099-01-01T17:51:00"})
        assert status == "success" and "in the future" in watch["note"] and "+04:00" in watch["note"]
        past, _ = invoke(agents, ex, "watch_inbox", {"since": "2026-09-01T10:00:00+04:00"})
        assert past["since"] == "2026-09-01T06:00:00Z" and "note" not in past
        result, status = invoke(agents, ex, "watch_inbox", {"since": "yesterday"})
        assert status == "error" and result["kind"] == "invalid" and len(provider.data["inbox"]) == 2

    def test_watches_are_listed_and_closed(self, agents, gmail):
        ex, _ = executor(gmail)
        reply, _ = invoke(agents, ex, "watch_await_reply", {"thread_id": "t-quote"})
        inbox, _ = invoke(agents, ex, "watch_inbox", {"skip_from": "@northlight-seating.example"})
        listed, status = invoke(agents, ex, "watches_list", {})
        assert status == "success"
        assert [(w["watch_ref"], w["kind"]) for w in listed["watches"]] == [(reply["watch_ref"], "reply"),
                                                                            (inbox["inbox_ref"], "inbox")]
        assert listed["watches"][1]["skip_from"] == "@northlight-seating.example"
        closed, status = invoke(agents, ex, "watches_close", {"watch_ref": inbox["inbox_ref"]})
        assert status == "success" and closed["closed"] is True and closed["kind"] == "inbox"
        page, _ = invoke(agents, ex, "watch_new_mail", {})
        assert page["checked"] == 0
        listed, _ = invoke(agents, ex, "watches_list", {})
        assert [w["watch_ref"] for w in listed["watches"]] == [reply["watch_ref"]]
        missing, status = invoke(agents, ex, "watches_close", {"watch_ref": "data_999"})
        assert status == "error" and missing["kind"] == "not_found"

    def test_new_mail_is_declared_schedulable(self, agents):
        function = agents["gmail"].function("watch_new_mail")
        assert function["schedulable"] is True and function["level"] == 1


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, gmail):
        ex, provider = executor(gmail, access_token="expired")
        result, status = invoke(agents, ex, "search_find", {"query": "chairs"})
        assert status == "error" and result["kind"] == "auth" and "reconnect" in result["error"]
        result, status = invoke(agents, ex, "draft_compose", {"to": CUSTOMER, "subject": "x", "body": "y"}, 2)
        assert status == "error" and result["kind"] == "auth" and provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        result, status = invoke(agents, Chat(Records()), "account_status", {})
        assert status == "success" and result["connected"] is False and "No Google account" in result["problem"]

    def test_the_agent_never_mints_a_token(self, agents, gmail):
        ex, _ = executor(gmail)
        invoke(agents, ex, "search_find", {"query": "chairs", "max_results": 5})
        assert gmail.token_calls == 0
