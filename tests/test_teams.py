"""The Teams agent, its program spoken to in the standard's messages, against a loopback Microsoft Graph as strict as
Microsoft documents it: a one-to-one chat with Dana, the Ops group chat with Omar and Priya, and the Sidra Office team
with its General and Showroom channels."""

import pytest

from tests.conftest import resource
from tests.graph_teams_stub import (DM_DANA, GENERAL, OPS, PERSONAL_TENANT, SHOWROOM, TEAM, GraphTeamsStub, ago,
                                    token)
from tests.harness import Chat, Records, probe, run

DANA = "dana@sidra.example"
SCOPES = ["openid", "email", "offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send", "MailboxSettings.Read",
          "Calendars.ReadWrite", "Files.ReadWrite.All", "Chat.ReadWrite", "Team.ReadBasic.All", "Channel.ReadBasic.All",
          "ChannelMessage.Send", "Tasks.ReadWrite"]


@pytest.fixture
def graph():
    stub = GraphTeamsStub().start()
    yield stub
    stub.stop()


def executor(graph, access_token=""):
    provider = Records(secrets={"microsoft": graph.secret(access_token)})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs=None, chat_level=1):
    return run(ex.invoke(agents["teams"], name, inputs or {}, chat_level=chat_level))


def ok(agents, ex, name, inputs=None, chat_level=1):
    result, status = invoke(agents, ex, name, inputs, chat_level)
    assert status == "success", result
    return result


def posts(graph, path_end="messages"):
    return [r for r in graph.requests if r[0] == "POST" and r[1].endswith(path_end)]


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        assert probe(agents["teams"]) == []

    def test_it_declares_the_shared_microsoft_account(self, agents):
        secret = resource(agents["teams"].manifest, "secrets", "microsoft")
        assert secret["oauth"]["scopes"] == SCOPES
        assert secret["binding"] == {"cardinality": "many", "required": True}
        # Reading channel messages would need an administrator's consent for the one consent every agent shares.
        assert "ChannelMessage.Read.All" not in SCOPES

    def test_levels_limits_and_what_is_kept_where(self, agents):
        manifest = agents["teams"].manifest
        functions = {f["id"]: f for f in manifest["functions"]}
        assert {i for i, f in functions.items() if f["level"] == 3} == {"chats_send", "channels_post",
                                                                         "channels_reply"}
        assert [i for i, f in functions.items() if f.get("schedulable")] == ["watches_check"]
        assert functions["watches_check"]["level"] == 1 and "model" not in functions["watches_check"]["calls"]
        assert len(manifest["agent"]["instructions"]) <= 4000 and len(manifest["agent"]["examples"]) <= 6
        sent = {f["name"]: f["storage"] for f in resource(manifest, "data", "sent")["fields"]}
        assert sent["text"] == sent["to"] == "values"
        for function in functions.values():
            assert "account" in function["inputs"]["properties"] or function["id"].startswith("watches_")


