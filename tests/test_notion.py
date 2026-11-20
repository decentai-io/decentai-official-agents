"""The Notion agent, its program started and spoken to in the standard's
messages, against a loopback Notion API holding Sidra Office Supplies'
workspace: an "Office move tasks"
database with three rows, a "Showroom launch plan" page with a checklist,
a folded supplier list and a child page, and "Board minutes", which
exists but was not shared with the integration.
"""

import json

import pytest


from tests.harness import Chat, Records, probe, run
from tests.conftest import resource
from tests.notion_stub import NotionStub

TASKS_SCHEMA = {
    "Name": "title",
    "Status": ("status", ["Not started", "In progress", "Done"]),
    "Owner": "rich_text",
    "Due": "date",
    "Tags": ("multi_select", ["Furniture", "IT", "Facilities"]),
    "Signed off": "checkbox",
    "Budget": "number",
}


class Workspace:
    """The fictional workspace, and the ids a test needs."""

    def __init__(self, stub: NotionStub):
        self.stub = stub
        stub.clock = "2026-09-10T08:00:00.000Z"
        self.tasks = stub.add_database("Office move tasks", TASKS_SCHEMA)
        self.desks = stub.add_row(self.tasks, {
            "Name": "Order standing desks", "Status": "In progress", "Owner": "Dana",
            "Due": "2026-09-18", "Tags": ["Furniture"], "Signed off": False, "Budget": 4200},
            edited="2026-09-10T08:00:00.000Z")
        # Two rows edited in the same minute: the newest minute is shared.
        self.rack = stub.add_row(self.tasks, {
            "Name": "Move network rack", "Status": "Not started", "Owner": "Omar",
            "Due": "2026-09-25", "Tags": ["IT"], "Signed off": False, "Budget": 900},
            created="2026-09-09T10:00:00.000Z", edited="2026-09-10T08:30:00.000Z")
        self.movers = stub.add_row(self.tasks, {
            "Name": "Book movers", "Status": "Done", "Owner": "Dana",
            "Due": "2026-09-12", "Tags": ["Facilities"], "Signed off": True, "Budget": 1500},
            created="2026-09-09T10:00:00.000Z", edited="2026-09-10T08:30:00.000Z")

        self.plan = stub.add_page("Showroom launch plan", edited="2026-09-09T16:00:00.000Z")
        stub.add_block(self.plan, "heading_1", "Launch goals")
        stub.add_block(self.plan, "paragraph", "Open the showroom on 1 October.")
        stub.add_block(self.plan, "to_do", "Confirm opening date", checked=True)
        stub.add_block(self.plan, "to_do", "Print price cards", checked=False)
        contacts = stub.add_block(self.plan, "toggle", "Supplier contacts")
        stub.add_block(contacts, "paragraph", "Northlight Seating: quotes@northlight-seating.example")
        harbourline = stub.add_block(contacts, "bulleted_list_item", "Harbourline Desks")
        stub.add_block(harbourline, "paragraph", "Ask for Omar, weekdays only.")
        stub.add_block(self.plan, "numbered_list_item", "Clean the floor")
        stub.add_block(self.plan, "numbered_list_item", "Hang the signs")
        stub.add_block(self.plan, "callout", "Keys are with reception.")
        self.floor_plan = stub.add_page("Floor plan notes", parent_page=self.plan,
                                        edited="2026-09-09T15:00:00.000Z")
        stub.add_comment(self.plan, "Can we open a day earlier?")

        self.minutes = stub.add_page("Board minutes", edited="2026-09-08T12:00:00.000Z")
        stub.restricted.add(self.minutes)
        stub.clock = "2026-09-10T09:00:00.000Z"


@pytest.fixture
def ws():
    stub = NotionStub().start()
    yield Workspace(stub)
    stub.stop()


def executor(ws, access_token="at-1"):
    provider = Records(
        secrets={"notion": ws.stub.secret(access_token)})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["notion"], name, inputs, chat_level=chat_level))


def ok(result_status):
    result, status = result_status
    assert status == "success", result
    return result


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["notion"]
        assert probe(agent) == []

    def test_levels_follow_who_sees_the_change(self, agents):
        """A page or row the person can undo is level 2; a comment, which others see at once, is 3."""
        functions = {f["id"]: f for f in agents["notion"].manifest["functions"]}
        levels = {name: f["level"] for name, f in functions.items()}
        assert agents["notion"].manifest["agent"]["version"] == "1.1.0"
        assert {n for n, level in levels.items() if level == 3} == {"comments_add"}
        assert {n for n, level in levels.items() if level == 2} == {
            "pages_create", "pages_update", "pages_archive", "blocks_append"}
        assert {n for n, level in levels.items() if level == 1} == {
            "watch_database", "watch_changes", "watches_close"}
        assert all("account" in f["inputs"]["properties"] for f in functions.values())
        changes = functions["watch_changes"]
        assert changes["schedulable"] is True and "model" not in changes["calls"]
        assert [n for n, f in functions.items() if f.get("schedulable")] == ["watch_changes"]

    def test_the_sign_in_is_declared_the_way_notion_wants_it(self, agents):
        """The account is read from the token's own answer; the url is
        there only because the platform's schema asks for one."""
        oauth = resource(agents["notion"].manifest, "secrets", "notion")["oauth"]
        assert oauth["provider"] == "notion"
        assert (oauth["token_auth"], oauth["token_format"]) == ("basic", "json")
        assert oauth["authorize_params"] == {"owner": "user"}
        assert oauth["identity"] == {"source": "token", "url": "https://api.notion.com/v1/users/me",
                                     "field": "owner.user.person.email"}

    def test_status_names_the_workspace(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "account_status", {}))
        assert result == {"connected": True, "workspace": "Sidra Office Supplies",
                          "integration": "DecentAI", "account": NotionStub.ACCOUNT}


