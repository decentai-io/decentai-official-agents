"""The Todoist agent, its program started and spoken to in the standard's
messages, against a loopback Todoist holding a fictional task list for
Sidra Office Supplies: an Inbox, a shared
"Office Move" project with Furniture and IT sections, and a private
"Suppliers" project. Due dates are relative to today, because Todoist's
"today" and "overdue" are.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest


from tests.harness import Chat, Records, probe, run
from tests.conftest import resource
from tests.todoist_stub import TodoistStub

TODAY = date.today()


@pytest.fixture
def todoist():
    stub = TodoistStub().start()
    stub.inbox = stub.add_project("Inbox", inbox=True)
    stub.move = stub.add_project("Office Move", shared=True)
    stub.suppliers = stub.add_project("Suppliers")
    stub.furniture = stub.add_section(stub.move, "Furniture")
    stub.add_section(stub.move, "IT")
    stub.add_label("calls")
    stub.add_label("errands")
    stub.chairs = stub.add_task("Order twenty desk chairs", stub.move, section_id=stub.furniture,
                                labels=["calls"], priority=4, due_date=TODAY.isoformat(),
                                description="Northlight quote Q-2041")
    stub.quote = stub.add_task("Chase the Northlight quote", stub.suppliers, labels=["calls"],
                               priority=2, due_date=(TODAY - timedelta(days=2)).isoformat())
    stub.keys = stub.add_task("Collect keys for the new office", stub.move,
                              due_date=(TODAY + timedelta(days=5)).isoformat())
    stub.review = stub.add_task("Weekly supplier review", stub.suppliers,
                                due_date=(TODAY + timedelta(days=1)).isoformat(),
                                due_string="every Monday", recurring=True)
    yield stub
    stub.stop()


def executor(todoist, access_token="tk-1"):
    provider = Records(secrets={"todoist": todoist.secret(access_token)})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=2):
    return run(ex.invoke(agents["todoist"], name, inputs, chat_level=chat_level))


def known(result):
    """The real names a refusal lists, which come with its words."""
    return json.loads(result["error"][result["error"].rindex(' {"known"') + 1:])["known"]


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["todoist"]
        assert probe(agent) == []

    def test_the_levels_are_what_the_actions_cost(self, agents):
        levels = {f["id"]: f["level"] for f in agents["todoist"].manifest["functions"]}
        # A change in the person's own Todoist that they can undo is 2; what others see or can't be undone is 3.
        assert levels == {
            "account_status": 0, "projects_list": 0, "projects_create": 2,
            "sections_list": 0, "labels_list": 0,
            "tasks_list": 0, "tasks_get": 0, "tasks_create": 2, "tasks_update": 2, "tasks_move": 2,
            "tasks_complete": 2, "tasks_reopen": 2, "tasks_delete": 3,
            "comments_add": 3, "sync_watch": 1, "sync_completed": 1, "watches_list": 0, "watches_close": 1}
        functions = agents["todoist"].manifest["functions"]
        assert all("account" in f["inputs"]["properties"] for f in functions)
        assert [f["id"] for f in functions if f.get("schedulable")] == ["sync_completed"]
        completed = agents["todoist"].function("sync_completed")
        assert completed["schedulable"] is True and "model" not in completed["calls"]

    def test_the_secret_signs_in_with_commas_between_scopes(self, agents):
        secret = resource(agents["todoist"].manifest, "secrets", "todoist")
        assert secret["oauth"]["provider"] == "todoist"
        assert secret["oauth"]["scope_separator"] == ","
        assert secret["oauth"]["scopes"] == ["data:read_write", "data:delete"]

    def test_status_reports_the_account(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success"
        assert result == {"connected": True, "email": TodoistStub.ACCOUNT,
                          "full_name": "Sam Haddad", "timezone": "Asia/Dubai"}


class TestReading:
    def test_open_tasks_come_with_app_priorities_projects_and_links(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "tasks_list", {})
        assert status == "success", result
        rows = {r["content"]: r for r in result["tasks"]}
        assert len(rows) == 4 and result["more"] is False
        chairs = rows["Order twenty desk chairs"]
        # API priority 4 is what the app shows as P1.
        assert chairs["priority"] == "P1"
        assert chairs["project"] == "Office Move" and chairs["labels"] == ["calls"]
        assert chairs["due_date"] == TODAY.isoformat()
        assert chairs["link"] == f"https://app.todoist.com/app/task/{todoist.chairs}"
        assert rows["Weekly supplier review"]["recurring"] is True
        assert rows["Collect keys for the new office"]["priority"] == "P4"

    def test_by_project_section_and_label_names(self, agents, todoist):
        ex, _ = executor(todoist)
        result, _ = invoke(agents, ex, "tasks_list",
                           {"project": "office move", "section": "Furniture"})
        assert [r["task_id"] for r in result["tasks"]] == [todoist.chairs]
        result, _ = invoke(agents, ex, "tasks_list", {"label": "Calls"})
        assert {r["task_id"] for r in result["tasks"]} == {todoist.chairs, todoist.quote}

    def test_a_filter_query_is_todoists_own(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "tasks_list", {"query": "today | overdue"})
        assert status == "success", result
        assert {r["task_id"] for r in result["tasks"]} == {todoist.chairs, todoist.quote}
        refused, status = invoke(agents, ex, "tasks_list",
                                 {"query": "today", "project": "Suppliers"})
        assert status == "error" and refused["kind"] == "invalid"
        bad, status = invoke(agents, ex, "tasks_list", {"query": "whenever-ish"})
        assert status == "error" and bad["kind"] == "invalid" and "query" in bad["error"]

    def test_a_long_list_is_paged_by_cursor(self, agents, todoist):
        ex, _ = executor(todoist)
        first, _ = invoke(agents, ex, "tasks_list", {"max_results": 3})
        assert len(first["tasks"]) == 3 and first["more"] is True
        rest, _ = invoke(agents, ex, "tasks_list",
                         {"max_results": 3, "cursor": first["next_cursor"]})
        assert len(rest["tasks"]) == 1 and rest["more"] is False

    def test_an_unknown_project_is_refused_with_the_real_names(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "tasks_list", {"project": "Warehouse"})
        assert status == "error" and result["kind"] == "invalid"
        assert known(result) == ["Inbox", "Office Move", "Suppliers"]
        assert "Office Move" in result["error"]

    def test_get_has_the_description_and_comment_count(self, agents, todoist):
        ex, _ = executor(todoist)
        invoke(agents, ex, "comments_add",
               {"task_id": todoist.chairs, "content": "Asked for a sample."}, chat_level=3)
        result, status = invoke(agents, ex, "tasks_get", {"task_id": todoist.chairs})
        assert status == "success", result
        assert result["description"] == "Northlight quote Q-2041"
        assert result["comment_count"] == 1 and result["completed"] is False
        missing, status = invoke(agents, ex, "tasks_get", {"task_id": "6X9999"})
        assert status == "error" and missing["kind"] == "not_found"

    def test_projects_sections_and_labels(self, agents, todoist):
        ex, _ = executor(todoist)
        projects, _ = invoke(agents, ex, "projects_list", {})
        move = next(p for p in projects["projects"] if p["name"] == "Office Move")
        assert move["shared"] is True and move["inbox"] is False
        sections, _ = invoke(agents, ex, "sections_list", {"project": "Office Move"})
        assert [s["name"] for s in sections["sections"]] == ["Furniture", "IT"]
        assert sections["sections"][0]["project"] == "Office Move"
        labels, _ = invoke(agents, ex, "labels_list", {})
        assert [l["name"] for l in labels["labels"]] == ["calls", "errands"]


class TestWriting:
    def test_create_refuses_an_unknown_project_or_label_and_sends_nothing(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "tasks_create",
                                {"content": "Book movers", "project": "Warehouse"})
        assert status == "error" and result["kind"] == "invalid"
        assert known(result) == ["Inbox", "Office Move", "Suppliers"]
        result, status = invoke(agents, ex, "tasks_create",
                                {"content": "Book movers", "labels": ["urgent"]})
        assert status == "error" and known(result) == ["calls", "errands"]
        assert todoist.writes == []

    def test_create_files_it_where_the_person_said(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "tasks_create", {
            "content": "Order standing desks", "project": "Office Move",
            "section": "furniture", "due_string": "tomorrow", "priority": "P1",
            "labels": ["CALLS"], "description": "Twenty, oak."})
        assert status == "success", result
        _, path, body = todoist.writes[-1]
        assert path == "tasks"
        assert body == {"content": "Order standing desks", "description": "Twenty, oak.",
                        "due_string": "tomorrow", "priority": 4, "labels": ["calls"],
                        "project_id": todoist.move, "section_id": todoist.furniture}
        assert result["priority"] == "P1" and result["project"] == "Office Move"
        assert result["due_date"] == (TODAY + timedelta(days=1)).isoformat()
        assert todoist.tasks[result["task_id"]]["section_id"] == todoist.furniture

    def test_a_bad_due_date_is_refused_before_anything_is_sent(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "tasks_create",
                                {"content": "Book movers", "due_date": "next week"})
        assert status == "error" and "YYYY-MM-DD" in result["error"]
        assert todoist.writes == []

    def test_update_complete_reopen_and_delete(self, agents, todoist):
        ex, _ = executor(todoist)
        updated, status = invoke(agents, ex, "tasks_update", {
            "task_id": todoist.keys, "due_date": "2026-10-01", "priority": "P2"})
        assert status == "success", updated
        assert updated["due_date"] == "2026-10-01" and updated["priority"] == "P2"
        assert todoist.tasks[todoist.keys]["priority"] == 3

        done, status = invoke(agents, ex, "tasks_complete", {"task_id": todoist.keys})
        assert status == "success", done
        assert done == {"task_id": todoist.keys, "content": "Collect keys for the new office",
                        "completed": True, "already": False, "recurring": False}
        assert todoist.tasks[todoist.keys]["checked"] is True
        again, _ = invoke(agents, ex, "tasks_complete", {"task_id": todoist.keys})
        assert again["already"] is True

        reopened, status = invoke(agents, ex, "tasks_reopen", {"task_id": todoist.keys})
        assert status == "success" and reopened["reopened"] is True
        assert todoist.tasks[todoist.keys]["checked"] is False

        refused, status = invoke(agents, ex, "tasks_delete", {"task_id": todoist.keys})
        assert status == "error" and refused.get("denied") is True
        assert todoist.tasks[todoist.keys]["is_deleted"] is False
        gone, status = invoke(agents, ex, "tasks_delete",
                              {"task_id": todoist.keys}, chat_level=3)
        assert status == "success" and gone["deleted"] is True
        assert todoist.tasks[todoist.keys]["is_deleted"] is True

    def test_a_recurring_task_says_it_recurs(self, agents, todoist):
        ex, _ = executor(todoist)
        done, _ = invoke(agents, ex, "tasks_complete", {"task_id": todoist.review})
        assert done["recurring"] is True

    def test_a_comment_needs_the_chats_trust(self, agents, todoist):
        ex, _ = executor(todoist)
        refused, status = invoke(agents, ex, "comments_add",
                                 {"task_id": todoist.chairs, "content": "Ordered."})
        assert status == "error" and refused.get("denied") is True
        assert todoist.comments == {}
        posted, status = invoke(agents, ex, "comments_add",
                                {"task_id": todoist.chairs, "content": "Ordered."}, chat_level=3)
        assert status == "success", posted
        assert posted["task"] == "Order twenty desk chairs"
        assert todoist.comments[posted["comment_id"]]["content"] == "Ordered."

    def test_a_project_is_created_under_a_real_parent(self, agents, todoist):
        ex, _ = executor(todoist)
        made, status = invoke(agents, ex, "projects_create",
                              {"name": "Reception", "parent": "Office Move"})
        assert status == "success", made
        assert made["parent_id"] == todoist.move and made["name"] == "Reception"
        refused, status = invoke(agents, ex, "projects_create",
                                 {"name": "Reception", "parent": "Warehouse"})
        assert status == "error" and refused["kind"] == "invalid"


class TestWatchingCompletions:
    def test_each_completion_is_handed_on_exactly_once(self, agents, todoist):
        ex, provider = executor(todoist)
        base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        # Ticked off before the watch began: marks the spot, is not news.
        earlier = todoist.add_task("Measure the meeting room", todoist.move)
        todoist.complete_at(earlier, base - timedelta(minutes=30))

        watch, status = invoke(agents, ex, "sync_watch", {"note": "mirror into Tasks"})
        assert status == "success", watch
        assert watch["since"] == todoist.tasks[earlier]["completed_at"]

        assert watch["source"] == "activity" and watch["recurring_seen"] is True

        quiet, status = invoke(agents, ex, "sync_completed", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "completed": [], "more": False, "problems": [], "closed": []}

        # Two tasks completed in the very same second, answered by
        # Todoist newest first: both arrive, oldest first, once.
        todoist.complete_at(todoist.quote, base)
        todoist.complete_at(todoist.chairs, base)
        news, _ = invoke(agents, ex, "sync_completed", {})
        assert sorted(r["task_id"] for r in news["completed"]) == sorted([todoist.quote, todoist.chairs])
        row = next(r for r in news["completed"] if r["task_id"] == todoist.chairs)
        assert row["watch_ref"] == watch["watch_ref"] and row["project"] == "Office Move"
        assert row["completed_at"] == todoist.tasks[todoist.chairs]["completed_at"]
        assert news["more"] is False

        again, _ = invoke(agents, ex, "sync_completed", {})
        assert again["completed"] == []

        # A third in that same second is neither lost nor shown twice.
        todoist.complete_at(todoist.keys, base)
        news, _ = invoke(agents, ex, "sync_completed", {})
        assert [r["task_id"] for r in news["completed"]] == [todoist.keys]
        again, _ = invoke(agents, ex, "sync_completed", {})
        assert again["completed"] == []
        # The ids on the cursor are the activity log's events, one per completion.
        keys = provider.data["watch"][watch["watch_ref"]]["keys"]
        assert len(set(keys["cursor_ids"].split(","))) == 3
        assert keys["account"] == TodoistStub.ACCOUNT and keys["status"] == "active"
        assert provider.data["watch"][watch["watch_ref"]]["values"]["note"] == "mirror into Tasks"

    def test_a_project_watch_pages_oldest_first(self, agents, todoist):
        ex, _ = executor(todoist)
        base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
        watch, status = invoke(agents, ex, "sync_watch", {
            "project": "Suppliers", "since": (base - timedelta(minutes=1)).isoformat()})
        assert status == "success", watch
        assert watch["project"] == "Suppliers"

        todoist.complete_at(todoist.chairs, base)                           # Office Move
        todoist.complete_at(todoist.review, base + timedelta(minutes=5))
        todoist.complete_at(todoist.quote, base + timedelta(minutes=1))
        extra = todoist.add_task("Pay the Northlight deposit", todoist.suppliers)
        todoist.complete_at(extra, base + timedelta(minutes=9))

        page, _ = invoke(agents, ex, "sync_completed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["task_id"] for r in page["completed"]] == [todoist.quote, todoist.review]
        assert page["more"] is True
        page, _ = invoke(agents, ex, "sync_completed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["task_id"] for r in page["completed"]] == [extra]
        assert page["more"] is False
        assert todoist.activity_queries and all(
            q.get("parent_project_id") == todoist.suppliers and q["event_type"] == "completed"
            for q in todoist.activity_queries)

    def test_a_bad_since_is_refused_and_records_nothing(self, agents, todoist):
        ex, provider = executor(todoist)
        result, status = invoke(agents, ex, "sync_watch", {"since": "last week"})
        assert status == "error" and result["kind"] == "invalid"
        assert provider.data == {}


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, todoist):
        ex, provider = executor(todoist, access_token="revoked")
        result, status = invoke(agents, ex, "tasks_list", {})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "tasks_create",
                                {"content": "Book movers", "project": "Office Move"})
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "sync_watch", {})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}
        assert todoist.writes == []

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = Chat(Records())
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success" and result["connected"] is False
        assert "No Todoist account" in result["problem"]


def two_accounts(todoist, second_token="revoked"):
    """Sam's account, and Omar's, whose token Todoist no longer takes."""
    omar = {**todoist.secret(second_token), "account": "omar@sidra.example"}
    provider = Records(secrets={"todoist": [todoist.secret(), omar]})
    return Chat(provider), provider


class TestMovingAndSubTasks:
    def test_a_task_moves_to_a_project_with_its_sub_tasks(self, agents, todoist):
        ex, _ = executor(todoist)
        step = todoist.add_task("Get three quotes", todoist.move, section_id=todoist.furniture,
                                parent_id=todoist.chairs)
        moved, status = invoke(agents, ex, "tasks_move", {"task_id": todoist.chairs, "project": "suppliers"})
        assert status == "success", moved
        assert todoist.writes[-1] == ("POST", f"tasks/{todoist.chairs}/move", {"project_id": todoist.suppliers})
        assert moved["project"] == "Suppliers" and moved["section_id"] == ""
        assert todoist.tasks[step]["project_id"] == todoist.suppliers

    def test_a_task_moves_to_a_section_or_under_another_task(self, agents, todoist):
        ex, _ = executor(todoist)
        moved, status = invoke(agents, ex, "tasks_move", {"task_id": todoist.keys, "section": "Furniture"})
        assert status == "success", moved
        assert todoist.writes[-1][2] == {"section_id": todoist.furniture}
        assert moved["section_id"] == todoist.furniture
        under, status = invoke(agents, ex, "tasks_move", {"task_id": todoist.quote, "parent_id": todoist.chairs})
        assert status == "success", under
        assert todoist.writes[-1][2] == {"parent_id": todoist.chairs}
        assert under["parent_id"] == todoist.chairs and under["project"] == "Office Move"

    def test_a_move_names_exactly_one_place_or_nothing_is_sent(self, agents, todoist):
        ex, _ = executor(todoist)
        for inputs in ({"task_id": todoist.keys},
                       {"task_id": todoist.keys, "project": "Suppliers", "parent_id": todoist.chairs},
                       {"task_id": todoist.keys, "parent_id": todoist.keys}):
            refused, status = invoke(agents, ex, "tasks_move", inputs)
            assert status == "error" and refused["kind"] == "invalid", inputs
        refused, status = invoke(agents, ex, "tasks_move", {"task_id": todoist.keys, "project": "Warehouse"})
        assert status == "error" and known(refused) == ["Inbox", "Office Move", "Suppliers"]
        assert todoist.writes == []

    def test_an_update_never_moves_a_task(self, agents, todoist):
        # Todoist moves a task only through its move endpoint: the stub refuses project_id on an update, as v1 does.
        ex, _ = executor(todoist)
        updated, status = invoke(agents, ex, "tasks_update", {"task_id": todoist.keys, "content": "Collect keys"})
        assert status == "success", updated
        assert set(todoist.writes[-1][2]) == {"content"}

    def test_sub_tasks_are_added_listed_and_shown_on_their_parent(self, agents, todoist):
        ex, _ = executor(todoist)
        made, status = invoke(agents, ex, "tasks_create",
                              {"content": "Book the delivery", "parent_id": todoist.chairs})
        assert status == "success", made
        assert todoist.writes[-1][2] == {"content": "Book the delivery", "parent_id": todoist.chairs}
        assert made["parent_id"] == todoist.chairs and made["project"] == "Office Move"
        listed, status = invoke(agents, ex, "tasks_list", {"parent_id": todoist.chairs})
        assert status == "success" and [t["task_id"] for t in listed["tasks"]] == [made["task_id"]]
        parent, _ = invoke(agents, ex, "tasks_get", {"task_id": todoist.chairs})
        assert parent["subtasks"] == [{"task_id": made["task_id"], "content": "Book the delivery"}]
        refused, status = invoke(agents, ex, "tasks_create",
                                 {"content": "x", "parent_id": todoist.chairs, "project": "Suppliers"})
        assert status == "error" and refused["kind"] == "invalid"
        missing, status = invoke(agents, ex, "tasks_list", {"parent_id": "6X9999"})
        assert status == "error" and missing["kind"] == "not_found"

    def test_arabic_survives_the_round_trip(self, agents, todoist):
        ex, _ = executor(todoist)
        dubai = todoist.add_project("مكتب دبي")
        todoist.add_label("مكالمات")
        made, status = invoke(agents, ex, "tasks_create", {
            "content": "اطلب عشرين كرسي مكتب", "project": "مكتب دبي", "labels": ["مكالمات"],
            "description": "عرض سعر نورثلايت"})
        assert status == "success", made
        assert todoist.tasks[made["task_id"]]["content"] == "اطلب عشرين كرسي مكتب"
        listed, _ = invoke(agents, ex, "tasks_list", {"project": dubai})
        assert [(t["content"], t["project"], t["labels"]) for t in listed["tasks"]] == [
            ("اطلب عشرين كرسي مكتب", "مكتب دبي", ["مكالمات"])]


class TestWatchesThatLastAndFail:
    def test_a_recurring_completion_reaches_the_watch_and_the_task_stays_open(self, agents, todoist):
        ex, _ = executor(todoist)
        watch, _ = invoke(agents, ex, "sync_watch", {})
        assert watch["recurring_seen"] is True and "warning" not in watch
        next_week = todoist.tasks[todoist.review]["due"]["date"]
        done, _ = invoke(agents, ex, "tasks_complete", {"task_id": todoist.review})
        assert done["recurring"] is True and todoist.tasks[todoist.review]["checked"] is False
        news, _ = invoke(agents, ex, "sync_completed", {})
        [row] = news["completed"]
        assert (row["task_id"], row["content"], row["recurring"]) == (
            todoist.review, "Weekly supplier review", True)
        assert row["next_due_date"] > next_week and "due_date" not in row
        # The next occurrence ticked off is news again: one row per completion, not per task.
        invoke(agents, ex, "tasks_complete", {"task_id": todoist.review})
        again, _ = invoke(agents, ex, "sync_completed", {})
        assert [r["task_id"] for r in again["completed"]] == [todoist.review]

    def test_without_an_activity_log_it_reads_completed_tasks_and_says_what_it_misses(self, agents, todoist):
        todoist.activity_log = False
        ex, provider = executor(todoist)
        watch, status = invoke(agents, ex, "sync_watch", {})
        assert status == "success", watch
        assert watch["source"] == "completed" and watch["recurring_seen"] is False
        assert "recurring" in watch["warning"]
        invoke(agents, ex, "tasks_complete", {"task_id": todoist.review})
        invoke(agents, ex, "tasks_complete", {"task_id": todoist.keys})
        news, _ = invoke(agents, ex, "sync_completed", {})
        assert [r["task_id"] for r in news["completed"]] == [todoist.keys]
        assert provider.data["watch"][watch["watch_ref"]]["keys"]["source"] == "completed"

    def test_a_watch_whose_project_is_gone_closes_and_stays_closed(self, agents, todoist):
        ex, provider = executor(todoist)
        watch, _ = invoke(agents, ex, "sync_watch", {"project": "Suppliers"})
        todoist.delete_project(todoist.suppliers)
        news, status = invoke(agents, ex, "sync_completed", {})
        assert status == "success", news
        assert [c["watch_ref"] for c in news["closed"]] == [watch["watch_ref"]] and news["problems"] == []
        assert "Suppliers" in news["closed"][0]["why"]
        assert provider.data["watch"][watch["watch_ref"]]["keys"]["status"] == "closed"
        later, _ = invoke(agents, ex, "sync_completed", {})
        assert later == {"checked": 0, "completed": [], "more": False, "problems": [], "closed": []}

    def test_one_failing_watch_keeps_its_place_and_the_others_are_handed_on(self, agents, todoist):
        ex, provider = two_accounts(todoist)
        mine, status = invoke(agents, ex, "sync_watch", {"account": "sam"})
        assert status == "success", mine
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        omars = provider.create_data("watch", {"since": since, "cursor_ids": "", "status": "active",
                                               "source": "activity", "account": "omar@sidra.example"})
        invoke(agents, ex, "tasks_complete", {"task_id": todoist.keys, "account": "sam@sidra.example"})
        news, status = invoke(agents, ex, "sync_completed", {})
        assert status == "success", news
        assert [(r["watch_ref"], r["task_id"]) for r in news["completed"]] == [(mine["watch_ref"], todoist.keys)]
        assert [(p["watch_ref"], p["kind"]) for p in news["problems"]] == [(omars, "auth")]
        assert provider.data["watch"][omars]["keys"]["since"] == since
        assert provider.data["watch"][mine["watch_ref"]]["keys"]["since"] != mine["since"]
        # Unnamed with two connected, a function says which are there.
        refused, status = invoke(agents, ex, "tasks_list", {})
        assert status == "error" and "omar@sidra.example" in refused["error"]

    def test_watches_are_listed_and_closed(self, agents, todoist):
        ex, _ = executor(todoist)
        watch, _ = invoke(agents, ex, "sync_watch", {"project": "Office Move"})
        listed, status = invoke(agents, ex, "watches_list", {})
        assert status == "success", listed
        assert [(w["watch_ref"], w["status"], w["project"], w["account"], w["source"])
                for w in listed["watches"]] == [
            (watch["watch_ref"], "active", "Office Move", TodoistStub.ACCOUNT, "activity")]
        closed, status = invoke(agents, ex, "watches_close", {"watch_ref": watch["watch_ref"]})
        assert status == "success"
        assert closed == {"watch_ref": watch["watch_ref"], "closed": True, "already": False}
        assert invoke(agents, ex, "watches_list", {})[0]["watches"] == []
        assert invoke(agents, ex, "watches_list", {"include_closed": True})[0]["total"] == 1
        invoke(agents, ex, "tasks_complete", {"task_id": todoist.keys})
        assert invoke(agents, ex, "sync_completed", {})[0]["checked"] == 0
        again, _ = invoke(agents, ex, "watches_close", {"watch_ref": watch["watch_ref"]})
        assert again["already"] is True
        missing, status = invoke(agents, ex, "watches_close", {"watch_ref": "data_404"})
        assert status == "error" and missing["kind"] == "not_found"

    def test_a_watch_from_1_0_0_still_counts(self, agents, todoist):
        ex, provider = executor(todoist)
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        old = provider.create_data("watch", {"since": since, "cursor_ids": "", "status": "watching"})
        invoke(agents, ex, "tasks_complete", {"task_id": todoist.keys})
        news, _ = invoke(agents, ex, "sync_completed", {})
        assert [r["task_id"] for r in news["completed"]] == [todoist.keys]
        assert provider.data["watch"][old]["keys"]["status"] == "active"


class TestABusyTodoist:
    def test_a_429_is_waited_out_as_retry_after_says(self, agents, todoist):
        ex, _ = executor(todoist)
        todoist.rate_limit = 1
        listed, status = invoke(agents, ex, "tasks_list", {})
        assert status == "success" and len(listed["tasks"]) == 4

    def test_a_write_refused_as_busy_is_done_once(self, agents, todoist):
        ex, _ = executor(todoist)
        invoke(agents, ex, "labels_list", {})
        todoist.rate_limit = 1
        made, status = invoke(agents, ex, "tasks_create", {"content": "Book movers"})
        assert status == "success", made
        assert [w for w in todoist.writes if w[1] == "tasks"] == [("POST", "tasks", {"content": "Book movers"})]

    def test_a_503_after_the_write_was_done_does_not_do_it_twice(self, agents, todoist):
        ex, _ = executor(todoist)
        todoist.busy_after_write = True
        made, status = invoke(agents, ex, "tasks_create", {"content": "Book movers"})
        assert status == "success", made
        assert sum(1 for t in todoist.tasks.values() if t["content"] == "Book movers") == 1
        assert len(set(todoist.request_ids[-2:])) == 1

    def test_a_long_wait_is_rate_not_a_failure(self, agents, todoist):
        ex, _ = executor(todoist)
        invoke(agents, ex, "sync_watch", {})
        todoist.rate_limit = 50
        news, status = invoke(agents, ex, "sync_completed", {})
        assert status == "success" and [p["kind"] for p in news["problems"]] == ["rate"]
        todoist.rate_limit = 50
        listed, status = invoke(agents, ex, "tasks_list", {})
        assert status == "error" and listed["kind"] == "rate"
