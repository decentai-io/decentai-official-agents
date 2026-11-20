"""The shared library every connector is written on: HTTP that waits out a busy service and never sends a write twice,
watches that never lose what one of them read when another fails, accounts picked by address."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from standard.accounts import account
from standard.agent import ResourceDenied
from standard.http import Http, HttpError
from standard.google import Google
from standard.graph import Graph, Me, recipients, sorted_by, utc
from standard.watch import Gone, Seen, check, close, listing


class Scripted:
    """A loopback server answering each request with the next scripted answer: (status, body, headers)."""

    def __init__(self, answers):
        self.answers, self.seen = list(answers), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.seen.append((self.command, self.path, self.rfile.read(length) if length else b"",
                                   dict(self.headers)))
                status, body, headers = outer.answers.pop(0) if outer.answers else (200, {}, {})
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for name, value in {"Content-Type": "application/json", **headers}.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()


class Quick(Http):
    SERVICE = "Example"
    WAIT_MOST = 3.0

    @classmethod
    def pause(cls, response, attempt):
        return min(0.05, super().pause(response, attempt))


@pytest.fixture(autouse=True)
def under_test(monkeypatch):
    monkeypatch.setenv("AGENTS_UNDER_TEST", "1")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def run(awaitable):
    return asyncio.run(awaitable)


def test_a_read_waits_out_a_busy_service_and_retry_after():
    server = Scripted([(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"}),
                       (503, {}, {}), (200, {"ok": 1}, {})])
    try:
        assert run(Quick("t", api_base_url=server.base).get("thing")) == {"ok": 1}
        assert len(server.seen) == 3
    finally:
        server.stop()


def test_a_busy_service_past_the_wait_is_rate_with_its_words():
    server = Scripted([(429, {"error": {"message": "slow down"}}, {"Retry-After": "60"})])
    try:
        with pytest.raises(HttpError) as failed:
            run(Quick("t", api_base_url=server.base).get("thing"))
        assert failed.value.kind == "rate" and "60 seconds" in failed.value.message
        assert len(server.seen) == 1
    finally:
        server.stop()


def test_a_write_is_sent_again_only_when_the_service_said_it_did_nothing():
    server = Scripted([(429, {}, {"Retry-After": "0"}), (201, {"id": "x"}, {})])
    try:
        assert run(Quick("t", api_base_url=server.base).post("things", json={"a": 1})) == {"id": "x"}
        assert [s[0] for s in server.seen] == ["POST", "POST"]
    finally:
        server.stop()
    server = Scripted([(500, {"error": {"message": "oops"}}, {})])
    try:
        with pytest.raises(HttpError) as failed:
            run(Quick("t", api_base_url=server.base).post("things", json={"a": 1}))
        assert failed.value.kind == "http" and len(server.seen) == 1
    finally:
        server.stop()
    server = Scripted([(504, {}, {})])
    try:
        with pytest.raises(HttpError) as failed:
            run(Quick("t", api_base_url=server.base).post("things", json={"a": 1}))
        assert failed.value.kind == "unknown" and len(server.seen) == 1
    finally:
        server.stop()


def test_refusals_have_kinds():
    cases = [(401, "auth"), (404, "not_found"), (409, "conflict"), (412, "conflict"), (423, "locked"),
             (400, "invalid"), (403, "denied")]
    for status, kind in cases:
        server = Scripted([(status, {"error": {"message": "no"}}, {})])
        try:
            with pytest.raises(HttpError) as failed:
                run(Quick("t", api_base_url=server.base).get("thing"))
            assert failed.value.kind == kind, status
        finally:
            server.stop()


def test_no_token_is_auth_and_nothing_is_sent():
    server = Scripted([])
    try:
        with pytest.raises(HttpError) as failed:
            run(Quick("", api_base_url=server.base).get("thing"))
        assert failed.value.kind == "auth" and not server.seen
    finally:
        server.stop()


def test_a_page_token_elsewhere_is_refused():
    with pytest.raises(HttpError) as failed:
        run(Quick("t", api_base_url="http://127.0.0.1:9").get("", absolute="https://evil.example/v1.0/me"))
    assert failed.value.kind == "invalid"


def test_pages_follow_graphs_next_link():
    server = Scripted([])
    server.answers = [(200, {"value": [1, 2], "@odata.nextLink": f"{server.base}/items?$skip=2"}, {}),
                      (200, {"value": [3]}, {})]
    try:
        assert run(Quick("t", api_base_url=server.base).pages("items")) == [1, 2, 3]
    finally:
        server.stop()


def test_a_download_is_held_to_its_size():
    server = Scripted([(200, b"x" * 2000, {"Content-Type": "application/octet-stream"})])
    try:
        with pytest.raises(HttpError) as failed:
            run(Quick("t", api_base_url=server.base).download("file", most=1000))
        assert failed.value.kind == "too_large"
    finally:
        server.stop()


def test_google_says_busy_with_403_and_missing_permission_as_scope():
    busy = {"error": {"code": 403, "message": "Rate Limit Exceeded",
                      "errors": [{"reason": "userRateLimitExceeded"}]}}
    scope = {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                       "status": "PERMISSION_DENIED"}}

    class QuickGoogle(Google):
        WAIT_MOST = 3.0
        pause = Quick.pause

    server = Scripted([(403, busy, {}), (200, {"ok": True}, {})])
    try:
        assert run(QuickGoogle({"access_token": "t", "api_base_url": server.base}).get("x")) == {"ok": True}
    finally:
        server.stop()
    server = Scripted([(403, scope, {})])
    try:
        with pytest.raises(HttpError) as failed:
            run(QuickGoogle({"access_token": "t", "api_base_url": server.base}).get("x"))
        assert failed.value.kind == "scope" and "reconnect" in failed.value.message
    finally:
        server.stop()


def test_graph_knows_every_address_of_the_account_and_a_personal_one():
    me = Me.of({"mail": "Ahmed@Work.example", "userPrincipalName": "ahmed_w@tenant.onmicrosoft.com",
                "proxyAddresses": ["SMTP:ahmed@work.example", "smtp:a.m@work.example"]})
    assert {"ahmed@work.example", "ahmed_w@tenant.onmicrosoft.com", "a.m@work.example"} <= me.mine
    assert not me.personal and Me.of({"userPrincipalName": "someone@outlook.com"}).personal
    assert Graph({"access_token": "opaque-personal-token"}).tenant() == ""


def test_graph_helpers():
    assert recipients("Dana <dana@x.example>, dana@x.example; b@y.example") == [
        {"emailAddress": {"address": "dana@x.example", "name": "Dana"}},
        {"emailAddress": {"address": "b@y.example"}}]
    assert recipients(["a@x.example", "b@y.example"])[1]["emailAddress"]["address"] == "b@y.example"
    assert utc("2026-11-20T10:00:00.1234567Z") == "2026-11-20T10:00:00Z"
    assert utc("2026-11-20T14:00:00+04:00") == "2026-11-20T10:00:00Z"
    assert utc("tomorrow") == ""
    rows = [{"t": "2026-11-20T10:00:00Z"}, {"t": "2026-11-19T10:00:00Z"}]
    assert [r["t"][:10] for r in sorted_by(rows, "t")] == ["2026-11-19", "2026-11-20"]


class FakeRecords:
    def __init__(self, rows=None, fail_update=()):
        self.rows, self.updates, self.fail_update = {r["ref"]: r for r in rows or []}, [], set(fail_update)

    async def list(self, resource, filters=None):
        return list(self.rows.values())

    async def read(self, resource, ref=None):
        return self.rows[ref]

    async def update(self, resource, ref, fields):
        if ref in self.fail_update:
            raise ResourceDenied("no", "failed")
        self.updates.append((ref, fields))
        self.rows[ref]["fields"].update(fields)

    async def secret(self, resource, ref=None):
        return {"account": self.rows[ref]["fields"]["account"], "access_token": "t"}

    async def create(self, resource, fields):
        raise ResourceDenied("asked", "refused")


class FakeCall:
    def __init__(self, records, inputs=None):
        self.records, self.inputs = records, inputs or {}


def watches(*refs):
    return [{"ref": r, "fields": {"status": "active", "label": r}} for r in refs]


def test_a_failing_watch_keeps_its_cursor_and_the_others_are_handed_on():
    records = FakeRecords(watches("a", "b", "c"))

    async def read(watch):
        if watch["ref"] == "b":
            raise HttpError("rate", "busy")
        if watch["ref"] == "c":
            raise HttpError("not_found", "gone")
        return Seen([{"row": watch["ref"]}], {"since": "2026-11-20"})

    result = run(check(FakeCall(records), "inbox", list(records.rows.values()), read))
    assert result["rows"] == [{"row": "a"}]
    assert [p["watch_ref"] for p in result["problems"]] == ["b"]
    assert [c["watch_ref"] for c in result["closed"]] == ["c"]
    assert records.updates == [("a", {"since": "2026-11-20"}), ("c", {"status": "closed"})]


def test_no_cursor_moves_until_every_watch_was_read():
    records, order = FakeRecords(watches("a", "b")), []

    async def read(watch):
        order.append(("read", watch["ref"], len(records.updates)))
        return Seen([{"row": watch["ref"]}], {"since": watch["ref"]})

    run(check(FakeCall(records), "inbox", list(records.rows.values()), read))
    assert order == [("read", "a", 0), ("read", "b", 0)] and len(records.updates) == 2


def test_a_cursor_that_cannot_be_kept_still_hands_the_rows_on():
    records = FakeRecords(watches("a"), fail_update={"a"})

    async def read(watch):
        return Seen([{"row": 1}], {"since": "x"})

    result = run(check(FakeCall(records), "inbox", list(records.rows.values()), read))
    assert result["rows"] == [{"row": 1}] and "again" in result["problems"][0]["error"]


def test_the_time_budget_leaves_the_rest_for_next_time():
    records = FakeRecords(watches("a", "b"))

    async def read(watch):
        await asyncio.sleep(0.05)
        return Seen([{"row": watch["ref"]}], {"since": "x"})

    result = run(check(FakeCall(records), "inbox", list(records.rows.values()), read, budget=0.01))
    assert result["checked"] == 1 and result["more"] is True


def test_a_watch_gone_or_done_closes_and_listing_and_close():
    records = FakeRecords(watches("a", "b"))

    async def read(watch):
        if watch["ref"] == "a":
            raise Gone("The conversation was deleted.")
        return Seen([], None, close="Replied.")

    result = run(check(FakeCall(records), "inbox", list(records.rows.values()), read))
    assert {c["why"] for c in result["closed"]} == {"The conversation was deleted.", "Replied."}
    assert run(listing(FakeCall(records), "inbox", lambda r: {})) == []
    records = FakeRecords(watches("z"))
    assert run(close(FakeCall(records), "inbox", "z"))["already"] is False
    assert run(close(FakeCall(records), "inbox", "z"))["already"] is True


def test_an_account_is_picked_by_address_and_several_need_a_name():
    rows = [{"ref": "1", "fields": {"account": "ahmed@work.example"}},
            {"ref": "2", "fields": {"account": "ahmed@home.example"}}]
    records = FakeRecords(rows)
    secret, why = run(account(FakeCall(records), "microsoft", "", service="Microsoft"))
    assert secret is None and "Several" in why
    secret, why = run(account(FakeCall(records), "microsoft", "work", service="Microsoft"))
    assert secret["account"] == "ahmed@work.example"
    secret, why = run(account(FakeCall(records), "microsoft", "ahmed", service="Microsoft"))
    assert secret is None and "matches" in why