class TestFinding:
    def test_search_finds_a_page_by_title(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "search_find", {"query": "launch"}))
        [row] = result["results"]
        assert (row["id"], row["kind"], row["title"], row["parent"]) == (
            ws.plan, "page", "Showroom launch plan", "workspace")
        assert row["url"].startswith("https://www.notion.so/")

    def test_search_narrows_to_databases_and_never_shows_what_was_not_shared(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "search_find", {"kind": "database"}))
        assert [r["title"] for r in result["results"]] == ["Office move tasks"]
        everything = ok(invoke(agents, ex, "search_find", {}))
        assert "Board minutes" not in [r["title"] for r in everything["results"]]
        # Newest edited first.
        edited = [r["last_edited"] for r in everything["results"]]
        assert edited == sorted(edited, reverse=True)

    def test_search_pages_with_a_cursor(self, agents, ws):
        ex, _ = executor(ws)
        first = ok(invoke(agents, ex, "search_find", {"max_results": 2}))
        assert len(first["results"]) == 2 and first["next_cursor"]
        rest = ok(invoke(agents, ex, "search_find",
                         {"start_cursor": first["next_cursor"]}))
        ids = [r["id"] for r in first["results"] + rest["results"]]
        assert len(ids) == len(set(ids)) and "next_cursor" not in rest


class TestReadingPages:
    def test_a_page_reads_as_lines_with_one_level_of_nesting(self, agents, ws):
        ex, _ = executor(ws)
        page = ok(invoke(agents, ex, "pages_read", {"page_id": ws.plan}))
        assert page["title"] == "Showroom launch plan"
        assert page["properties"] == {"title": "Showroom launch plan"}
        lines = [(b["depth"], b["text"]) for b in page["blocks"]]
        assert lines == [
            (0, "# Launch goals"),
            (0, "Open the showroom on 1 October."),
            (0, "[x] Confirm opening date"),
            (0, "[ ] Print price cards"),
            (0, "> Supplier contacts"),
            (1, "Northlight Seating: quotes@northlight-seating.example"),
            (1, "- Harbourline Desks (has nested content, not shown)"),
            (0, "1. Clean the floor"),
            (0, "2. Hang the signs"),
            (0, "Note: Keys are with reception."),
            (0, f"[page] Floor plan notes (page_id {ws.floor_plan})"),
        ]
        assert (page["total"], page["more"], page["total_capped"]) == (9, False, False)
        # The child page is named, never walked into; the deeper paragraph
        # is not fetched.
        assert "Omar" not in str(page["blocks"])

    def test_a_long_page_is_read_in_windows(self, agents, ws):
        ex, _ = executor(ws)
        first = ok(invoke(agents, ex, "pages_read", {"page_id": ws.plan, "max_blocks": 4}))
        assert [b["index"] for b in first["blocks"]] == [0, 1, 2, 3]
        assert (first["total"], first["more"], first["next_from"]) == (9, True, 4)
        second = ok(invoke(agents, ex, "pages_read",
                           {"page_id": ws.plan, "from": first["next_from"], "max_blocks": 4}))
        assert second["blocks"][0]["text"] == "> Supplier contacts"
        assert second["next_from"] == 8
        # Numbering counts from the list's start, not the window's.
        third = ok(invoke(agents, ex, "pages_read",
                          {"page_id": ws.plan, "from": 6, "max_blocks": 4}))
        assert third["blocks"][0]["text"] == "2. Hang the signs"
        assert third["more"] is False and "next_from" not in third

    def test_a_long_paragraph_is_clipped(self, agents, ws):
        ws.stub.add_block(ws.plan, "paragraph", "Price list. " * 400)
        ex, _ = executor(ws)
        page = ok(invoke(agents, ex, "pages_read", {"page_id": ws.plan, "from": 9}))
        text = page["blocks"][0]["text"]
        assert len(text) <= 2000 and text.endswith("…")

    def test_a_row_reads_its_properties_as_text(self, agents, ws):
        ex, _ = executor(ws)
        row = ok(invoke(agents, ex, "pages_read", {"page_id": ws.movers}))
        assert row["properties"] == {
            "Name": "Book movers", "Status": "Done", "Owner": "Dana", "Due": "2026-09-12",
            "Tags": "Facilities", "Signed off": "yes", "Budget": "1500"}
        assert row["parent"] == f"database {ws.tasks}"
        assert row["blocks"] == [] and row["total"] == 0

    def test_comments_are_listed(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "comments_list", {"page_id": ws.plan}))
        [comment] = result["comments"]
        assert comment["text"] == "Can we open a day earlier?"
        assert comment["author_id"] == "user-dana"


