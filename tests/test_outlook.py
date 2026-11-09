"""The Outlook agent, its program started and spoken to in the standard's messages, against a loopback Microsoft Graph holding
fictional mail for Sidra Office Supplies. The same cases
as the Gmail agent's suite: the contracts are the same, the wire is not.
"""

import pytest


from tests.harness import Chat, Records, probe, run
from tests.graph_stub import GraphStub

SUPPLIER = "quotes@northlight-seating.example"
QUOTE_PDF = b"%PDF-1.4 fictional quotation " + bytes(range(256))
CUSTOMER = "dana@harbourline.example"


@pytest.fixture
def graph():
    stub = GraphStub().start()
    stub.add_message("t-quote", SUPPLIER, GraphStub.ACCOUNT,
                     "Quotation Q-2041 — 20 office chairs",
                     "Please find our quotation attached. Delivery in 10 working days.",
                     "Thu, 03 Sep 2026 10:12:00 +0400",
                     attachments=[("Q-2041.pdf", "application/pdf", QUOTE_PDF)])
    stub.add_message("t-office", CUSTOMER, GraphStub.ACCOUNT,
                     "New office — furniture for 20 desks",
                     "We are moving in November and need desks and chairs for 20 people.",
                     "Wed, 02 Sep 2026 15:40:00 +0400")
    yield stub
    stub.stop()


