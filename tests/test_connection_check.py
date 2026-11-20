"""The Connection Check agent's program.

The hosts it tries stand on this machine: a loopback web server is
every host that answers, and the test's way out is what refuses. A
program here is not confined, so a connection made past the way out
arrives, and the check says so; the verdict of an agent that is held
all the way is proved where programs are confined, in the sandbox.
"""

import json
import socket
import threading

import pytest
import yaml


from tests.harness import Chat, Records, probe, requirements, run, way_out
from tests.conftest import ROOT
from tests.web_stub import web  # noqa: F401

DECLARED = "example.com"


def invoke(agents, name, inputs=None):
    executor = Chat(Records())
    return run(executor.invoke(
        agents["connection_check"], f"check_{name}",
        inputs or {}, chat_level=0))


def port_of(stub) -> int:
    return int(stub.url.rsplit(":", 1)[1])


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def rows_of(result):
    return {row["attempt"]: row for row in result["rows"]}


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["connection_check"]
        assert probe(agent) == []

    def test_it_declares_one_host_and_the_check_is_measured_against_it(self, agents):
        manifest = yaml.safe_load(
            (ROOT / "connection_check" / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["network"] == {"hosts": [DECLARED]}
        source = (ROOT / "connection_check" / "tools" / "check_tool.py").read_text(
            encoding="utf-8")
        assert f'DECLARED = "{DECLARED}"' in source
        assert requirements(ROOT / "connection_check") == []

    def test_both_functions_are_reads(self, agents):
        agent = agents["connection_check"]
        assert {name: agent.function(f"check_{name}")["level"]
                for name in ("report", "reach")} == {"report": 0, "reach": 0}


class TestWhereNothingHoldsIt:
    """No way out: a program connects as any program on the machine does."""

    @pytest.fixture
    def machine(self, web, monkeypatch):
        # The program is pointed at no way out.
        way_out().pointed = ""
        port = port_of(web)
        monkeypatch.setenv("DECENTAI_AGENT_CONNECTION_CHECK_TEST", json.dumps({
            "port": port, "direct": f"127.0.0.1:{port}", "name": "localhost",
            "resolve": {DECLARED: "127.0.0.1", "www.wikipedia.org": "127.0.0.1"}}))
        return port

    def test_the_check_says_nothing_holds_it(self, agents, machine):
        executor = Chat(Records())
        result, status = run(executor.invoke(agents["connection_check"], "check_report", {}, chat_level=0))
        assert status == "success", result
        # The rows are shown in the chat too.
        [shown] = executor.said
        assert shown["table"]["title"] == "What this agent could reach" and len(shown["table"]["rows"]) == 5
        assert result["through_the_proxy"] is False and result["held"] is False
        assert "Nothing holds this agent" in result["verdict"]
        rows = rows_of(result)
        assert rows["The host this agent declared"]["outcome"] == "reached"
        # What an agent that is held is refused, this one reached.
        undeclared = rows["A host it did not declare"]
        assert undeclared["outcome"] == "reached" and undeclared["as_expected"] is False
        assert rows["Past the proxy: a connection made straight"]["outcome"] == "reached"

    def test_an_address_inside_is_not_knocked_on(self, agents, machine):
        inside = rows_of(invoke(agents, "report")[0])["An address inside the network"]
        assert inside["outcome"] == "not_tried"
        assert "does not try one" in inside["reason"]

    def test_one_address_is_tried_straight(self, agents, machine):
        result, status = invoke(agents, "reach", {"address": f"127.0.0.1:{machine}"})
        assert status == "success", result
        assert result == {"host": "127.0.0.1", "port": machine, "declared": False,
                          "outcome": "reached", "through_the_proxy": False}

    def test_a_private_address_is_not_tried(self, agents, machine):
        result, status = invoke(agents, "reach", {"address": "http://10.0.0.5/admin"})
        assert status == "success", result
        assert result["outcome"] == "not_tried" and result["port"] == 80
        assert "inside the network" in result["reason"]


class TestBehindThePlatformsProxy:
    """The way out opens the one host the manifest declares, and knows
    the program by where it arrived: no pass."""

    @pytest.fixture
    def proxy(self, web, monkeypatch):
        """The test's own way out, which the program is pointed at; the
        stub's port, and one nothing listens on, count as the web's."""
        port, closed = port_of(web), closed_port()
        found = way_out()
        found.web.update((port, closed))
        monkeypatch.setenv("DECENTAI_AGENT_CONNECTION_CHECK_TEST", json.dumps({
            "port": port, "direct": f"127.0.0.1:{port}", "name": "localhost"}))
        found.port_of_the_site, found.closed_port = port, closed
        return found

    def test_the_host_it_declared_is_reached(self, agents, proxy):
        result, status = invoke(agents, "reach", {"address": DECLARED})
        assert status == "success", result
        assert result == {"host": DECLARED, "port": proxy.port_of_the_site,
                          "declared": True, "outcome": "reached",
                          "through_the_proxy": True}
        assert proxy.refusals == 0

    def test_a_host_it_did_not_declare_is_refused_and_told_why(self, agents, proxy):
        result, status = invoke(agents, "reach", {"address": "https://api.github.com/user"})
        assert status == "success", result
        assert (result["host"], result["declared"]) == ("api.github.com", False)
        assert result["outcome"] == "refused"
        assert result["reason"] == (f"api.github.com:{proxy.port_of_the_site} is not a host this "
                                    f"agent declared, or was opened for this call.")
        assert proxy.refusals == 1

    def test_an_address_it_did_not_declare_is_refused(self, agents, proxy):
        result, _ = invoke(agents, "reach", {"address": "10.0.0.5"})
        assert result["outcome"] == "refused"
        assert "10.0.0.5" in result["reason"] and "not a host this agent declared" in result["reason"]

    def test_a_declared_host_that_does_not_answer_is_not_called_refused(
            self, agents, proxy):
        result, _ = invoke(agents, "reach",
                           {"address": f"{DECLARED}:{proxy.closed_port}"})
        assert result["outcome"] == "unreachable"
        assert "could not be reached" in result["reason"]
        assert proxy.refusals == 0

    def test_the_check_tells_held_by_the_proxy_from_held_all_the_way(
            self, agents, proxy):
        """The proxy refuses what was not declared. Nothing on this
        machine makes it the only way out, and the check says that
        too."""
        result, status = invoke(agents, "report")
        assert status == "success", result
        rows = rows_of(result)
        assert [(rows[name]["outcome"], rows[name]["as_expected"]) for name in (
            "The host this agent declared", "A host it did not declare",
            "An address inside the network")] == [
            ("reached", True), ("refused", True), ("refused", True)]
        assert "www.wikipedia.org" in rows["A host it did not declare"]["reason"]
        assert "not a host this agent declared" in rows["An address inside the network"]["reason"]
        straight = rows["Past the proxy: a connection made straight"]
        assert (straight["outcome"], straight["as_expected"]) == ("reached", False)
        assert result["through_the_proxy"] is True and result["held"] is False
        assert result["verdict"].startswith("Partly held.")

    def test_a_refusal_whose_reason_follows_the_head_is_told(self, agents, proxy):
        """The platform's way out answers a refusal with 403 Forbidden and
        its reason on the next line: a stand-in answering so, pointed at."""
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()

            def answer():
                link, _ = server.accept()
                with link:
                    link.recv(4096)
                    link.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                                 b"Connection: close\r\n\r\nexample.com is blocked by the "
                                 b"organization's policy.\n")
            threading.Thread(target=answer, daemon=True).start()
            proxy.pointed = f"http://127.0.0.1:{server.getsockname()[1]}"
            result, _ = invoke(agents, "reach", {"address": DECLARED})
        assert result["outcome"] == "refused"
        assert result["reason"] == "example.com is blocked by the organization's policy."


class TestWhatIsNotAnAddress:
    @pytest.mark.parametrize("address, says", [
        ("ftp://example.com/file", "is not tried"),
        ("https://sara:secret@example.com/", "a name and a password"),
        ("not a host", "names no host"),
        ("https://", "names no host"),
    ])
    def test_it_is_refused_before_anything_is_tried(self, agents, address, says):
        result, status = invoke(agents, "reach", {"address": address})
        assert status == "error", result
        assert result["kind"] == "invalid_address" and says in result["error"]


class TestTheFixesOf101:
    """A failed name lookup is not the platform's refusal, and no socket waits on the agent's loop."""

    @pytest.fixture
    def held(self, web, monkeypatch):
        """Behind the way out, with the straight connection refused (a port nothing listens on) and a name that no
        name service finds: every row as a held agent's, the lookup included."""
        port, closed = port_of(web), closed_port()
        way_out().web.add(port)
        monkeypatch.setenv("DECENTAI_AGENT_CONNECTION_CHECK_TEST", json.dumps({
            "port": port, "direct": f"127.0.0.1:{closed}", "name": "nothing-here.invalid"}))
        return port

    def test_a_failed_lookup_is_unreachable_not_refused(self, agents, held):
        result, status = invoke(agents, "report")
        assert status == "success", result
        lookup = rows_of(result)["Past the proxy: a name looked up"]
        assert (lookup["expected"], lookup["outcome"], lookup["as_expected"]) == ("unreachable", "unreachable", True)
        assert "does not show what stopped it" in lookup["reason"]
        assert all(row["outcome"] != "refused" or "reason" in row for row in result["rows"])
        assert result["held"] is True and result["verdict"].startswith("Held.")

    def test_with_no_way_out_nothing_counts_as_held(self, agents, web, monkeypatch):
        """No proxy, no name service, the straight connection refused by this machine: every row may look as a
        held agent's would, and still it is not held, since nothing of the platform's did it."""
        way_out().pointed = ""
        monkeypatch.setenv("DECENTAI_AGENT_CONNECTION_CHECK_TEST", json.dumps({
            "port": closed_port(), "direct": f"127.0.0.1:{closed_port()}", "name": "nothing-here.invalid",
            "resolve": {DECLARED: "127.0.0.1", "www.wikipedia.org": "127.0.0.1"}}))
        result, status = invoke(agents, "report")
        assert status == "success", result
        assert result["through_the_proxy"] is False and result["held"] is False
        assert "Nothing holds this agent" in result["verdict"]

    def test_a_slow_attempt_does_not_hold_up_the_agents_other_calls(self, agents, monkeypatch):
        """A way out that takes a connection and says nothing: the attempt waits on its socket. Meanwhile another
        call of the same chat is answered at once, which it would not be with the socket on the loop."""
        import asyncio
        import time

        with socket.socket() as silent:
            silent.bind(("127.0.0.1", 0))
            silent.listen()
            way_out().pointed = f"http://127.0.0.1:{silent.getsockname()[1]}"
            executor = Chat(Records())
            agent = agents["connection_check"]

            async def scenario():
                await agent.started()
                slow = asyncio.ensure_future(executor.invoke(agent, "check_reach", {"address": DECLARED},
                                                             chat_level=0))
                await asyncio.sleep(1)
                began = time.monotonic()
                quick, status = await executor.invoke(agent, "check_reach", {"address": "ftp://x.example/"},
                                                      chat_level=0)
                took = time.monotonic() - began
                await agent.cancel(next(iter(agent.calls)))
                await asyncio.gather(slow, return_exceptions=True)
                return quick, status, took

            quick, status, took = run(scenario())
        assert status == "error" and quick["kind"] == "invalid_address"
        assert took < 3, took