class TestReadingDatabases:
    def test_the_schema_names_every_property_and_option(self, agents, ws):
        ex, _ = executor(ws)
        schema = ok(invoke(agents, ex, "databases_get", {"database_id": ws.tasks}))
        assert schema["title"] == "Office move tasks"
        by_name = {p["name"]: p for p in schema["properties"]}
        assert by_name["Status"] == {"name": "Status", "type": "status",
                                     "options": ["Not started", "In progress", "Done"]}
        assert by_name["Tags"]["options"] == ["Furniture", "IT", "Facilities"]
        assert by_name["Due"] == {"name": "Due", "type": "date"}

    def _names(self, result):
        return sorted(r["title"] for r in result["rows"])

    def test_rows_are_filtered_simply(self, agents, ws):
        ex, _ = executor(ws)
        query = lambda **kw: ok(invoke(agents, ex, "databases_query",
                                       {"database_id": ws.tasks, **kw}))
        assert self._names(query(filter_property="Status", filter_operator="equals",
                                 filter_value="in progress")) == ["Order standing desks"]
        assert self._names(query(filter_property="Tags", filter_operator="contains",
                                 filter_value="IT")) == ["Move network rack"]
        assert self._names(query(filter_property="Signed off", filter_operator="checkbox",
                                 filter_value=True)) == ["Book movers"]
        assert self._names(query(filter_property="Due", filter_operator="on_or_after",
                                 filter_value="2026-09-15")) == [
            "Move network rack", "Order standing desks"]
        # The option was sent as the schema spells it.
        assert ws.stub.queries[0]["filter"] == {"property": "Status",
                                                "status": {"equals": "In progress"}}

    def test_rows_sort_and_page(self, agents, ws):
        ex, _ = executor(ws)
        first = ok(invoke(agents, ex, "databases_query", {
            "database_id": ws.tasks, "sort_property": "Due", "sort_direction": "ascending",
            "max_results": 2}))
        assert [r["title"] for r in first["rows"]] == ["Book movers", "Order standing desks"]
        assert first["rows"][0]["properties"]["Signed off"] == "yes"
        rest = ok(invoke(agents, ex, "databases_query", {
            "database_id": ws.tasks, "sort_property": "Due", "sort_direction": "ascending",
            "start_cursor": first["next_cursor"]}))
        assert [r["title"] for r in rest["rows"]] == ["Move network rack"]
        assert "next_cursor" not in rest

    def test_a_filter_on_an_option_that_does_not_exist_is_refused(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "databases_query", {
            "database_id": ws.tasks, "filter_property": "Status",
            "filter_operator": "equals", "filter_value": "Blocked"})
        assert status == "error" and result["kind"] == "invalid"
        assert "Not started, In progress, Done" in result["error"]
        assert ws.stub.queries == []