def executor(graph, access_token="at-1"):
    provider = Records(secrets={"microsoft": graph.secret(access_token)})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["outlook"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["outlook"]
        assert probe(agent) == []

    def test_the_account_reports_its_address(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "account_status", {})
        assert (status, result) == ("success", {"connected": True,
                                                "email": GraphStub.ACCOUNT})


class TestSeveralAccounts:
    """Two connected accounts: the agent lists them, a function takes
    the one the user means, and a draft follows the account it was made
    on. With several, one must be named."""

    OTHER = "other@sidra.example"

    def executor(self, graph):
        # The second account's token is one the loopback service refuses:
        # a call that reaches it proves which account was used.
        provider = Records(secrets={"microsoft": [
            {"ref": "sec_first", **graph.secret()},
            {"ref": "sec_second", **graph.secret("at-2"), "account": self.OTHER},
        ]})
        return Chat(provider), provider

    def test_the_accounts_are_listed_without_a_token(self, agents, graph):
        ex, _ = self.executor(graph)
        result, status = invoke(agents, ex, "account_list", {})
        assert status == "success", result
        assert [a["account"] for a in result["accounts"]] == [GraphStub.ACCOUNT, self.OTHER]
        assert "at-1" not in str(result) and "at-2" not in str(result)

    def test_a_function_takes_the_account_the_user_means(self, agents, graph):
        ex, _ = self.executor(graph)
        # Unnamed: several are connected, and the answer names them.
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success" and result["connected"] is False, result
        assert GraphStub.ACCOUNT in result["problem"] and self.OTHER in result["problem"]
        # Named: that account answers.
        result, status = invoke(agents, ex, "account_status",
                                {"account": GraphStub.ACCOUNT})
        assert (status, result["connected"]) == ("success", True), result
        # Named: the other account's credential is the one used — its
        # token is refused by the service, which is the proof.
        result, status = invoke(agents, ex, "account_status",
                                {"account": self.OTHER})
        assert status == "success" and result["connected"] is False, result
        # Named wrongly: told which are connected.
        result, status = invoke(agents, ex, "search_find",
                                {"query": "chairs", "account": "nobody@x.example"})
        assert status == "error" and self.OTHER in result["error"], result

    def test_a_draft_is_sent_from_the_account_it_was_made_on(self, agents, graph):
        ex, provider = self.executor(graph)
        result, status = invoke(agents, ex, "draft_compose", {
            "account": GraphStub.ACCOUNT, "to": CUSTOMER, "subject": "Hello", "body": "Hi."})
        assert status == "success", result
        [draft] = provider.data["draft"].values()
        assert draft["keys"]["account"] == GraphStub.ACCOUNT


class TestReading:
    def test_find_returns_rows_with_thread_ids_and_links(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_find",
                                {"query": "from:northlight quotation"})
        assert status == "success", result
        assert result["total_estimate"] == 1
        row = result["messages"][0]
        assert row["thread_id"] == "t-quote"
        assert row["subject"].startswith("Quotation Q-2041")
        assert row["link"].endswith(row["message_id"])
        assert "next_page_token" not in result

    def test_find_paginates(self, agents, graph):
        for i in range(3):
            graph.add_message(f"t-{i}", SUPPLIER, GraphStub.ACCOUNT, f"Update {i}",
                              "chairs", "Fri, 04 Sep 2026 09:00:00 +0400")
        ex, _ = executor(graph)
        first, _ = invoke(agents, ex, "search_find",
                          {"query": "subject:Update", "max_results": 2})
        assert len(first["messages"]) == 2 and first["next_page_token"]
        assert first["total_estimate"] == 3
        second, _ = invoke(agents, ex, "search_find",
                           {"query": "subject:Update", "max_results": 2,
                            "page_token": first["next_page_token"]})
        ids = {m["message_id"] for m in first["messages"] + second["messages"]}
        assert len(ids) == 3 and "next_page_token" not in second

    def test_a_thread_is_read_whole_with_its_attachments(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_thread",
                                {"thread_id": "t-quote"})
        assert status == "success", result
        message = result["messages"][0]
        assert message["from"] == SUPPLIER
        assert "10 working days" in message["body"]
        assert message["truncated"] is False
        assert result["attachments"] == [{
            "message_id": "AAMk0001", "attachment_id": "att-AAMk0001-1",
            "filename": "Q-2041.pdf", "mime_type": "application/pdf",
            "size": len(QUOTE_PDF)}]

    def test_an_attachment_is_saved_as_a_file_of_bytes(self, agents, graph):
        ex, provider = executor(graph)
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": "AAMk0001", "attachment_id": "att-AAMk0001-1",
            "filename": "Q-2041.pdf"}, chat_level=2)
        assert status == "success", result
        assert ex.files[result["file_ref"]] == {"name": "Q-2041.pdf", "content": QUOTE_PDF}
        assert result["size"] == len(QUOTE_PDF)

    def _attachment_of(self, graph, filename):
        for ref, item in graph.attachments.items():
            if item["name"] == filename:
                return item["message_id"], ref
        raise AssertionError(f"no attachment {filename}")

    def test_an_attachment_too_big_to_keep_is_refused_not_attempted(
            self, agents, graph):
        """Past what the platform keeps as a chat's file, a sentence naming
        the size, before the bytes are fetched at all."""
        graph.add_message("t-big", SUPPLIER, GraphStub.ACCOUNT, "Large quote",
                          "See attached.", "Thu, 03 Sep 2026 10:12:00 +0400",
                          attachments=[("huge.pdf", "application/pdf", b"%PDF-1.4 x")])
        message_id, attachment_id = self._attachment_of(graph, "huge.pdf")
        big = 26 * 1024 * 1024
        graph.attachments[attachment_id]["size"] = big     # as Graph reports it
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": message_id, "attachment_id": attachment_id,
            "filename": "huge.pdf"}, chat_level=2)
        assert status == "error", result
        assert result["kind"] == "too_large"
        # It says how big it was and what the limit is, in the message.
        assert str(big) in result["error"].replace(",", "")
        assert ex.files == {}

    def test_a_cloud_link_attachment_is_named_for_what_it_is(self, agents, graph):
        """A reference attachment has no bytes. It used to come back as
        "Outlook returned an empty attachment", which reads like a fault."""
        graph.add_message("t-link", SUPPLIER, GraphStub.ACCOUNT, "Floor plans",
                          "Linked below.", "Thu, 03 Sep 2026 10:12:00 +0400",
                          attachments=[("plans.pdf", "application/pdf", b"", "reference")])
        message_id, attachment_id = self._attachment_of(graph, "plans.pdf")
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": message_id, "attachment_id": attachment_id,
            "filename": "plans.pdf"}, chat_level=2)
        assert status == "error", result
        assert result["kind"] == "unsupported"
        assert "link to cloud storage" in result["error"]

    def test_an_attached_message_is_named_for_what_it_is(self, agents, graph):
        graph.add_message("t-fwd", SUPPLIER, GraphStub.ACCOUNT, "FW: quotation",
                          "Forwarding this.", "Thu, 03 Sep 2026 10:12:00 +0400",
                          attachments=[("Original.eml", "message/rfc822", b"", "item")])
        message_id, attachment_id = self._attachment_of(graph, "Original.eml")
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_save_attachment", {
            "message_id": message_id, "attachment_id": attachment_id,
            "filename": "Original.eml"}, chat_level=2)
        assert status == "error", result
        assert result["kind"] == "unsupported"
        assert "message or calendar item" in result["error"]

    def test_a_long_thread_is_bounded_and_says_there_is_more(self, agents, graph):
        """An unbounded thread with full bodies overruns what the assistant
        can be shown, and the person is told something vague about their
        query. The agent returns less, and says so."""
        for i in range(6):
            graph.add_message("t-long", SUPPLIER, GraphStub.ACCOUNT,
                              "Re: chairs", f"Message number {i}.",
                              "Fri, 04 Sep 2026 09:00:00 +0400")
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_thread",
                                {"thread_id": "t-long", "max_messages": 3})
        assert status == "success", result
        assert result["message_count"] == 3
        assert len(result["messages"]) == 3
        assert result["more_messages"] is True

    def test_a_short_thread_says_there_is_no_more(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_thread",
                                {"thread_id": "t-quote", "max_messages": 3})
        assert status == "success", result
        assert result["message_count"] == 1
        assert result["more_messages"] is False

    def test_an_unknown_thread_is_named_not_invented(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "search_thread",
                                {"thread_id": "t-nope"})
        assert status == "error" and result["kind"] == "not_found"