class TestAccounts:
    def test_status_says_whose_account(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(agents, ex, "account_status")
        assert (result["connected"], result["email"], result["name"]) == (True, GraphTeamsStub.ACCOUNT, "Demo Rahman")

    def test_a_personal_account_is_told_before_graph_is_asked(self, agents, graph):
        ex, _ = executor(graph, access_token=token(PERSONAL_TENANT))
        for name, inputs in (("chats_list", {}), ("chats_missed", {}),
                             ("chats_send", {"chat_id": DM_DANA, "text": "Hi"})):
            result, status = invoke(agents, ex, name, inputs, chat_level=3)
            assert status == "error" and result["kind"] == "personal", result
            assert "Teams needs a work or school account" in result["error"]
        assert graph.requests == []
        status = ok(agents, ex, "account_status")
        assert status["connected"] is False and "work or school" in status["problem"]

    def test_an_expired_connection_says_reconnect(self, agents, graph):
        ex, _ = executor(graph, access_token=token(oid="u-demo") + "x")
        result, status = invoke(agents, ex, "chats_list")
        assert status == "error" and result["kind"] == "auth" and "reconnect" in result["error"].lower()


class TestReading:
    def test_chats_come_by_their_latest_message_and_are_paged(self, agents, graph):
        # Graph's lastUpdatedDateTime is not the order: the latest message is.
        for n in range(60):
            graph.add_chat(f"19:c{n:02d}@thread.v2", "group", f"Project {n:02d}",
                           ["demo@sidra.example", "priya@sidra.example"])
            graph.add_message(f"19:c{n:02d}@thread.v2", "priya@sidra.example", f"<p>Note {n}</p>", ago(200 + n))
        graph.chats[OPS]["lastUpdatedDateTime"] = ago(100000)
        ex, _ = executor(graph)
        first = ok(agents, ex, "chats_list", {"max_results": 20})
        assert [c["chat_id"] for c in first["chats"][:3]] == [OPS, DM_DANA, "19:c00@thread.v2"]
        assert first["chats"][0]["last_message"] == "Northlight moved the chairs to the 24th."
        [asked] = [r for r in graph.requests if r[1] == "me/chats"]
        assert asked[2]["$orderby"] == "lastMessagePreview/createdDateTime desc" and asked[2]["$top"] == "20"
        seen = [c["chat_id"] for c in first["chats"]]
        token_ = first["next_page_token"]
        while token_:
            page = ok(agents, ex, "chats_list", {"max_results": 20, "page_token": token_})
            seen += [c["chat_id"] for c in page["chats"]]
            token_ = page.get("next_page_token")
        assert len(seen) == len(set(seen)) == 62

    def test_with_narrows_by_address_or_name_and_unread_is_said(self, agents, graph):
        ex, _ = executor(graph)
        with_dana = ok(agents, ex, "chats_list", {"with": DANA})["chats"]
        assert [c["chat_id"] for c in with_dana] == [DM_DANA] and with_dana[0]["unread"] is True
        assert [c["chat_id"] for c in ok(agents, ex, "chats_list", {"with": "Omar"})["chats"]] == [OPS]

    def test_both_expansions_refused_falls_back_to_the_preview(self, agents, graph):
        graph.refuse_both_expands = True
        ex, _ = executor(graph)
        assert [c["chat_id"] for c in ok(agents, ex, "chats_list")["chats"]] == [OPS, DM_DANA]

    def test_a_chat_reads_as_text_without_system_notices(self, agents, graph):
        ex, _ = executor(graph)
        [message] = ok(agents, ex, "chats_read", {"chat_id": DM_DANA})["messages"]
        assert (message["from"], message["from_name"], message["mine"]) == (DANA, "Dana Harbour", False)
        assert message["text"] == ("Can you send the showroom drawings by Thursday?\n"
                                   "Harbourline & I want to sign off the layout.")

    def test_a_long_chat_is_read_past_fifty_and_further_back(self, agents, graph):
        for n in range(130):
            graph.add_message(OPS, "priya@sidra.example", f"<p>Line {n}</p>", ago(29 - n * 0.1))
        ex, _ = executor(graph)
        read = ok(agents, ex, "chats_read", {"chat_id": OPS, "max_results": 120})
        assert len(read["messages"]) == 120
        assert read["messages"][-1]["text"] == "Line 129" and read["messages"][0]["text"] == "Line 10"
        older = ok(agents, ex, "chats_read", {"chat_id": OPS, "max_results": 120,
                                              "page_token": read["next_page_token"]})
        assert [m["text"] for m in older["messages"]][-1] == "Line 9"
        assert all(int(r[2].get("$top", 50)) <= 50 for r in graph.requests)

    def test_arabic_survives(self, agents, graph):
        graph.add_chat("19:layla@thread.v2", "oneOnOne", None, ["demo@sidra.example", "layla@sidra.example"])
        graph.add_message("19:layla@thread.v2", "layla@sidra.example", "<p>هل وصلت الكراسي؟</p>", ago(3))
        ex, _ = executor(graph)
        [message] = ok(agents, ex, "chats_read", {"chat_id": "19:layla@thread.v2"})["messages"]
        assert (message["text"], message["from_name"]) == ("هل وصلت الكراسي؟", "ليلى حداد")
        sent = ok(agents, ex, "chats_send", {"chat_id": "19:layla@thread.v2", "text": "نعم، وصلت اليوم."},
                  chat_level=3)
        assert sent["status"] == "sent" and graph.sends[-1]["body"]["content"] == "نعم، وصلت اليوم."

    def test_an_unknown_chat_is_named_not_invented(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "chats_read", {"chat_id": "19:nope@thread.v2"})
        assert status == "error" and result["kind"] == "not_found"

    def test_a_busy_graph_is_waited_out(self, agents, graph):
        graph.throttle(2)
        ex, _ = executor(graph)
        assert len(ok(agents, ex, "chats_list")["chats"]) == 2


class TestWhatDidIMiss:
    def test_unread_chats_with_what_others_said(self, agents, graph):
        graph.add_message(DM_DANA, "dana@sidra.example", "<p>Also the chairs.</p>", ago(20))
        graph.add_message(DM_DANA, "demo@sidra.example", "<p>On it.</p>", ago(10))
        graph.add_message(DM_DANA, "dana@sidra.example", "<p>Thanks!</p>", ago(5))
        ex, _ = executor(graph)
        missed = ok(agents, ex, "chats_missed", {"hours": 3})
        [chat] = missed["chats"]
        assert chat["chat_id"] == DM_DANA and chat["new_count"] == 3
        assert [m["text"] for m in chat["messages"]] == [
            "Can you send the showroom drawings by Thursday?\nHarbourline & I want to sign off the layout.",
            "Also the chairs.", "Thanks!"]
        # Ops was read after its last message: unread only leaves it out, recent brings it in.
        everything = ok(agents, ex, "chats_missed", {"hours": 3, "unread_only": False})
        assert {c["chat_id"] for c in everything["chats"]} == {DM_DANA, OPS}
        for asked in (r for r in graph.requests if r[1].endswith("/messages")):
            assert asked[2]["$orderby"].split()[0] == asked[2]["$filter"].split()[0] == "lastModifiedDateTime"


class TestSending:
    def test_a_send_is_one_post_and_recorded(self, agents, graph):
        ex, provider = executor(graph)
        before = len(graph.requests)
        sent = ok(agents, ex, "chats_send", {"chat_id": DM_DANA, "text": "The drawings are coming Thursday."},
                  chat_level=3)
        assert sent["status"] == "sent" and sent["chat_id"] == DM_DANA
        made = graph.requests[before:]
        # No read before it beyond the records, and nothing read back after it.
        assert [r[0] for r in made] == ["POST"] and made[0][1] == f"chats/{DM_DANA}/messages"
        assert graph.sends[-1]["body"] == {"contentType": "text", "content": "The drawings are coming Thursday."}
        record = provider.data["sent"][sent["sent_ref"]]
        assert record["keys"]["status"] == "sent" and record["keys"]["message_id"] == sent["message_id"]
        assert record["values"]["text"] == "The drawings are coming Thursday."
        assert "text" not in record["keys"] and "to" not in record["keys"]

    def test_a_person_by_address_gets_their_chat_found_or_made(self, agents, graph):
        ex, _ = executor(graph)
        sent = ok(agents, ex, "chats_send", {"to": DANA, "text": "Hello"}, chat_level=3)
        assert sent["chat_id"] == DM_DANA and len(graph.chats) == 2
        made = ok(agents, ex, "chats_send", {"to": "priya@sidra.example", "text": "Can you check the export?"},
                  chat_level=3)
        assert len(graph.chats) == 3 and made["chat_id"] not in (DM_DANA, OPS)
        result, status = invoke(agents, ex, "chats_send", {"to": "nobody@sidra.example", "text": "Hi"}, chat_level=3)
        assert status == "error" and result["kind"] == "not_found"

    def test_a_name_is_not_an_address(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "chats_send", {"to": "Dana", "text": "Hi"}, chat_level=3)
        assert status == "error" and "email address" in result["error"]
        result, status = invoke(agents, ex, "chats_send", {"text": "Hi"}, chat_level=3)
        assert status == "error" and "chat_id" in result["error"]
        assert graph.sends == []

    def test_a_send_with_no_answer_is_unknown_recorded_and_never_sent_twice(self, agents, graph):
        graph.drop_send = True
        ex, provider = executor(graph)
        words = {"chat_id": DM_DANA, "text": "The drawings are coming Thursday."}
        first = ok(agents, ex, "chats_send", words, chat_level=3)
        assert first["status"] == "unknown" and len(graph.sends) == 1
        assert provider.data["sent"][first["sent_ref"]]["keys"]["status"] == "unknown"
        graph.drop_send = False
        again, status = invoke(agents, ex, "chats_send", words, chat_level=3)
        assert status == "error" and again["kind"] == "unknown" and "again: true" in again["error"]
        assert len(graph.sends) == 1
        sent = ok(agents, ex, "chats_send", {**words, "again": True}, chat_level=3)
        assert sent["status"] == "sent" and len(graph.sends) == 2

    def test_an_unknown_send_that_arrived_is_found_not_sent_again(self, agents, graph):
        graph.drop_send = graph.drop_after_storing = True
        ex, provider = executor(graph)
        words = {"chat_id": DM_DANA, "text": "Arrived after all."}
        first = ok(agents, ex, "chats_send", words, chat_level=3)
        assert first["status"] == "unknown"
        graph.drop_send = False
        second = ok(agents, ex, "chats_send", words, chat_level=3)
        assert (second["status"], second["already"], second["sent_ref"]) == ("sent", True, first["sent_ref"])
        assert len(graph.sends) == 1
        assert provider.data["sent"][first["sent_ref"]]["keys"]["status"] == "sent"

    def test_a_busy_graph_takes_the_send_once(self, agents, graph):
        ex, _ = executor(graph)
        graph.throttle(1)
        sent = ok(agents, ex, "chats_send", {"chat_id": OPS, "text": "Once only."}, chat_level=3)
        assert sent["status"] == "sent"
        assert len([m for m in graph.messages[OPS] if m["body"]["content"] == "Once only."]) == 1

    def test_mentions_name_people_in_the_chat(self, agents, graph):
        ex, _ = executor(graph)
        ok(agents, ex, "chats_send", {"chat_id": OPS, "text": "@Omar can you confirm the 24th?\nThanks",
                                      "mention": ["omar@sidra.example", "Priya"]}, chat_level=3)
        body = graph.sends[-1]
        assert body["body"]["contentType"] == "html"
        assert body["body"]["content"] == ('<at id="1">Priya Menon</at> <at id="0">Omar Nasser</at> can you confirm '
                                           'the 24th?<br>Thanks')
        assert [(m["id"], m["mentioned"]["user"]["id"]) for m in body["mentions"]] == [(0, "u-omar"), (1, "u-priya")]
        result, status = invoke(agents, ex, "chats_send", {"chat_id": OPS, "text": "Hi", "mention": ["dana"]},
                                chat_level=3)
        assert status == "error" and "not among them" in result["error"] and len(graph.sends) == 1

    def test_replies_are_what_came_after_from_someone_else_past_fifty(self, agents, graph):
        ex, _ = executor(graph)
        sent = ok(agents, ex, "chats_send", {"chat_id": DM_DANA, "text": "Drawings Thursday."}, chat_level=3)
        before = ok(agents, ex, "chats_replies", {"sent_ref": sent["sent_ref"]})
        assert before == {"replied": False, "chat_id": DM_DANA, "replies": []}
        for n in range(70):
            graph.reply(DM_DANA, DANA, f"Answer {n}")
        after = ok(agents, ex, "chats_replies", {"sent_ref": sent["sent_ref"]})
        assert after["replied"] is True and len(after["replies"]) == 70
        assert after["replies"][0]["text"] == "Answer 0"

    def test_sending_needs_the_persons_go_ahead(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "chats_send", {"chat_id": DM_DANA, "text": "Hi"}, chat_level=2)
        assert status == "error" and graph.sends == []


class TestChannels:
    def test_teams_channels_a_post_and_a_reply(self, agents, graph):
        ex, provider = executor(graph)
        assert [t["name"] for t in ok(agents, ex, "teams_list")["teams"]] == ["Sidra Office"]
        channels = ok(agents, ex, "channels_list", {"team_id": TEAM})["channels"]
        assert [(c["channel_id"], c["name"]) for c in channels] == [(GENERAL, "General"), (SHOWROOM, "Showroom")]
        posted = ok(agents, ex, "channels_post", {"team_id": TEAM, "channel_id": SHOWROOM, "subject": "Chairs",
                                                  "text": "Northlight now deliver on the 24th.",
                                                  "mention": ["Omar"]}, chat_level=3)
        assert graph.sends[-1]["subject"] == "Chairs" and graph.sends[-1]["mentions"][0]["mentioned"]["user"][
            "id"] == "u-omar"
        record = provider.data["sent"][posted["sent_ref"]]
        assert (record["keys"]["kind"], record["values"]["to"]) == ("channel", "Sidra Office / Showroom")
        replied = ok(agents, ex, "channels_reply", {"team_id": TEAM, "channel_id": SHOWROOM,
                                                    "message_id": posted["message_id"], "text": "Confirmed."},
                     chat_level=3)
        assert replied["status"] == "sent" and graph.replies[posted["message_id"]][0]["body"]["content"] == "Confirmed."

    def test_reading_a_channel_says_an_administrator_must_consent(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "channels_read", {"team_id": TEAM, "channel_id": GENERAL})
        assert status == "error" and result["kind"] == "scope" and "administrator" in result["error"]
        posted = ok(agents, ex, "channels_post", {"team_id": TEAM, "channel_id": GENERAL, "text": "Hello"},
                    chat_level=3)
        result, status = invoke(agents, ex, "chats_replies", {"sent_ref": posted["sent_ref"]})
        assert status == "error" and "administrator" in result["error"]

    def test_with_consent_a_channel_and_its_replies_are_read(self, agents, graph):
        graph.channel_read_granted = True
        for n in range(60):
            graph.add_channel_message(GENERAL, "omar@sidra.example", f"Post {n}", ago(100 - n))
        ex, _ = executor(graph)
        read = ok(agents, ex, "channels_read", {"team_id": TEAM, "channel_id": GENERAL, "max_results": 60})
        assert len(read["messages"]) == 60 and read["messages"][-1]["text"] == "Post 59"
        posted = ok(agents, ex, "channels_post", {"team_id": TEAM, "channel_id": GENERAL, "text": "Who's in?"},
                    chat_level=3)
        for n in range(55):
            graph.add_reply(posted["message_id"], "priya@sidra.example", f"Me {n}")
        answered = ok(agents, ex, "chats_replies", {"sent_ref": posted["sent_ref"]})
        assert answered["replied"] is True and len(answered["replies"]) == 55

    def test_a_channel_the_team_does_not_have_is_refused(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "channels_post", {"team_id": TEAM, "channel_id": "19:nope@thread.tacv2",
                                                              "text": "Hi"}, chat_level=3)
        assert status == "error" and "no such channel" in result["error"] and graph.sends == []


class TestWatches:
    def test_new_messages_are_handed_on_once(self, agents, graph):
        ex, provider = executor(graph)
        started = ok(agents, ex, "watches_start")
        assert ok(agents, ex, "watches_check")["messages"] == []
        graph.add_message(DM_DANA, "dana@sidra.example", "<p>New drawing?</p>")
        graph.add_message(OPS, "demo@sidra.example", "<p>Mine</p>")
        graph.add_message(OPS, "omar@sidra.example", "<p>On my way</p>")
        found = ok(agents, ex, "watches_check")
        assert [(m["chat_id"], m["text"]) for m in found["messages"]] == [(DM_DANA, "New drawing?"),
                                                                          (OPS, "On my way")]
        assert found["messages"][0]["watch_ref"] == started["watch_ref"]
        assert ok(agents, ex, "watches_check")["messages"] == []

    def test_one_failing_watch_loses_nothing_and_a_gone_chat_closes(self, agents, graph):
        ex, provider = executor(graph)
        dana = ok(agents, ex, "watches_start", {"chat_id": DM_DANA})
        ops = ok(agents, ex, "watches_start", {"chat_id": OPS})
        graph.add_message(DM_DANA, "dana@sidra.example", "<p>One</p>")
        graph.add_message(OPS, "omar@sidra.example", "<p>Two</p>")
        graph.failing_chats.add(OPS)
        found = ok(agents, ex, "watches_check")
        assert [m["text"] for m in found["messages"]] == ["One"]
        assert [p["watch_ref"] for p in found["problems"]] == [ops["watch_ref"]]
        graph.failing_chats.clear()
        assert [m["text"] for m in ok(agents, ex, "watches_check")["messages"]] == ["Two"]
        del graph.chats[DM_DANA]
        gone = ok(agents, ex, "watches_check")
        assert [c["watch_ref"] for c in gone["closed"]] == [dana["watch_ref"]]
        assert [w["watch_ref"] for w in ok(agents, ex, "watches_list")["watches"]] == [ops["watch_ref"]]
        assert ok(agents, ex, "watches_close", {"watch_ref": ops["watch_ref"]})["closed"] is True
        assert ok(agents, ex, "watches_list")["watches"] == []
        assert ok(agents, ex, "watches_check")["checked"] == 0
