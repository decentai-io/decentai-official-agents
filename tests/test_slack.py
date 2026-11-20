"""The Slack agent, its program started and spoken to in the standard's
messages, against a loopback Slack Web API: the Sidra Office Supplies
workspace, with #general, #showroom, a private
#finance, DMs with Dana and Omar, and a group DM with Omar and Priya.
"""

import pytest


from tests.harness import Chat, Records, probe, run
from tests.conftest import resource
from tests.slack_stub import (DANA, DM_DANA, DM_OMAR, FINANCE, GENERAL, GROUP, ME, OMAR, OLD_TOKEN,
                              PRIYA, REVOKED_TOKEN, SHOWROOM, TEAM_URL, SlackStub)
from standard.http import FILE_MOST


@pytest.fixture
def slack():
    stub = SlackStub().start()
    yield stub
    stub.stop()


def executor(slack, access_token="xoxp-demo"):
    provider = Records(secrets={"slack": slack.secret(access_token)})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["slack"], name, inputs, chat_level=chat_level))


def ok(agents, ex, name, inputs, chat_level=1):
    result, status = invoke(agents, ex, name, inputs, chat_level)
    assert status == "success", result
    return result


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["slack"]
        assert probe(agent) == []

    def test_only_sending_is_level_three_and_the_clock_runs_the_watch(self, agents):
        functions = {f["id"]: f for f in agents["slack"].manifest["functions"]}
        assert {n for n, f in functions.items() if f["level"] == 3} == {
            "messages_send", "messages_direct", "reactions_add", "reactions_remove"}
        assert functions["files_read"]["level"] == 0 and functions["watches_list"]["level"] == 0
        assert functions["watches_close"]["level"] == 1
        assert {n for n, f in functions.items() if f.get("schedulable")} == {"watch_new"}
        assert functions["watch_new"]["level"] == 1
        assert "model" not in functions["watch_new"]["calls"]
        assert functions["watch_start"]["level"] == 1

    def test_it_signs_in_for_a_user_token(self, agents):
        oauth = resource(agents["slack"].manifest, "secrets", "slack")["oauth"]
        assert (oauth["provider"], oauth["scope_param"], oauth["scope_separator"],
                oauth["token_path"]) == ("slack", "user_scope", ",", "authed_user")
        assert {"search:read", "chat:write", "im:history", "im:write", "files:read", "reactions:write",
                "users:read.email"} <= set(oauth["scopes"])
        # Asked only for what a function uses: no mpim:write (DMs open with one person), no reactions:read.
        assert not {"mpim:write", "reactions:read", "files:write"} & set(oauth["scopes"])

    def test_every_function_takes_an_account_but_closing_by_ref(self, agents):
        for function in agents["slack"].manifest["functions"]:
            if function["id"] != "watches_close":
                assert "account" in function["inputs"]["properties"], function["id"]

    def test_sent_text_and_watch_notes_are_values_not_keys(self, agents):
        manifest = agents["slack"].manifest
        sent = {f["name"]: f for f in resource(manifest, "data", "sent")["fields"]}
        watch = {f["name"]: f for f in resource(manifest, "data", "watch")["fields"]}
        assert sent["text"]["storage"] == "values"
        assert watch["note"]["storage"] == "values" and watch["state"]["storage"] == "values"

    def test_status_says_whose_account(self, agents, slack):
        ex, _ = executor(slack)
        status = ok(agents, ex, "account_status", {})
        assert {k: status[k] for k in ("connected", "team", "user", "user_id")} == {
            "connected": True, "team": "Sidra Office Supplies", "user": "demo", "user_id": ME}
        assert [a["account"] for a in status["accounts"]] == ["demo"]