class TestWriting:
    def test_a_row_is_checked_against_the_schema_sent_and_recorded(self, agents, ws):
        ex, provider = executor(ws)
        made = ok(invoke(agents, ex, "pages_create", {
            "database_id": ws.tasks, "title": "Order desk lamps",
            "properties": {"Status": "Not started", "Owner": "Dana", "Due": "2026-09-20",
                           "Tags": ["Furniture"], "Budget": 350, "Signed off": False},
            "paragraphs": ["Warm white, twelve units."]}, chat_level=3))
        [sent] = ws.stub.created
        # Since 2025-09-03 a row's parent is its data source, never the database.
        assert sent["parent"] == {"type": "data_source_id", "data_source_id": ws.stub.source_of(ws.tasks)}
        assert sent["properties"]["Status"] == {"status": {"name": "Not started"}}
        assert sent["properties"]["Tags"] == {"multi_select": [{"name": "Furniture"}]}
        assert sent["properties"]["Due"] == {"date": {"start": "2026-09-20"}}
        assert sent["properties"]["Name"]["title"][0]["text"]["content"] == "Order desk lamps"
        assert sent["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == \
            "Warm white, twelve units."
        assert made["title"] == "Order desk lamps"
        assert made["parent"] == f"database {ws.tasks}"
        record = provider.data["write"][made["write_ref"]]
        assert record["keys"]["action"] == "page_created"
        assert record["keys"]["page_id"] == made["page_id"]
        assert record["values"]["detail"]["sent"]["Status"] == "Not started"
        row = ok(invoke(agents, ex, "pages_read", {"page_id": made["page_id"]}))
        assert row["properties"]["Budget"] == "350"
        assert row["blocks"][0]["text"] == "Warm white, twelve units."

    def test_an_unknown_property_is_refused_with_the_real_ones_listed(self, agents, ws):
        ex, provider = executor(ws)
        result, status = invoke(agents, ex, "pages_create", {
            "database_id": ws.tasks, "title": "Order lamps",
            "properties": {"Priority": "High"}}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert "no property 'Priority'" in result["error"]
        assert "Name, Status, Owner, Due, Tags, Signed off, Budget" in result["error"]
        assert ws.stub.created == [] and provider.data == {}

    def test_an_unknown_option_is_refused_with_the_options_listed(self, agents, ws):
        ex, provider = executor(ws)
        result, status = invoke(agents, ex, "pages_create", {
            "database_id": ws.tasks, "title": "Order lamps",
            "properties": {"Tags": ["Furniture", "Lighting"]}}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert "'Lighting' is not an option of 'Tags'" in result["error"]
        assert "Furniture, IT, Facilities" in result["error"]
        assert ws.stub.created == [] and provider.data == {}

    def test_a_computed_or_badly_typed_value_is_refused(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "pages_create", {
            "database_id": ws.tasks, "properties": {"Due": "next Friday"}}, chat_level=3)
        assert status == "error" and "YYYY-MM-DD" in result["error"]
        result, status = invoke(agents, ex, "pages_create", {
            "database_id": ws.tasks, "properties": {"Budget": "a lot"}}, chat_level=3)
        assert status == "error" and "number" in result["error"]
        assert ws.stub.created == []

    def test_a_page_under_a_page_takes_a_title_and_paragraphs(self, agents, ws):
        ex, provider = executor(ws)
        made = ok(invoke(agents, ex, "pages_create", {
            "parent_page_id": ws.plan, "title": "Opening day checklist",
            "paragraphs": ["Doors at 10.", "Coffee for forty."]}, chat_level=3))
        assert ws.stub.created[0]["parent"] == {"type": "page_id", "page_id": ws.plan}
        page = ok(invoke(agents, ex, "pages_read", {"page_id": made["page_id"]}))
        assert page["title"] == "Opening day checklist"
        assert [b["text"] for b in page["blocks"]] == ["Doors at 10.", "Coffee for forty."]
        refused, status = invoke(agents, ex, "pages_create", {
            "parent_page_id": ws.plan, "title": "x", "properties": {"Status": "Done"}},
            chat_level=3)
        assert status == "error" and "database" in refused["error"]
        assert len(provider.data["write"]) == 1

    def test_an_update_records_what_was_there_before(self, agents, ws):
        ex, provider = executor(ws)
        result = ok(invoke(agents, ex, "pages_update", {
            "page_id": ws.desks, "properties": {"Status": "Done", "Budget": 3950}},
            chat_level=3))
        assert result["changed"] == ["Status", "Budget"]
        assert result["previous"] == {"Status": "In progress", "Budget": "4200"}
        assert result["now"] == {"Status": "Done", "Budget": "3950"}
        assert ws.stub.updated[0]["properties"]["Status"] == {"status": {"name": "Done"}}
        record = provider.data["write"][result["write_ref"]]
        assert (record["keys"]["action"], record["values"]["changed"]) == (
            "page_updated", "Status, Budget")
        assert record["keys"]["parent_id"] == ws.tasks
        assert record["values"]["detail"]["previous"] == {"Status": "In progress",
                                                           "Budget": "4200"}

    def test_an_update_with_an_unknown_option_sends_nothing(self, agents, ws):
        ex, provider = executor(ws)
        result, status = invoke(agents, ex, "pages_update", {
            "page_id": ws.desks, "properties": {"Status": "Blocked"}}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert ws.stub.updated == [] and provider.data == {}

    def test_blocks_are_appended_in_order(self, agents, ws):
        ex, provider = executor(ws)
        result = ok(invoke(agents, ex, "blocks_append", {
            "page_id": ws.plan, "items": [
                {"type": "to_do", "text": "Order price card stock"},
                {"type": "bullet", "text": "Ask about Saturday delivery"},
                {"text": "Signed off by Dana."}]}, chat_level=3))
        assert result["appended"] == 3 and len(result["block_ids"]) == 3
        page = ok(invoke(agents, ex, "pages_read", {"page_id": ws.plan, "from": 9}))
        assert [b["text"] for b in page["blocks"]] == [
            "[ ] Order price card stock", "- Ask about Saturday delivery", "Signed off by Dana."]
        record = provider.data["write"][result["write_ref"]]
        assert record["keys"]["action"] == "blocks_appended"

    def test_a_comment_is_added_and_recorded(self, agents, ws):
        ex, provider = executor(ws)
        result = ok(invoke(agents, ex, "comments_add", {
            "page_id": ws.plan, "text": "Opening a day earlier works for the movers."},
            chat_level=3))
        assert ws.stub.commented[0]["parent"] == {"page_id": ws.plan}
        listed = ok(invoke(agents, ex, "comments_list", {"page_id": ws.plan}))
        assert [c["text"] for c in listed["comments"]][-1] == \
            "Opening a day earlier works for the movers."
        assert provider.data["write"][result["write_ref"]]["keys"]["action"] == \
            "comment_added"

    def test_writing_needs_the_chats_trust(self, agents, ws):
        ex, provider = executor(ws)
        refused, status = invoke(agents, ex, "pages_create", {
            "parent_page_id": ws.plan, "title": "Unapproved"}, chat_level=1)
        assert status == "error" and refused.get("denied") is True
        assert ws.stub.created == [] and provider.data == {}


class TestWatching:
    def _changes(self, agents, ex, **inputs):
        return ok(invoke(agents, ex, "watch_changes", inputs))

    def test_created_and_edited_rows_are_handed_on_exactly_once(self, agents, ws):
        stub = ws.stub
        ex, _ = executor(ws)
        watch = ok(invoke(agents, ex, "watch_database",
                          {"database_id": ws.tasks, "note": "tell me what moved"}))
        # From now in Notion's clock: the newest edit marks the spot, and
        # both rows on that minute count as seen.
        assert (watch["database"], watch["since"]) == ("Office move tasks",
                                                       "2026-09-10T08:30:00.000Z")
        assert self._changes(agents, ex) == {"checked": 1, "rows": [], "more": False, "problems": [],
                                             "closed": []}
        assert stub.queries[-1]["filter"] == {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": "2026-09-10T08:30:00.000Z"}}
        assert stub.queries[-1]["sorts"] == [{"timestamp": "last_edited_time",
                                              "direction": "ascending"}]

        # 09:10 — a new row, and an old row edited, in the same minute.
        stub.clock = "2026-09-10T09:10:00.000Z"
        lamps = stub.add_row(ws.tasks, {"Name": "Order desk lamps", "Status": "Not started"})
        stub.edit_row(ws.desks, {"Status": "Done"})
        news = self._changes(agents, ex)
        rows = {r["page_id"]: r for r in news["rows"]}
        assert set(rows) == {lamps, ws.desks} and news["more"] is False
        assert rows[lamps]["change"] == "created"
        assert rows[ws.desks]["change"] == "edited"
        assert rows[ws.desks]["properties"]["Status"] == "Done"
        assert rows[ws.desks]["watch_ref"] == watch["watch_ref"]
        assert rows[ws.desks]["database"] == "Office move tasks"
        assert self._changes(agents, ex)["rows"] == []

        # Still 09:10: a third row edited on the cursor's own minute is
        # new, and is reported once.
        stub.edit_row(ws.rack, {"Owner": "Dana"})
        news = self._changes(agents, ex)
        assert [(r["page_id"], r["change"]) for r in news["rows"]] == [(ws.rack, "edited")]
        assert self._changes(agents, ex)["rows"] == []

        # The honest limit: a second edit to a reported row within the
        # same minute carries the same time and is not seen...
        stub.edit_row(ws.desks, {"Budget": 3900})
        assert self._changes(agents, ex)["rows"] == []
        # ...until an edit in a later minute.
        stub.clock = "2026-09-10T09:11:00.000Z"
        stub.edit_row(ws.desks, {"Budget": 3850})
        news = self._changes(agents, ex, watch_ref=watch["watch_ref"])
        assert [(r["page_id"], r["change"]) for r in news["rows"]] == [(ws.desks, "edited")]
        assert news["rows"][0]["properties"]["Budget"] == "3850"
        assert self._changes(agents, ex)["rows"] == []

    def test_a_busy_minute_is_handed_on_in_turns_and_says_more(self, agents, ws):
        stub = ws.stub
        ex, _ = executor(ws)
        watch = ok(invoke(agents, ex, "watch_database", {"database_id": ws.tasks}))
        stub.clock = "2026-09-10T09:20:00.000Z"
        made = {stub.add_row(ws.tasks, {"Name": f"Label boxes, floor {n}"}) for n in (1, 2, 3)}
        first = self._changes(agents, ex, watch_ref=watch["watch_ref"], max_results=2)
        assert len(first["rows"]) == 2 and first["more"] is True
        second = self._changes(agents, ex, watch_ref=watch["watch_ref"], max_results=2)
        assert len(second["rows"]) == 1 and second["more"] is False
        assert {r["page_id"] for r in first["rows"] + second["rows"]} == made
        assert all(r["change"] == "created" for r in first["rows"] + second["rows"])
        assert self._changes(agents, ex)["rows"] == []


class TestTheConnection:
    def test_a_page_not_shared_says_how_to_share_it(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "pages_read", {"page_id": ws.minutes})
        assert status == "error" and result["kind"] == "denied"
        assert "Connections" in result["error"] and "integration" in result["error"]

    def test_an_unknown_id_carries_the_same_hint(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "databases_get",
                                {"database_id": "00000000-0000-4000-8000-000000000000"})
        assert status == "error" and result["kind"] == "not_found"
        assert "not shared with the integration" in result["error"]
        assert "Connections" in result["error"]

    def test_a_short_rate_limit_is_waited_out(self, agents, ws):
        """Notion's 429 with Retry-After: waited as told, then the answer, for a search (a POST that reads)."""
        ws.stub.limit_next, ws.stub.retry_after = 2, "1"
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "search_find", {"query": "launch"}))
        assert [r["id"] for r in result["results"]] == [ws.plan] and ws.stub.limited == 2

    def test_a_write_refused_as_busy_is_sent_again_once_it_did_nothing(self, agents, ws):
        ex, _ = executor(ws)
        ok(invoke(agents, ex, "pages_read", {"page_id": ws.plan}))
        ws.stub.limit_next, ws.stub.retry_after = 1, "1"
        ok(invoke(agents, ex, "blocks_append", {"page_id": ws.plan, "items": [{"text": "Once."}]},
                  chat_level=2))
        assert len(ws.stub.appended) == 1 and ws.stub.limited == 1

    def test_a_long_rate_limit_says_when_to_try_again(self, agents, ws):
        ws.stub.limit_next, ws.stub.retry_after = 10, "40"
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "search_find", {"query": "launch"})
        assert status == "error" and result["kind"] == "rate"
        assert "40 seconds" in result["error"] and ws.stub.limited == 1

    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, ws):
        ex, provider = executor(ws, access_token="expired")
        result, status = invoke(agents, ex, "search_find", {"query": "launch"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "pages_create", {
            "database_id": ws.tasks, "title": "Order lamps"}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "watch_database", {"database_id": ws.tasks})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {} and ws.stub.created == []

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = Chat(Records())
        result = ok(invoke(agents, ex, "account_status", {}))
        assert result["connected"] is False
        assert "No Notion account" in result["problem"]

    def test_every_request_carries_the_pinned_version(self, agents, ws):
        ex, _ = executor(ws)
        ok(invoke(agents, ex, "pages_read", {"page_id": ws.plan}))
        ok(invoke(agents, ex, "databases_query", {"database_id": ws.tasks}))
        assert ws.stub.requests >= 4 and ws.stub.unversioned == 0


PEOPLE_SCHEMA = {"Task": "title", "Assignee": "people", "Cost": "number",
                 "Stage": ("select", ["Open", "Closed"])}


class TestDataSources:
    def test_a_database_of_several_data_sources_asks_which(self, agents, ws):
        stub = ws.stub
        stub.add_source(ws.tasks, "Archive 2025", {"Name": "title"})
        ex, _ = executor(ws)
        schema = ok(invoke(agents, ex, "databases_get", {"database_id": ws.tasks}))
        assert [s["name"] for s in schema["data_sources"]] == ["Office move tasks", "Archive 2025"]
        result, status = invoke(agents, ex, "databases_query", {"database_id": ws.tasks})
        assert status == "error" and result["kind"] == "invalid" and "2 data sources" in result["error"]
        # Each is read by its own id, which a search gives.
        archive = schema["data_sources"][1]["data_source_id"]
        assert ok(invoke(agents, ex, "databases_query", {"database_id": archive}))["rows"] == []
        found = ok(invoke(agents, ex, "search_find", {"kind": "database"}))
        assert {r["id"] for r in found["results"]} == {stub.source_of(ws.tasks), archive}
        assert {r["database_id"] for r in found["results"]} == {ws.tasks}

    def test_a_row_is_written_under_a_data_source_it_was_given(self, agents, ws):
        ex, _ = executor(ws)
        source = ws.stub.source_of(ws.tasks)
        made = ok(invoke(agents, ex, "pages_create", {"database_id": source, "title": "Recycle boxes"},
                         chat_level=2))
        assert ws.stub.created[0]["parent"]["data_source_id"] == source
        assert made["parent"] == f"database {ws.tasks}"


class TestExactValues:
    def test_numbers_travel_with_their_own_digits(self, agents, ws):
        ex, _ = executor(ws)
        ok(invoke(agents, ex, "pages_update", {"page_id": ws.desks, "properties": {"Budget": "12.50"}},
                  chat_level=2))
        ok(invoke(agents, ex, "pages_update", {"page_id": ws.rack, "properties": {"Budget": 0.1}},
                  chat_level=2))
        sent = [raw for raw in ws.stub.raw if b'"Budget"' in raw]
        assert b'"number": 12.50' in sent[0] and b'"number": 0.1}' in sent[1]
        refused, status = invoke(agents, ex, "pages_update", {
            "page_id": ws.desks, "properties": {"Budget": "1,250"}}, chat_level=2)
        assert status == "error" and refused["kind"] == "invalid" and "1250" in refused["error"]
        rows = ok(invoke(agents, ex, "databases_query", {
            "database_id": ws.tasks, "filter_property": "Budget", "filter_operator": "less_than",
            "filter_value": "12.6"}))
        assert sorted(r["title"] for r in rows["rows"]) == ["Move network rack", "Order standing desks"]
        assert b'"less_than": 12.6' in ws.stub.raw[-1]

    def test_arabic_text_survives_the_way_there_and_back(self, agents, ws):
        ex, _ = executor(ws)
        title = "طلب مكاتب جديدة \U0001FA91"
        made = ok(invoke(agents, ex, "pages_create", {
            "database_id": ws.tasks, "title": title, "properties": {"Owner": "ليلى"},
            "paragraphs": ["التسليم يوم الخميس."]}, chat_level=2))
        assert title.encode("utf-8") in ws.stub.raw[-1]
        row = ok(invoke(agents, ex, "pages_read", {"page_id": made["page_id"]}))
        assert (row["title"], row["properties"]["Owner"]) == (title, "ليلى")
        assert row["blocks"][0]["text"] == "التسليم يوم الخميس."
        found = ok(invoke(agents, ex, "databases_query", {
            "database_id": ws.tasks, "filter_property": "Owner", "filter_operator": "equals",
            "filter_value": "ليلى"}))
        assert [r["title"] for r in found["rows"]] == [title]

    def test_a_long_text_is_sent_in_pieces_notion_takes(self, agents, ws):
        ex, _ = executor(ws)
        # 1,990 characters, but 2,980 UTF-16 units as Notion counts them: two pieces, no emoji cut in half.
        text = "🪑" * 990 + "م" * 1000
        ok(invoke(agents, ex, "blocks_append", {"page_id": ws.plan, "items": [{"text": text}]}, chat_level=2))
        pieces = [p["text"]["content"] for p in ws.stub.appended[0]["children"][0]["paragraph"]["rich_text"]]
        assert len(pieces) == 2 and "".join(pieces) == text
        assert all(len(p.encode("utf-16-le")) // 2 <= 2000 for p in pieces)


class TestPeopleAndFilters:
    def _crew(self, ws):
        crew = ws.stub.add_database("Fit-out crew", PEOPLE_SCHEMA)
        ws.stub.add_row(crew, {"Task": "Paint", "Assignee": ["user-dana"], "Cost": 300, "Stage": "Open"})
        ws.stub.add_row(crew, {"Task": "Wiring", "Assignee": ["user-omar"], "Cost": 900, "Stage": "Open"})
        ws.stub.add_row(crew, {"Task": "Signs", "Assignee": ["user-layla"], "Cost": 150, "Stage": "Closed"})
        return crew

    def test_people_are_assigned_by_name_or_address(self, agents, ws):
        crew = self._crew(ws)
        ex, _ = executor(ws)
        made = ok(invoke(agents, ex, "pages_create", {
            "database_id": crew, "title": "Carpets",
            "properties": {"Assignee": ["omar@sidra.example", "ليلى حداد"]}}, chat_level=2))
        assert ws.stub.created[0]["properties"]["Assignee"] == {"people": [{"id": "user-omar"},
                                                                            {"id": "user-layla"}]}
        row = ok(invoke(agents, ex, "pages_read", {"page_id": made["page_id"]}))
        assert row["properties"]["Assignee"] == "Omar Nasser, ليلى حداد"
        refused, status = invoke(agents, ex, "pages_update", {
            "page_id": made["page_id"], "properties": {"Assignee": "Zaid"}}, chat_level=2)
        assert status == "error" and refused["kind"] == "invalid" and "Dana Harbour" in refused["error"]
        assert len(ws.stub.updated) == 0

    def test_a_compound_filter_is_built_from_the_schema(self, agents, ws):
        crew = self._crew(ws)
        ex, _ = executor(ws)
        found = ok(invoke(agents, ex, "databases_query", {"database_id": crew, "filter": {
            "match": "any", "conditions": [
                {"property": "Assignee", "operator": "contains", "value": "dana@sidra.example"},
                {"match": "all", "conditions": [
                    {"property": "Stage", "operator": "equals", "value": "closed"},
                    {"property": "Cost", "operator": "less_than", "value": 200}]}]}}))
        assert sorted(r["title"] for r in found["rows"]) == ["Paint", "Signs"]
        assert ws.stub.queries[-1]["filter"] == {"or": [
            {"property": "Assignee", "people": {"contains": "user-dana"}},
            {"and": [{"property": "Stage", "select": {"equals": "Closed"}},
                     {"property": "Cost", "number": {"less_than": 200}}]}]}

    def test_a_filter_on_a_property_that_is_not_there_sends_nothing(self, agents, ws):
        crew = self._crew(ws)
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "databases_query", {"database_id": crew, "filter": {
            "conditions": [{"property": "Priority", "operator": "equals", "value": "High"}]}})
        assert status == "error" and result["kind"] == "invalid" and "Task, Assignee" in result["error"]
        assert ws.stub.queries == []


class TestTrash:
    def test_a_page_is_trashed_and_restored_and_recorded(self, agents, ws):
        ex, provider = executor(ws)
        gone = ok(invoke(agents, ex, "pages_archive", {"page_id": ws.movers}, chat_level=2))
        assert gone["in_trash"] is True and gone["already"] is False
        assert ws.stub.updated[-1] == {"page_id": ws.movers, "in_trash": True}
        rows = ok(invoke(agents, ex, "databases_query", {"database_id": ws.tasks}))
        assert "Book movers" not in [r["title"] for r in rows["rows"]]
        refused, status = invoke(agents, ex, "pages_update", {
            "page_id": ws.movers, "properties": {"Status": "Done"}}, chat_level=2)
        assert status == "error" and "trash" in refused["error"]
        again = ok(invoke(agents, ex, "pages_archive", {"page_id": ws.movers}, chat_level=2))
        assert again["already"] is True and "write_ref" not in again
        back = ok(invoke(agents, ex, "pages_archive", {"page_id": ws.movers, "restore": True}, chat_level=2))
        assert back["in_trash"] is False
        actions = [r["keys"]["action"] for r in provider.data["write"].values()]
        assert actions == ["page_archived", "page_restored"]

    def test_trashing_needs_the_chats_trust(self, agents, ws):
        ex, _ = executor(ws)
        refused, status = invoke(agents, ex, "pages_archive", {"page_id": ws.movers}, chat_level=1)
        assert status == "error" and refused.get("denied") is True and ws.stub.updated == []


class TestWatchesStandOnTheirOwn:
    def _second(self, ws):
        crew = ws.stub.add_database("Fit-out crew", PEOPLE_SCHEMA)
        ws.stub.add_row(crew, {"Task": "Paint"}, edited="2026-09-10T08:45:00.000Z")
        return crew

    def test_one_failing_watch_loses_nothing_of_another(self, agents, ws):
        stub = ws.stub
        crew = self._second(ws)
        ex, provider = executor(ws)
        tasks = ok(invoke(agents, ex, "watch_database", {"database_id": ws.tasks}))
        fitout = ok(invoke(agents, ex, "watch_database", {"database_id": crew}))
        stub.clock = "2026-09-10T09:30:00.000Z"
        lamps = stub.add_row(ws.tasks, {"Name": "Order desk lamps"})
        stub.add_row(crew, {"Task": "Carpets"})
        stub.restricted.add(stub.source_of(crew))
        news = ok(invoke(agents, ex, "watch_changes", {}))
        assert [r["page_id"] for r in news["rows"]] == [lamps]
        [problem] = news["problems"]
        assert (problem["watch_ref"], problem["kind"]) == (fitout["watch_ref"], "denied")
        kept = provider.data["watch"]
        assert kept[tasks["watch_ref"]]["keys"]["since"] == "2026-09-10T09:30:00.000Z"
        assert kept[fitout["watch_ref"]]["keys"]["since"] == "2026-09-10T08:45:00.000Z"
        # Shared again: what it missed comes now, once.
        stub.restricted.clear()
        later = ok(invoke(agents, ex, "watch_changes", {}))
        assert [r["database"] for r in later["rows"]] == ["Fit-out crew"] and later["problems"] == []

    def test_a_watch_whose_database_is_gone_closes_itself(self, agents, ws):
        stub = ws.stub
        crew = self._second(ws)
        ex, provider = executor(ws)
        fitout = ok(invoke(agents, ex, "watch_database", {"database_id": crew}))
        del stub.sources[stub.source_of(crew)]
        news = ok(invoke(agents, ex, "watch_changes", {}))
        assert [c["watch_ref"] for c in news["closed"]] == [fitout["watch_ref"]] and news["problems"] == []
        assert provider.data["watch"][fitout["watch_ref"]]["keys"]["status"] == "closed"
        assert ok(invoke(agents, ex, "watch_changes", {}))["checked"] == 0

    def test_watches_are_listed_and_closed(self, agents, ws):
        ex, _ = executor(ws)
        watch = ok(invoke(agents, ex, "watch_database", {"database_id": ws.tasks, "note": "moves"}))
        [listed] = ok(invoke(agents, ex, "watches_list", {}))["watches"]
        assert (listed["watch_ref"], listed["status"], listed["database"], listed["note"]) == (
            watch["watch_ref"], "active", "Office move tasks", "moves")
        closed = ok(invoke(agents, ex, "watches_close", {"watch_ref": watch["watch_ref"]}))
        assert closed == {"watch_ref": watch["watch_ref"], "closed": True, "already": False}
        assert ok(invoke(agents, ex, "watches_list", {}))["watches"] == []
        assert [w["status"] for w in ok(invoke(agents, ex, "watches_list", {"include_closed": True}))[
            "watches"]] == ["closed"]
        assert ok(invoke(agents, ex, "watch_changes", {}))["checked"] == 0

    def test_a_busy_minute_never_overflows_a_field(self, agents, ws):
        """Hundreds of rows on one minute: the ids are kept in values (an object), never in a keys string."""
        stub = ws.stub
        ex, provider = executor(ws)
        watch = ok(invoke(agents, ex, "watch_database", {"database_id": ws.tasks}))
        stub.clock = "2026-09-10T09:40:00.000Z"
        made = {stub.add_row(ws.tasks, {"Name": f"Box {n}"}) for n in range(450)}
        seen = set()
        for _ in range(30):
            news = ok(invoke(agents, ex, "watch_changes", {"max_results": 25}))
            seen |= {r["page_id"] for r in news["rows"]}
            if not news["more"]:
                break
        assert seen == made
        kept = provider.data["watch"][watch["watch_ref"]]
        assert len(kept["values"]["seen_rows"]["ids"]) == 450 and "seen" not in kept["keys"]
        assert all(len(str(v)) < 200 for v in kept["keys"].values())

    def test_a_watch_kept_by_1_0_goes_on_from_where_it_was(self, agents, ws):
        """status watching, the database's id and a seen keys string, as 1.0.0 wrote them."""
        stub = ws.stub
        ex, provider = executor(ws)
        ref = provider.create_data("watch", {
            "database_id": ws.tasks, "since": "2026-09-10T08:30:00.000Z", "status": "watching",
            "seen": json.dumps({ws.rack: 1, ws.movers: 1})}, {"database": "Office move tasks"})
        stub.clock = "2026-09-10T09:50:00.000Z"
        lamps = stub.add_row(ws.tasks, {"Name": "Order desk lamps"})
        news = ok(invoke(agents, ex, "watch_changes", {}))
        assert [r["page_id"] for r in news["rows"]] == [lamps]
        kept = provider.data["watch"][ref]
        assert kept["keys"]["data_source_id"] == stub.source_of(ws.tasks) and kept["keys"]["seen"] == ""
        assert [w["status"] for w in ok(invoke(agents, ex, "watches_list", {}))["watches"]] == ["active"]