class TestDrafting:
    def test_a_reply_is_an_outlook_draft_in_the_conversation_and_a_record(self, agents, graph):
        ex, provider = executor(graph)
        result, status = invoke(agents, ex, "draft_reply", {
            "thread_id": "t-quote",
            "body": "Thank you. Could delivery happen by Thursday 17 September?"})
        assert status == "success", result
        assert result["to"] == SUPPLIER
        assert result["subject"] == "Re: Quotation Q-2041 — 20 office chairs"
        draft = graph.drafts[result["outlook_draft_id"]]
        assert draft["conversationId"] == "t-quote"
        assert draft["inReplyTo"] == "<AAMk0001@stub.example>"
        assert draft["toRecipients"][0]["emailAddress"]["address"] == SUPPLIER
        assert "Thursday 17 September" in draft["body"]["content"]
        record = provider.data["draft"][result["draft_ref"]]
        assert record["keys"]["status"] == "prepared"
        assert record["keys"]["kind"] == "reply"
        assert graph.sends == []                       # nothing was sent

    def test_a_reply_to_our_own_last_message_goes_back_to_them(self, agents, graph):
        graph.add_message("t-quote", GraphStub.ACCOUNT, SUPPLIER,
                          "Re: Quotation Q-2041 — 20 office chairs",
                          "Noted, thanks.", "Thu, 03 Sep 2026 11:00:00 +0400")
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "draft_reply",
                                {"thread_id": "t-quote", "body": "One more question."})
        assert status == "success" and result["to"] == SUPPLIER

    def test_sending_needs_the_chats_trust_and_is_confirmed_by_outlook(self, agents, graph):
        ex, provider = executor(graph)
        drafted, _ = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "Your new office",
            "body": "Dana, we can furnish 20 desks by November."})
        # Level 3, chat at 1, nobody to approve: refused, nothing sent.
        refused, status = invoke(agents, ex, "draft_send",
                                 {"draft_ref": drafted["draft_ref"]})
        assert status == "error" and refused.get("denied") is True
        assert graph.sends == []

        sent, status = invoke(agents, ex, "draft_send",
                              {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success", sent
        assert sent["sent"] is True and sent["status"] == "sent"
        assert graph.messages[sent["message_id"]]["isDraft"] is False   # Outlook's word
        record = provider.data["draft"][drafted["draft_ref"]]
        assert record["keys"]["status"] == "sent"
        assert record["keys"]["sent_message_id"] == sent["message_id"]
        # Sent twice is refused.
        again, status = invoke(agents, ex, "draft_send",
                               {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and "already sent" in again["error"]

    def test_an_unanswered_send_is_unknown_and_never_retried(self, agents, graph):
        ex, provider = executor(graph)
        drafted, _ = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "Hello", "body": "Body."})
        graph.drop_send = True
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert "unknown" in result["error"]
        assert graph.sends == [drafted["outlook_draft_id"]]        # exactly once
        record = provider.data["draft"][drafted["draft_ref"]]
        assert record["keys"]["status"] == "unknown"
        # And the function refuses to try again on its own.
        graph.drop_send = False
        result, status = invoke(agents, ex, "draft_send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert graph.sends == [drafted["outlook_draft_id"]]

    def test_a_discarded_draft_is_gone_from_outlook(self, agents, graph):
        ex, provider = executor(graph)
        drafted, _ = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "Hello", "body": "Body."})
        result, status = invoke(agents, ex, "draft_discard",
                                {"draft_ref": drafted["draft_ref"]})
        assert (status, result) == ("success", {"discarded": True})
        assert drafted["outlook_draft_id"] not in graph.messages
        assert provider.data["draft"][drafted["draft_ref"]]["keys"]["status"] == "discarded"


class TestWatching:
    def test_a_watch_reports_only_while_the_reply_is_missing(self, agents, graph):
        ex, _ = executor(graph)
        watch, status = invoke(agents, ex, "watch_await_reply",
                               {"thread_id": "t-quote", "note": "delivery by Thursday?"})
        assert status == "success", watch
        assert watch["since_message_id"] == "AAMk0001"

        # Our own follow-up is not a reply.
        graph.add_message("t-quote", GraphStub.ACCOUNT, SUPPLIER,
                          "Re: Quotation Q-2041 — 20 office chairs",
                          "Could delivery happen by Thursday?", "Thu, 03 Sep 2026 12:00:00 +0400")
        checked, status = invoke(agents, ex, "watch_check", {})
        assert status == "success", checked
        assert checked["answered"] == []
        assert checked["unanswered"] == [{
            "watch_ref": watch["watch_ref"], "thread_id": "t-quote",
            "subject": "Quotation Q-2041 — 20 office chairs",
            "note": "delivery by Thursday?"}]

        # The supplier answers: unanswered empties, the watch settles.
        reply_id = graph.add_message("t-quote", SUPPLIER, GraphStub.ACCOUNT,
                                     "Re: Quotation Q-2041 — 20 office chairs",
                                     "Yes, Thursday works.", "Fri, 04 Sep 2026 08:30:00 +0400")
        checked, _ = invoke(agents, ex, "watch_check", {})
        assert checked["unanswered"] == []
        assert checked["answered"][0]["message_id"] == reply_id
        assert checked["answered"][0]["reply_from"] == SUPPLIER
        checked, _ = invoke(agents, ex, "watch_check", {})
        assert checked == {"checked": 0, "unanswered": [], "answered": []}

    def test_check_is_declared_schedulable(self, agents):
        function = agents["outlook"].function("watch_check")
        assert function["schedulable"] is True
        assert function["level"] == 1


class TestWatchingTheInbox:
    def test_the_inbox_watch_hands_on_only_what_arrived_since(self, agents, graph):
        ex, _ = executor(graph)
        watch, status = invoke(agents, ex, "watch_inbox", {"note": "act on it"})
        assert status == "success", watch
        # From now, in the mailbox's clock: the newest message marks the
        # spot and is not news itself.
        assert watch["since"] == "2026-09-03T06:12:00Z"

        quiet, status = invoke(agents, ex, "watch_new_mail", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "messages": [], "more": False}

        # Our own mail is not news; a customer's is.
        graph.add_message("t-office", GraphStub.ACCOUNT, CUSTOMER,
                          "Re: New office", "Noted.", "Fri, 04 Sep 2026 08:00:00 +0400")
        arrived = graph.add_message("t-office", CUSTOMER, GraphStub.ACCOUNT,
                                    "Re: New office — dates",
                                    "Can we meet Tuesday?", "Fri, 04 Sep 2026 09:00:00 +0400")
        news, _ = invoke(agents, ex, "watch_new_mail", {})
        assert [m["message_id"] for m in news["messages"]] == [arrived]
        row = news["messages"][0]
        assert (row["thread_id"], row["inbox_ref"]) == ("t-office", watch["inbox_ref"])
        assert row["from"] == CUSTOMER and row["subject"] == "Re: New office — dates"
        assert news["more"] is False

        # Handed on once: the cursor moved.
        again, _ = invoke(agents, ex, "watch_new_mail", {})
        assert again["messages"] == []

        # A second mail in the very same second is neither lost nor
        # shown twice — the ids on the cursor's second are remembered.
        twin = graph.add_message("t-new", SUPPLIER, GraphStub.ACCOUNT,
                                 "Price list", "Attached.", "Fri, 04 Sep 2026 09:00:00 +0400")
        news, _ = invoke(agents, ex, "watch_new_mail", {})
        assert [m["message_id"] for m in news["messages"]] == [twin]
        again, _ = invoke(agents, ex, "watch_new_mail", {})
        assert again["messages"] == []

    def test_a_start_in_the_future_is_taken_as_now_and_said(self, agents, graph):
        """The chat shows the model every time in the person's zone, so
        "17:51" arrives meaning Dubai and is read as UTC — four hours
        ahead. A watch that starts later tonight finds nothing until
        then, run after run, and says nothing is wrong."""
        ex, _ = executor(graph)
        watch, status = invoke(agents, ex, "watch_inbox",
                               {"since": "2099-01-01T17:51:00"})
        assert status == "success", watch
        assert watch["since"] == "2026-09-03T06:12:00Z"       # from now, not from 2099
        assert "in the future" in watch["note"] and "+04:00" in watch["note"]

        # A past start with its offset is still honoured, and converted.
        past, status = invoke(agents, ex, "watch_inbox",
                              {"since": "2026-09-01T10:00:00+04:00"})
        assert status == "success", past
        assert past["since"] == "2026-09-01T06:00:00Z" and "note" not in past

    def test_the_inbox_watch_narrows_senders_and_pages(self, agents, graph):
        ex, _ = executor(graph)
        watch, status = invoke(agents, ex, "watch_inbox", {
            "since": "2026-09-04T00:00:00+00:00",
            "only_from": "@harbourline.example"})
        assert status == "success", watch
        assert watch["since"] == "2026-09-04T00:00:00Z"

        first = graph.add_message("t-office", CUSTOMER, GraphStub.ACCOUNT,
                                  "Desks", "First.", "Fri, 04 Sep 2026 09:00:00 +0400")
        graph.add_message("t-quote", SUPPLIER, GraphStub.ACCOUNT,
                          "Chairs", "Noise.", "Fri, 04 Sep 2026 09:05:00 +0400")
        second = graph.add_message("t-office", CUSTOMER, GraphStub.ACCOUNT,
                                   "Desks again", "Second.", "Fri, 04 Sep 2026 09:10:00 +0400")

        # Two at a time: the first page holds the customer and the
        # supplier; only the customer is handed on, and "more" is true.
        page, _ = invoke(agents, ex, "watch_new_mail",
                         {"inbox_ref": watch["inbox_ref"], "max_results": 2})
        assert [m["message_id"] for m in page["messages"]] == [first]
        assert page["more"] is True
        page, _ = invoke(agents, ex, "watch_new_mail",
                         {"inbox_ref": watch["inbox_ref"], "max_results": 2})
        assert [m["message_id"] for m in page["messages"]] == [second]
        assert page["more"] is False

    def test_a_bad_since_is_refused_and_records_nothing(self, agents, graph):
        ex, provider = executor(graph)
        result, status = invoke(agents, ex, "watch_inbox", {"since": "yesterday"})
        assert status == "error" and result["kind"] == "invalid"
        assert provider.data == {}

    def test_new_mail_is_declared_schedulable(self, agents):
        function = agents["outlook"].function("watch_new_mail")
        assert function["schedulable"] is True
        assert function["level"] == 1


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, graph):
        ex, provider = executor(graph, access_token="expired")
        result, status = invoke(agents, ex, "search_find", {"query": "chairs"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "draft_compose", {
            "to": CUSTOMER, "subject": "x", "body": "y"})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = Chat(Records())
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success" and result["connected"] is False
        assert "No Microsoft account" in result["problem"]

    def test_the_agent_never_mints_a_token(self, agents, graph):
        ex, _ = executor(graph)
        invoke(agents, ex, "search_find", {"query": "chairs", "max_results": 5})
        assert graph.token_calls == 0


class TestAPageTokenIsGraphsOwn:
    @pytest.mark.parametrize("token", ["http://graph.microsoft.com/v1.0/me/messages?$skip=10",
                                       "https://contoso.sharepoint.com/x", "https://graph.microsoft.com.evil.example/"])
    def test_one_pointing_elsewhere_is_refused_before_it_is_sent(self, token):
        from agents.outlook.tools.graph import GraphClient, GraphError

        with pytest.raises(GraphError, match="not one Microsoft gave"):
            GraphClient({"access_token": "at-1"})._request("GET", "", absolute=token)