class TestReading:
    def test_conversations_are_named_for_people(self, agents, slack):
        ex, _ = executor(slack)
        listed = ok(agents, ex, "conversations_list", {})
        rows = {c["conversation_id"]: (c["kind"], c["name"]) for c in listed["conversations"]}
        assert rows == {
            GENERAL: ("channel", "general"), SHOWROOM: ("channel", "showroom"),
            FINANCE: ("private_channel", "finance"), DM_DANA: ("dm", "Dana Harbour"),
            DM_OMAR: ("dm", "Omar Nasser"), GROUP: ("group_dm", "Omar Nasser, Priya Menon")}
        assert listed["more"] is False
        dms = ok(agents, ex, "conversations_list", {"kind": "dm", "max_results": 1})
        assert len(dms["conversations"]) == 1 and dms["more"] is True

    def test_a_conversation_reads_oldest_first_as_text(self, agents, slack):
        ex, _ = executor(slack)
        read = ok(agents, ex, "conversations_messages", {"conversation_id": SHOWROOM})
        assert read["name"] == "showroom" and read["more"] is False
        first, second = read["messages"]
        assert (first["author"], first["mine"], first["reply_count"]) == ("Demo", True, 1)
        assert first["ts"] == slack.chairs_ts
        assert first["link"] == (f"{TEAM_URL}archives/{SHOWROOM}/"
                                 f"p{slack.chairs_ts.replace('.', '')}")
        assert second["author"] == "Dana Harbour"
        assert second["text"] == "Harbourline want the layout signed off & the drawings by Thursday."
        # Only the newest one, and it says there is more.
        latest = ok(agents, ex, "conversations_messages",
                    {"conversation_id": SHOWROOM, "max_results": 1})
        assert [m["author"] for m in latest["messages"]] == ["Dana Harbour"]
        assert latest["more"] is True

    def test_join_notices_are_left_out_and_mentions_read_as_names(self, agents, slack):
        ex, _ = executor(slack)
        general = ok(agents, ex, "conversations_messages", {"conversation_id": GENERAL})
        assert [m["text"] for m in general["messages"]] == ["Office closed on the 24th for the move."]
        group = ok(agents, ex, "conversations_messages", {"conversation_id": GROUP})
        assert group["messages"][0]["text"] == "@Demo can you check the Cloudledger export?"

    def test_a_thread_reads_parent_then_replies(self, agents, slack):
        ex, _ = executor(slack)
        thread = ok(agents, ex, "conversations_replies",
                    {"conversation_id": SHOWROOM, "thread_ts": slack.chairs_ts})
        assert [(m["author"], m["text"]) for m in thread["messages"]] == [
            ("Demo", "Has Northlight confirmed the chair delivery?"),
            ("Omar Nasser", "Not yet, chasing them today.")]
        reply = thread["messages"][1]
        assert reply["thread_ts"] == slack.chairs_ts and "?thread_ts=" in reply["link"]

    def test_search_finds_newest_first_with_where(self, agents, slack):
        ex, _ = executor(slack)
        found = ok(agents, ex, "messages_search", {"query": "drawings"})
        assert [(m["conversation_id"], m["conversation"], m["kind"]) for m in found["messages"]] == [
            (DM_DANA, "Dana Harbour", "dm"), (SHOWROOM, "showroom", "channel")]
        assert found["total"] == 2 and found["more"] is False
        assert found["messages"][0]["author"] == "Dana Harbour"

    def test_an_unknown_conversation_is_named_not_invented(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "conversations_messages",
                                {"conversation_id": "C0NOPE"})
        assert status == "error" and result["kind"] == "not_found"

    def test_a_ts_that_is_not_one_is_refused(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "conversations_replies",
                                {"conversation_id": SHOWROOM, "thread_ts": "yesterday"})
        assert status == "error" and result["kind"] == "invalid"


class TestAnswered:
    def test_a_reply_in_the_thread_is_an_answer(self, agents, slack):
        ex, _ = executor(slack)
        answered = ok(agents, ex, "messages_answered",
                      {"conversation_id": SHOWROOM, "ts": slack.chairs_ts})
        assert answered["answered"] is True and answered["by"] == ["Omar Nasser", "Dana Harbour"]
        assert [(r["where"], r["author"]) for r in answered["replies"]] == [
            ("thread", "Omar Nasser"), ("conversation", "Dana Harbour")]

    def test_only_someone_else_writing_later_counts(self, agents, slack):
        ex, _ = executor(slack)
        mine = slack.say(DM_OMAR, ME, "Did the chairs ship?")
        slack.say(DM_OMAR, ME, "Any news?")
        before = ok(agents, ex, "messages_answered", {"conversation_id": DM_OMAR, "ts": mine})
        assert (before["answered"], before["by"], before["replies"]) == (False, [], [])
        slack.say(DM_OMAR, OMAR, "Yes, they left this morning.", thread_ts=mine)
        after = ok(agents, ex, "messages_answered", {"conversation_id": DM_OMAR, "ts": mine})
        assert after["answered"] is True and after["by"] == ["Omar Nasser"]
        assert after["replies"][0]["text"] == "Yes, they left this morning."

    def test_a_message_that_is_not_there_is_not_found(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "messages_answered",
                                {"conversation_id": SHOWROOM, "ts": "1700000000.000001"})
        assert status == "error" and result["kind"] == "not_found"


class TestSending:
    def test_a_send_posts_as_the_person_and_is_recorded(self, agents, slack):
        ex, provider = executor(slack)
        sent = ok(agents, ex, "messages_send", {
            "conversation_id": SHOWROOM, "thread_ts": slack.chairs_ts,
            "text": "The drawings are coming Thursday."}, chat_level=3)
        assert slack.posts == [{"channel": SHOWROOM, "text": "The drawings are coming Thursday.",
                                "thread_ts": slack.chairs_ts}]
        posted = slack.find(SHOWROOM, sent["ts"])
        assert posted["user"] == ME and posted["thread_ts"] == slack.chairs_ts
        record = provider.data["sent"][sent["sent_ref"]]["keys"]
        assert (record["conversation_id"], record["conversation"], record["ts"],
                record["thread_ts"]) == (SHOWROOM, "showroom", sent["ts"], slack.chairs_ts)
        assert record["link"] == sent["link"] and "?thread_ts=" in sent["link"]
        assert "text" not in record
        assert provider.data["sent"][sent["sent_ref"]]["values"]["text"] == "The drawings are coming Thursday."

    def test_sending_needs_the_chats_trust(self, agents, slack):
        ex, provider = executor(slack)
        result, status = invoke(agents, ex, "messages_send",
                                {"conversation_id": DM_DANA, "text": "Hi"}, chat_level=1)
        assert status == "error" and slack.posts == [] and provider.data == {}

    def test_a_send_that_gets_no_answer_is_unknown_and_not_retried(self, agents, slack):
        slack.drop_send = True
        ex, provider = executor(slack)
        result, status = invoke(agents, ex, "messages_send", {
            "conversation_id": DM_DANA, "text": "The drawings are coming Thursday."},
            chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert len(slack.posts) == 1 and slack.calls.count("chat.postMessage") == 1
        assert not provider.data.get("sent")

    def test_an_unknown_conversation_is_refused_before_anything_is_posted(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "messages_send",
                                {"conversation_id": "C0NOPE", "text": "Hi"}, chat_level=3)
        assert status == "error" and result["kind"] == "not_found"
        assert slack.posts == []


class TestWatching:
    def test_a_mention_and_a_dm_are_handed_on_exactly_once(self, agents, slack):
        ex, provider = executor(slack)
        watch = ok(agents, ex, "watch_start", {"note": "tell me what needs an answer"})
        assert watch["which"] == "both"
        # What was already there is not news.
        quiet = ok(agents, ex, "watch_new", {})
        assert quiet == {"checked": 1, "messages": [], "more": False, "unchecked": 0, "problems": [],
                         "closed": []}

        slack.say(SHOWROOM, ME, f"<@{ME}> note to self")                  # our own
        slack.say(DM_DANA, ME, "Sending them now.")                       # our own
        mention = slack.say(SHOWROOM, PRIYA, f"<@{ME}> can you approve the layout?",
                            thread_ts=slack.chairs_ts)
        dm = slack.say(DM_DANA, DANA, "Thanks! Are the chairs sorted?")
        slack.say(GENERAL, OMAR, "Lunch is here.")                        # neither

        news = ok(agents, ex, "watch_new", {})
        assert [(m["kind"], m["conversation_id"], m["ts"]) for m in news["messages"]] == [
            ("mention", SHOWROOM, mention), ("dm", DM_DANA, dm)]
        first, second = news["messages"]
        assert (first["author"], first["text"]) == ("Priya Menon", "@Demo can you approve the layout?")
        assert first["thread_ts"] == slack.chairs_ts and first["conversation"] == "showroom"
        assert (second["author"], second["conversation"]) == ("Dana Harbour", "Dana Harbour")
        assert first["watch_ref"] == watch["watch_ref"] and news["more"] is False
        kept = provider.data["watch"][watch["watch_ref"]]
        assert kept["keys"]["mention_after"] == mention
        assert kept["values"]["state"]["dms"][DM_DANA][0] == dm

        again = ok(agents, ex, "watch_new", {})
        assert again["messages"] == []

    def test_a_group_dm_counts_and_a_mention_in_a_dm_is_one_row(self, agents, slack):
        ex, _ = executor(slack)
        ok(agents, ex, "watch_start", {"which": "both"})
        said = slack.say(GROUP, PRIYA, f"<@{ME}> the export is ready")
        news = ok(agents, ex, "watch_new", {})
        assert [(m["kind"], m["ts"], m["conversation"]) for m in news["messages"]] == [
            ("dm", said, "Omar Nasser, Priya Menon")]

    def test_mentions_only_and_conversations_left_out(self, agents, slack):
        ex, _ = executor(slack)
        watch = ok(agents, ex, "watch_start", {"which": "mentions",
                                                     "skip_conversations": GENERAL})
        slack.say(DM_DANA, DANA, "Quick question")
        slack.say(GENERAL, OMAR, f"<@{ME}> everyone, the office moves on the 24th")
        wanted = slack.say(FINANCE, PRIYA, f"<@{ME}> the Cloudledger invoice needs you")
        news = ok(agents, ex, "watch_new", {"watch_ref": watch["watch_ref"]})
        assert [(m["kind"], m["ts"]) for m in news["messages"]] == [("mention", wanted)]

    def test_a_busy_check_pages_and_says_more(self, agents, slack):
        ex, _ = executor(slack)
        watch = ok(agents, ex, "watch_start", {"which": "dms"})
        said = [slack.say(DM_DANA, DANA, f"Update {n}") for n in range(3)]
        page = ok(agents, ex, "watch_new", {"max_results": 2})
        assert [m["ts"] for m in page["messages"]] == said[:2] and page["more"] is True
        page = ok(agents, ex, "watch_new", {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [m["ts"] for m in page["messages"]] == said[2:] and page["more"] is False

    def test_a_bad_skip_list_is_refused_and_records_nothing(self, agents, slack):
        ex, provider = executor(slack)
        result, status = invoke(agents, ex, "watch_start",
                                {"skip_conversations": "#general"})
        assert status == "error" and result["kind"] == "invalid"
        assert provider.data == {}


class TestTheConnection:
    def test_a_long_rate_limit_is_reported_with_the_wait(self, agents, slack):
        ex, _ = executor(slack)
        slack.rate_limit, slack.rate_wait = 1, "60"
        result, status = invoke(agents, ex, "conversations_list", {})
        assert status == "error" and result["kind"] == "rate"
        assert "60 seconds" in result["error"]

    def test_a_short_rate_limit_is_waited_out(self, agents, slack):
        ex, _ = executor(slack)
        slack.rate_limit, slack.rate_wait = 1, "1"
        listed = ok(agents, ex, "conversations_list", {"kind": "channel"})
        assert {c["conversation_id"] for c in listed["conversations"]} == {GENERAL, SHOWROOM}

    def test_a_write_refused_as_busy_is_sent_once_when_slack_allows(self, agents, slack):
        ex, _ = executor(slack)
        ok(agents, ex, "account_status", {})
        slack.rate_limit, slack.rate_wait, slack.rate_only = 1, "1", "chat.postMessage"
        ok(agents, ex, "messages_send", {"conversation_id": DM_DANA, "text": "On my way."}, chat_level=3)
        assert len(slack.posts) == 1 and slack.calls.count("chat.postMessage") == 2

    def test_a_connection_without_a_new_scope_says_reconnect(self, agents, slack):
        ex, _ = executor(slack, access_token=OLD_TOKEN)
        result, status = invoke(agents, ex, "reactions_add",
                                {"conversation_id": SHOWROOM, "ts": slack.chairs_ts, "emoji": "eyes"}, 3)
        assert status == "error" and result["kind"] == "scope"
        assert "reactions:write" in result["error"] and "Reconnect" in result["error"]

    def test_a_revoked_token_says_reconnect_and_writes_nothing(self, agents, slack):
        ex, provider = executor(slack, access_token=REVOKED_TOKEN)
        result, status = invoke(agents, ex, "conversations_list", {})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "watch_start", {})
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "messages_send",
                                {"conversation_id": DM_DANA, "text": "Hi"}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {} and slack.posts == []

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = Chat(Records())
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success" and result["connected"] is False
        assert "No Slack account" in result["problem"]
        result, status = invoke(agents, ex, "watch_new", {})
        assert status == "error" and result["kind"] == "auth"


class TestReadingMore:
    def test_with_oldest_alone_the_first_messages_after_it_come(self, agents, slack):
        ex, _ = executor(slack)
        start = slack.say(DM_OMAR, OMAR, "First")
        said = [slack.say(DM_OMAR, OMAR, f"Then {n}") for n in range(4)]
        read = ok(agents, ex, "conversations_messages",
                  {"conversation_id": DM_OMAR, "oldest": start, "max_results": 2})
        # Not the newest two: the two right after oldest, and more after them.
        assert [m["ts"] for m in read["messages"]] == said[:2] and read["more"] is True

    def test_arabic_text_survives_reading_and_sending(self, agents, slack):
        ex, provider = executor(slack)
        slack.say(DM_DANA, DANA, "هل وصلت الكراسي؟")
        read = ok(agents, ex, "conversations_messages", {"conversation_id": DM_DANA, "max_results": 1})
        assert read["messages"][0]["text"] == "هل وصلت الكراسي؟"
        sent = ok(agents, ex, "messages_send", {"conversation_id": DM_DANA, "text": "نعم، وصلت اليوم."}, 3)
        assert slack.find(DM_DANA, sent["ts"])["text"] == "نعم، وصلت اليوم."
        assert provider.data["sent"][sent["sent_ref"]]["values"]["text"] == "نعم، وصلت اليوم."


class TestPeopleAndDirectMessages:
    def test_a_person_is_found_by_address_or_name(self, agents, slack):
        ex, _ = executor(slack)
        by_address = ok(agents, ex, "people_find", {"who": "Priya@Sidra.example"})
        assert [p["user_id"] for p in by_address["people"]] == [PRIYA]
        by_name = ok(agents, ex, "people_find", {"who": "omar"})
        assert [p["user_id"] for p in by_name["people"]] == [OMAR]

    def test_a_direct_message_by_address_opens_the_conversation(self, agents, slack):
        ex, provider = executor(slack)
        sent = ok(agents, ex, "messages_direct", {"to": "priya@sidra.example", "text": "Samples arrived."}, 3)
        assert sent["user_id"] == PRIYA and sent["conversation_id"] == "D0PRIYA"
        assert slack.calls.count("conversations.open") == 1
        assert slack.find("D0PRIYA", sent["ts"])["text"] == "Samples arrived."
        assert provider.data["sent"][sent["sent_ref"]]["keys"]["conversation"] == "Priya Menon"

    def test_a_name_several_people_share_is_asked_never_picked(self, agents, slack):
        slack.add_user("U0DANA2", "dana.k", "Dana Karimi", "dana.k@sidra.example")
        ex, provider = executor(slack)
        result, status = invoke(agents, ex, "messages_direct", {"to": "Dana", "text": "Hi"}, 3)
        assert status == "error" and result["kind"] == "invalid"
        assert "Dana" in result["error"] and "2 people" in result["error"]
        assert slack.posts == [] and "conversations.open" not in slack.calls and not provider.data.get("sent")
        found = ok(agents, ex, "people_find", {"who": "Dana"})
        assert {p["user_id"] for p in found["people"]} == {DANA, "U0DANA2"}

    def test_an_address_nobody_has_is_not_found(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "messages_direct", {"to": "nobody@sidra.example", "text": "Hi"}, 3)
        assert status == "error" and result["kind"] == "not_found" and slack.posts == []


class TestReactions:
    def test_a_reaction_is_added_once_and_taken_back(self, agents, slack):
        ex, _ = executor(slack)
        where = {"conversation_id": SHOWROOM, "ts": slack.chairs_ts, "emoji": ":white_check_mark:"}
        assert ok(agents, ex, "reactions_add", where, 3)["changed"] is True
        assert ok(agents, ex, "reactions_add", where, 3)["changed"] is False
        assert slack.reactions[(SHOWROOM, slack.chairs_ts)] == ["white_check_mark"]
        assert ok(agents, ex, "reactions_remove", where, 3)["changed"] is True
        assert slack.reactions[(SHOWROOM, slack.chairs_ts)] == []

    def test_reacting_needs_the_chats_trust(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "reactions_add",
                                {"conversation_id": SHOWROOM, "ts": slack.chairs_ts, "emoji": "eyes"}, 1)
        assert status == "error" and not slack.reactions


class TestFiles:
    def test_a_shared_file_comes_into_the_chat_with_its_arabic_name(self, agents, slack):
        content = "البند,المبلغ\nكراسي,1200\n".encode("utf-8")
        slack.add_file("F0QUOTE", "عرض السعر.csv", content, mimetype="text/csv")
        slack.say(FINANCE, OMAR, "The quote", files=["F0QUOTE"])
        ex, _ = executor(slack)
        read = ok(agents, ex, "conversations_messages", {"conversation_id": FINANCE, "max_results": 1})
        assert read["messages"][0]["files"] == [
            {"file_id": "F0QUOTE", "name": "عرض السعر.csv", "size": len(content), "type": "text/csv"}]
        got = ok(agents, ex, "files_read", {"file_id": "F0QUOTE"})
        assert got["name"] == "عرض السعر.csv" and got["size"] == len(content)
        assert ex.files[got["file"]]["content"] == content and ex.files[got["file"]]["name"] == "عرض السعر.csv"

    def test_a_file_over_the_cap_is_refused_before_download_with_its_link(self, agents, slack):
        slack.add_file("F0BIG", "showroom.mov", b"x", size=FILE_MOST + 1)
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "files_read", {"file_id": "F0BIG"})
        assert status == "error" and result["kind"] == "too_large"
        assert "files/" in result["error"] and slack.downloads == []

    def test_a_file_that_lies_about_its_size_is_cut_off(self, agents, slack):
        slack.add_file("F0LIAR", "dump.bin", b"y" * (FILE_MOST + 10), size=10)
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "files_read", {"file_id": "F0LIAR"})
        assert status == "error" and result["kind"] == "too_large"

    def test_a_file_kept_elsewhere_never_gets_the_token(self, agents, slack):
        slack.add_file("F0DRIVE", "plan.gdoc", b"", is_external=True, mode="external",
                       url_private_download="https://docs.google.com/x")
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "files_read", {"file_id": "F0DRIVE"})
        assert status == "error" and result["kind"] == "invalid" and slack.downloads == []


class TestTheWatchUnderSlacksLimits:
    def test_a_mention_search_indexed_late_still_arrives_once(self, agents, slack):
        ex, _ = executor(slack)
        ok(agents, ex, "watch_start", {"which": "mentions"})
        late = slack.say(SHOWROOM, PRIYA, f"<@{ME}> the layout, please")
        slack.unindexed.add(late)
        early = slack.say(GENERAL, OMAR, f"<@{ME}> lunch?")
        first = ok(agents, ex, "watch_new", {})
        assert [m["ts"] for m in first["messages"]] == [early]
        slack.unindexed.clear()
        second = ok(agents, ex, "watch_new", {})
        assert [m["ts"] for m in second["messages"]] == [late]
        assert ok(agents, ex, "watch_new", {})["messages"] == []

    def test_every_dm_conversation_is_read_past_forty(self, agents, slack):
        ex, _ = executor(slack)
        ok(agents, ex, "watch_start", {"which": "dms"})
        said = set()
        for n in range(45):
            user = f"U0P{n:03d}"
            slack.add_user(user, f"p{n}", f"Person {n}", f"p{n}@sidra.example")
            slack.add_conversation(f"D0P{n:03d}", user, is_im=True, user=user)
            said.add(slack.say(f"D0P{n:03d}", user, f"Hello {n}"))
        handed, runs = [], 0
        while runs < 5:
            runs += 1
            news = ok(agents, ex, "watch_new", {"max_results": 25})
            handed += [m["ts"] for m in news["messages"]]
            if not news["more"]:
                break
        assert sorted(handed) == sorted(said) and len(handed) == len(set(handed)) and runs == 2

    def test_a_burst_bigger_than_a_page_loses_nothing_at_one_request_a_minute(self, agents, slack):
        ex, _ = executor(slack)
        ok(agents, ex, "watch_start", {"which": "dms"})
        slack.restricted = True
        said = [slack.say(DM_OMAR, OMAR, f"Line {n}") for n in range(20)]
        handed = []
        for _ in range(6):
            slack.minute_passed()
            news = ok(agents, ex, "watch_new", {"max_results": 25})
            assert news["problems"] == []
            handed += [m["ts"] for m in news["messages"]]
            if not news["more"]:
                break
        assert handed == said

    def test_a_rate_limit_on_dms_keeps_their_place_and_still_hands_on_mentions(self, agents, slack):
        ex, provider = executor(slack)
        watch = ok(agents, ex, "watch_start", {})
        slack.restricted = True
        dm = slack.say(DM_DANA, DANA, "Are you free?")
        other = slack.say(DM_OMAR, OMAR, "Ping")
        mention = slack.say(SHOWROOM, PRIYA, f"<@{ME}> sign-off?")
        first = ok(agents, ex, "watch_new", {})
        assert first["problems"] == [] and first["more"] is True and first["unchecked"] >= 1
        rows = [(m["kind"], m["ts"]) for m in first["messages"]]
        assert ("mention", mention) in rows and len(rows) == 2
        handed = [m["ts"] for m in first["messages"] if m["kind"] == "dm"]
        for _ in range(3):
            slack.minute_passed()
            later = ok(agents, ex, "watch_new", {})["messages"]
            assert all(m["kind"] == "dm" for m in later)
            handed += [m["ts"] for m in later]
        assert sorted(handed) == sorted([dm, other])
        assert provider.data["watch"][watch["watch_ref"]]["keys"]["status"] == "active"


class TestWatchesAcrossAccounts:
    def test_one_failing_watch_keeps_its_place_and_the_other_is_handed_on(self, agents, slack):
        provider = Records(secrets={"slack": [slack.secret(), slack.secret(REVOKED_TOKEN, account="old-workspace")]})
        ex = Chat(provider)
        good = ok(agents, ex, "watch_start", {"which": "dms", "account": "demo"})
        bad = provider.create_data("watch", {"which": "dms", "account": "old-workspace", "status": "active",
                                             "since": "1700000000.000000", "mention_after": "1700000000.000000"})
        dm = slack.say(DM_DANA, DANA, "Got it")
        news = ok(agents, ex, "watch_new", {})
        assert [m["ts"] for m in news["messages"]] == [dm] and news["messages"][0]["watch_ref"] == good["watch_ref"]
        assert [(p["watch_ref"], p["kind"]) for p in news["problems"]] == [(bad, "auth")]
        assert provider.data["watch"][bad]["keys"]["mention_after"] == "1700000000.000000"

    def test_a_watch_made_by_one_zero_zero_carries_on(self, agents, slack):
        ex, provider = executor(slack)
        cursor = slack.say(DM_DANA, DANA, "Before")
        old = provider.create_data("watch", {"which": "both", "cursor": cursor, "status": "watching",
                                             "skip_conversations": ""})
        after = slack.say(DM_DANA, DANA, "After")
        news = ok(agents, ex, "watch_new", {})
        assert [(m["ts"], m["watch_ref"]) for m in news["messages"]] == [(after, old)]
        assert provider.data["watch"][old]["keys"]["status"] == "active"

    def test_watches_are_listed_and_a_closed_one_is_not_checked(self, agents, slack):
        ex, provider = executor(slack)
        first = ok(agents, ex, "watch_start", {"which": "mentions", "note": "for the layout"})
        second = ok(agents, ex, "watch_start", {"which": "dms"})
        listed = ok(agents, ex, "watches_list", {})
        assert {(w["watch_ref"], w["which"], w["account"]) for w in listed["watches"]} == {
            (first["watch_ref"], "mentions", "demo"), (second["watch_ref"], "dms", "demo")}
        assert ok(agents, ex, "watches_close", {"watch_ref": first["watch_ref"]}) == {
            "watch_ref": first["watch_ref"], "closed": True, "already": False}
        slack.say(SHOWROOM, PRIYA, f"<@{ME}> layout?")
        news = ok(agents, ex, "watch_new", {})
        assert news["checked"] == 1 and news["messages"] == []
        assert [w["watch_ref"] for w in ok(agents, ex, "watches_list", {})["watches"]] == [second["watch_ref"]]
        assert provider.data["watch"][first["watch_ref"]]["values"]["note"] == "for the layout"

    def test_a_dm_conversation_that_went_away_is_dropped_not_failed(self, agents, slack):
        ex, _ = executor(slack)
        ok(agents, ex, "watch_start", {"which": "dms"})
        slack.add_conversation("D0GONE", OMAR, is_im=True, user=OMAR)
        slack.say("D0GONE", OMAR, "Soon gone")
        # users.conversations still lists it; its history says it is not there.
        slack.gone.add("D0GONE")
        dm = slack.say(DM_DANA, DANA, "Still here")
        news = ok(agents, ex, "watch_new", {})
        assert news["problems"] == [] and [m["ts"] for m in news["messages"]] == [dm]
