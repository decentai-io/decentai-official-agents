"""The Dropbox agent, its program started and spoken to in the standard's
messages, against a loopback Dropbox holding Sidra Office Supplies'
files: a lease under Contracts, a budget workbook under Finance, a
proposal draft at the top level, and a showroom floor plan Dana shared
without it ever being added to the account's Dropbox.
"""

import pytest


from tests.harness import Chat, Records, probe, run
from tests.conftest import resource
from tests.dropbox_stub import DropboxStub

DANA = "dana@sidra.example"
DANA_ID = "dbid:AADdana"
LEASE = b"%PDF-1.4\n% Riverside office lease\n"
BUDGET = b"PK\x03\x04 q3 budget workbook"
PROPOSAL = b"PK\x03\x04 proposal draft"
FLOOR_PLAN = b"%PDF-1.4\n% showroom floor plan\n"
NOTICE = b"%PDF-1.4\n% renewal notice\n"
INVOICE = b"%PDF-1.4\n% invoice INV-2209\n"


@pytest.fixture
def dropbox():
    stub = DropboxStub().start()
    stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
    stub.add_file("/Finance", "Q3 budget.xlsx", BUDGET)
    stub.add_file("/", "Proposal draft.docx", PROPOSAL)
    stub.share_with_me("Showroom floor plan.pdf", FLOOR_PLAN, "Dana Haddad")
    yield stub
    stub.stop()


def executor(dropbox, access_token="at-1"):
    provider = Records(
        secrets={"dropbox": dropbox.secret(access_token)})
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=0):
    return run(ex.invoke(agents["dropbox"], name, inputs, chat_level=chat_level))


def find_one(agents, ex, query):
    result, status = invoke(agents, ex, "files_search", {"query": query})
    assert status == "success", result
    assert len(result["items"]) == 1, result
    return result["items"][0]


def attach(ex, filename, raw):
    """A file a person attached to the chat."""
    return ex.file(filename, raw)


def downloaded(ex, result):
    return ex.files[result["file_ref"]]["content"]


def levels(agents, agent_id):
    return {f["id"]: f["level"] for f in agents[agent_id].manifest["functions"]}


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["dropbox"]
        assert probe(agent) == []

    def test_it_changes_the_drive_where_onedrive_does_and_watches_at_level_one(self, agents):
        """The twins keep the same contract; the change watch is extra,
        and runs unattended, so it is a read and a record write only."""
        mine = levels(agents, "dropbox")
        watch = {"changes_watch": 1, "changes_new": 1}
        onedrive = levels(agents, "onedrive")
        onedrive.pop("files_move_many")          # OneDrive's alone, so far
        assert mine == {**onedrive, **watch}
        new = agents["dropbox"].function("changes_new")
        assert new["schedulable"] is True and "model" not in new["calls"]

    def test_the_secret_is_a_dropbox_sign_in_whose_identity_is_a_post(self, agents):
        secret = resource(agents["dropbox"].manifest, "secrets", "dropbox")
        oauth = secret["oauth"]
        assert oauth["provider"] == "dropbox"
        assert oauth["authorize_params"] == {"token_access_type": "offline"}
        assert oauth["identity"] == {
            "url": "https://api.dropboxapi.com/2/users/get_current_account",
            "method": "POST", "field": "email"}
        assert [f["name"] for f in secret["fields"]] == ["api_base_url"]

    def test_no_list_returns_more_than_twenty_five_rows(self, agents):
        for function in agents["dropbox"].manifest["functions"]:
            most = function["inputs"].get("properties", {}).get("max_results")
            if most:
                assert most["maximum"] <= 25, function["id"]

    def test_status_reports_the_account(self, agents, dropbox):
        ex, _ = executor(dropbox)
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success", result
        assert result["connected"] is True and result["email"] == DropboxStub.ACCOUNT
        assert result["team"] == "Sidra Office Supplies"
        assert result["used_bytes"] == len(LEASE) + len(BUDGET) + len(PROPOSAL)


class TestFinding:
    def test_search_finds_a_file_and_says_where_it_is(self, agents, dropbox):
        ex, _ = executor(dropbox)
        row = find_one(agents, ex, "lease")
        assert row["name"] == "Riverside office lease.pdf"
        assert row["item_id"].startswith("id:")
        assert (row["folder"], row["path"], row["kind"], row["mime_type"], row["size"]) == (
            "/Contracts", "/Contracts/Riverside office lease.pdf", "file",
            "application/pdf", len(LEASE))

    def test_a_folder_is_listed_by_path_folders_first(self, agents, dropbox):
        ex, _ = executor(dropbox)
        top, status = invoke(agents, ex, "files_list", {})
        assert status == "success", top
        assert [(i["name"], i["kind"]) for i in top["items"]] == [
            ("Contracts", "folder"), ("Finance", "folder"), ("Proposal draft.docx", "file")]
        contracts, _ = invoke(agents, ex, "files_list", {"folder_path": "/Contracts/"})
        assert [i["name"] for i in contracts["items"]] == ["Riverside office lease.pdf"]
        by_id, _ = invoke(agents, ex, "files_list",
                          {"folder_id": top["items"][0]["item_id"]})
        assert by_id["items"] == contracts["items"]

    def test_paging_follows_the_cursor(self, agents, dropbox):
        ex, _ = executor(dropbox)
        first, _ = invoke(agents, ex, "files_list", {"max_results": 2})
        assert len(first["items"]) == 2 and first["next_page_token"]
        rest, _ = invoke(agents, ex, "files_list",
                         {"page_token": first["next_page_token"]})
        assert [i["name"] for i in rest["items"]] == ["Proposal draft.docx"]
        assert "next_page_token" not in rest

    def test_a_folder_that_is_not_there_is_named(self, agents, dropbox):
        ex, _ = executor(dropbox)
        result, status = invoke(agents, ex, "files_list",
                                {"folder_path": "/Contracts/Nope"})
        assert status == "error" and result["kind"] == "not_found"
        assert "/Contracts/Nope" in result["error"]

    def test_shared_with_me_says_who_shared(self, agents, dropbox):
        ex, _ = executor(dropbox)
        result, status = invoke(agents, ex, "files_shared_with_me", {})
        assert status == "success", result
        [row] = result["items"]
        assert (row["name"], row["shared_by"], row["shared"]) == (
            "Showroom floor plan.pdf", "Dana Haddad", True)

    def test_a_non_ascii_name_travels_in_the_header_escaped(self, agents, dropbox):
        """Dropbox-API-Arg is an HTTP header: the stub refuses anything
        but ASCII, so a name with an accent proves the escaping."""
        dropbox.add_file("/Contracts", "Café lease.pdf", LEASE)
        ex, _ = executor(dropbox)
        row = find_one(agents, ex, "Café")
        result, status = invoke(agents, ex, "files_download",
                                {"item_id": row["path"]}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Café lease.pdf"
        assert downloaded(ex, result) == LEASE


class TestFetching:
    def test_a_download_is_a_platform_file_of_the_same_bytes(self, agents, dropbox):
        ex, _ = executor(dropbox)
        row = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "files_download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Riverside office lease.pdf"
        assert downloaded(ex, result) == LEASE

    def test_a_word_file_comes_in_as_pdf_on_request(self, agents, dropbox):
        ex, _ = executor(dropbox)
        row = find_one(agents, ex, "proposal")
        result, status = invoke(agents, ex, "files_download",
                                {"item_id": row["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Proposal draft.pdf"
        assert downloaded(ex, result).startswith(b"%PDF")

    def test_a_spreadsheet_is_not_offered_as_pdf(self, agents, dropbox):
        ex, _ = executor(dropbox)
        row = find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "files_download",
                                {"item_id": row["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "error" and "Word and PowerPoint" in result["error"]
        assert dropbox.downloads == []

    def test_a_file_too_large_to_store_is_refused_before_it_is_fetched(
            self, agents, dropbox):
        """The agent carries at most 25 MiB. The size is in the metadata,
        so the bytes are never asked for. The error's size and limit come
        with its words."""
        raw = b"%PDF-1.4 " + b"x" * (26 * 1024 * 1024)
        dropbox.add_file("/Finance", "Scanned archive.pdf", raw)
        ex, _ = executor(dropbox)
        row = find_one(agents, ex, "Scanned archive")
        result, status = invoke(agents, ex, "files_download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "error", result
        assert result["kind"] == "too_large" and f'"limit": {25 * 1024 * 1024}' in result["error"]
        assert f"{len(raw) // 1024:,} KB" in result["error"]
        assert dropbox.downloads == []
        assert ex.files == {}

    def test_a_folder_cannot_be_downloaded(self, agents, dropbox):
        ex, _ = executor(dropbox)
        result, status = invoke(agents, ex, "files_download",
                                {"item_id": "/Contracts"}, chat_level=2)
        assert status == "error" and "folder" in result["error"]

    def test_a_shared_file_is_fetched_by_its_id_through_its_link(self, agents, dropbox):
        ex, _ = executor(dropbox)
        shared, _ = invoke(agents, ex, "files_shared_with_me", {})
        result, status = invoke(agents, ex, "files_download", {
            "item_id": shared["items"][0]["item_id"]}, chat_level=2)
        assert status == "success", result
        assert downloaded(ex, result) == FLOOR_PLAN
        assert dropbox.downloads == ["sharing/get_shared_link_file"]

    def test_a_dropbox_document_without_bytes_is_exported(self, agents, dropbox):
        dropbox.add_file("/", "Meeting notes.paper", b"", is_downloadable=False)
        ex, _ = executor(dropbox)
        row = find_one(agents, ex, "Meeting notes")
        result, status = invoke(agents, ex, "files_download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Meeting notes.paper.md"
        assert downloaded(ex, result).startswith(b"# Meeting notes")


class TestChanging:
    def test_an_upload_never_overwrites_by_default(self, agents, dropbox):
        ex, _ = executor(dropbox)
        names = []
        for content in (NOTICE, NOTICE + b"% second copy\n"):
            ref = attach(ex, "renewal notice.pdf", content)
            result, status = invoke(agents, ex, "files_upload", {
                "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
            assert status == "success", result
            names.append(result["item"]["name"])
            assert result["item"]["folder"] == "/Contracts"
        assert names == ["renewal notice.pdf", "renewal notice (1).pdf"]
        assert dropbox.by_path("/Contracts/renewal notice.pdf")["_content"] == NOTICE

    def test_fail_refuses_a_taken_name_and_replace_is_a_new_version(self, agents, dropbox):
        ex, _ = executor(dropbox)
        lease = find_one(agents, ex, "lease")
        ref = attach(ex, "Riverside office lease.pdf", NOTICE)
        refused, status = invoke(agents, ex, "files_upload", {
            "file_ref": ref, "folder_path": "/Contracts", "on_conflict": "fail"}, chat_level=3)
        assert status == "error" and "Riverside office lease.pdf" in refused["error"]
        result, status = invoke(agents, ex, "files_upload", {
            "file_ref": ref, "folder_path": "/Contracts", "on_conflict": "replace"}, chat_level=3)
        assert status == "success", result
        assert result["item"]["item_id"] == lease["item_id"]
        assert dropbox.by_path("/Contracts/Riverside office lease.pdf")["_content"] == NOTICE

    def test_a_folder_is_made_and_a_file_moved_and_renamed_into_it(self, agents, dropbox):
        ex, _ = executor(dropbox)
        made, status = invoke(agents, ex, "files_create_folder", {
            "name": "2026", "folder_path": "/Contracts"}, chat_level=3)
        assert status == "success", made
        assert (made["item"]["kind"], made["item"]["folder"]) == ("folder", "/Contracts")
        again, status = invoke(agents, ex, "files_create_folder", {
            "name": "2026", "folder_path": "/Contracts"}, chat_level=3)
        assert status == "error" and "already" in again["error"]
        lease = find_one(agents, ex, "lease")
        moved, status = invoke(agents, ex, "files_move", {
            "item_id": lease["item_id"], "to_folder_path": "/Contracts/2026",
            "new_name": "Riverside lease 2024-2027.pdf"}, chat_level=3)
        assert status == "success", moved
        assert (moved["item"]["name"], moved["item"]["folder"]) == (
            "Riverside lease 2024-2027.pdf", "/Contracts/2026")
        assert dropbox.by_path("/Contracts/2026/Riverside lease 2024-2027.pdf") is not None
        assert dropbox.by_path("/Contracts/Riverside office lease.pdf") is None

    def test_a_move_with_nothing_to_change_is_refused(self, agents, dropbox):
        ex, _ = executor(dropbox)
        lease = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "files_move",
                                {"item_id": lease["item_id"]}, chat_level=3)
        assert status == "error" and "Nothing to change" in result["error"]

    def test_delete_goes_to_dropboxs_deleted_files(self, agents, dropbox):
        ex, _ = executor(dropbox)
        budget = find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "files_delete",
                                {"item_id": budget["item_id"]}, chat_level=3)
        assert status == "success", result
        assert result["deleted"] is True and "deleted files" in result["note"]
        assert "restored" in result["note"]
        assert dropbox.deleted_files == ["/Finance/Q3 budget.xlsx"]
        after, _ = invoke(agents, ex, "files_search", {"query": "budget"})
        assert after["items"] == []

    def test_an_upload_that_gets_no_answer_is_unknown_and_not_retried(self, agents, dropbox):
        dropbox.drop_upload = True
        ex, _ = executor(dropbox)
        ref = attach(ex, "renewal notice.pdf", NOTICE)
        result, status = invoke(agents, ex, "files_upload", {
            "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert dropbox.uploads == ["renewal notice.pdf"]

    def test_a_change_needs_the_chats_trust(self, agents, dropbox):
        ex, _ = executor(dropbox)
        budget = find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "files_delete",
                                {"item_id": budget["item_id"]}, chat_level=2)
        assert status == "error" and "approv" in result["error"].lower()
        assert dropbox.deleted_files == []


class TestSharing:
    def test_a_person_given_access_shows_in_who_has_access_until_revoked(self, agents, dropbox):
        ex, provider = executor(dropbox)
        lease = find_one(agents, ex, "lease")
        shared, status = invoke(agents, ex, "sharing_invite", {
            "item_id": lease["item_id"], "recipients": [DANA], "role": "write"}, chat_level=3)
        assert status == "success", shared
        assert shared["invited"] == [DANA]
        access, _ = invoke(agents, ex, "sharing_permissions", {"item_id": lease["item_id"]})
        dana = next(p for p in access["permissions"] if p["who"] == DANA)
        assert (dana["role"], dana["via"]) == ("write", "invite")
        revoked, status = invoke(agents, ex, "sharing_revoke", {
            "item_id": lease["item_id"], "permission_id": dana["permission_id"]}, chat_level=3)
        assert status == "success" and revoked["revoked"] is True, revoked
        access, _ = invoke(agents, ex, "sharing_permissions", {"item_id": lease["item_id"]})
        assert [p["who"] for p in access["permissions"]] == [DropboxStub.ACCOUNT]
        statuses = [r["keys"]["status"] for r in provider.data["share"].values()]
        assert statuses == ["granted", "revoked"]

    def test_a_folder_becomes_a_shared_folder_and_its_files_inherit(self, agents, dropbox):
        ex, _ = executor(dropbox)
        shared, status = invoke(agents, ex, "sharing_invite", {
            "item_id": "/Finance", "recipients": [DANA]}, chat_level=3)
        assert status == "success", shared
        budget = find_one(agents, ex, "budget")
        access, _ = invoke(agents, ex, "sharing_permissions", {"item_id": budget["item_id"]})
        dana = next(p for p in access["permissions"] if p["who"] == DANA)
        assert (dana["role"], dana["via"]) == ("read", "inherited")
        result, status = invoke(agents, ex, "sharing_revoke", {
            "item_id": budget["item_id"], "permission_id": dana["permission_id"]}, chat_level=3)
        assert status == "error" and "parent folder" in result["error"]

    def test_a_link_is_for_the_team_unless_asked(self, agents, dropbox):
        ex, provider = executor(dropbox)
        lease = find_one(agents, ex, "lease")
        made, status = invoke(agents, ex, "sharing_link",
                              {"item_id": lease["item_id"]}, chat_level=3)
        assert status == "success", made
        assert made["reach"] == "people in Sidra Office Supplies"
        assert made["link"].startswith("https://www.dropbox.com/")
        assert dropbox.links[lease["item_id"]][0]["audience"] == "team"
        # Asked in so many words: the same link now reaches anyone.
        wider, status = invoke(agents, ex, "sharing_link", {
            "item_id": lease["item_id"], "link_scope": "anonymous"}, chat_level=3)
        assert status == "success", wider
        assert wider["link"] == made["link"] and wider["reach"] == "anyone with the link"
        assert dropbox.links[lease["item_id"]][0]["audience"] == "public"
        access, _ = invoke(agents, ex, "sharing_permissions", {"item_id": lease["item_id"]})
        link = next(p for p in access["permissions"] if p["via"] == "link")
        revoked, status = invoke(agents, ex, "sharing_revoke", {
            "item_id": lease["item_id"], "permission_id": link["permission_id"]}, chat_level=3)
        assert status == "success", revoked
        assert dropbox.links[lease["item_id"]] == []
        kinds = [r["keys"]["kind"] for r in provider.data["share"].values()]
        assert kinds == ["link", "link", "link"]

    def test_a_personal_account_is_asked_rather_than_widened(self, agents):
        stub = DropboxStub(team=False).start()
        try:
            stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
            ex, _ = executor(stub)
            result, status = invoke(agents, ex, "sharing_link",
                                    {"item_id": "/Contracts/Riverside office lease.pdf"},
                                    chat_level=3)
            assert status == "error" and "anyone with the link" in result["error"]
            assert stub.links == {}
        finally:
            stub.stop()

    def test_the_owner_cannot_be_removed(self, agents, dropbox):
        ex, _ = executor(dropbox)
        lease = find_one(agents, ex, "lease")
        access, _ = invoke(agents, ex, "sharing_permissions", {"item_id": lease["item_id"]})
        owner = next(p for p in access["permissions"] if p["via"] == "owner")
        result, status = invoke(agents, ex, "sharing_revoke", {
            "item_id": lease["item_id"], "permission_id": owner["permission_id"]}, chat_level=3)
        assert status == "error" and "owner" in result["error"]

    def test_a_name_is_not_an_address(self, agents, dropbox):
        ex, _ = executor(dropbox)
        lease = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "sharing_invite", {
            "item_id": lease["item_id"], "recipients": ["Dana"]}, chat_level=3)
        assert status == "error" and "email" in result["error"]


class TestWatchingChanges:
    def test_a_watch_hands_on_an_upload_and_a_delete_exactly_once(self, agents, dropbox):
        ex, provider = executor(dropbox)
        watch, status = invoke(agents, ex, "changes_watch",
                               {"folder_path": "/Finance", "note": "file invoices"}, chat_level=1)
        assert status == "success", watch
        assert watch["folder"] == "/Finance"

        quiet, status = invoke(agents, ex, "changes_new", {}, chat_level=1)
        assert status == "success", quiet
        assert quiet == {"checked": 1, "changes": [], "reset": [], "more": False}

        dropbox.add_file("/Finance", "Invoice INV-2209.pdf", INVOICE)
        dropbox.delete("/Finance/Q3 budget.xlsx")
        dropbox.add_file("/Contracts", "Elsewhere.pdf", NOTICE)   # not watched

        news, _ = invoke(agents, ex, "changes_new", {}, chat_level=1)
        assert [(c["change"], c["path"]) for c in news["changes"]] == [
            ("added_or_changed", "/Finance/Invoice INV-2209.pdf"),
            ("deleted", "/Finance/Q3 budget.xlsx")]
        added = news["changes"][0]
        assert added["size"] == len(INVOICE) and added["item_id"].startswith("id:")
        assert added["watch_ref"] == watch["watch_ref"] and news["more"] is False

        again, _ = invoke(agents, ex, "changes_new", {}, chat_level=1)
        assert again["changes"] == []
        [record] = provider.data["watch"].values()
        assert record["keys"]["status"] == "watching"

    def test_who_changed_a_file_in_a_shared_folder_is_named(self, agents, dropbox):
        dropbox.share_folder_with("/Finance", DANA)
        ex, _ = executor(dropbox)
        invoke(agents, ex, "changes_watch", {}, chat_level=1)
        dropbox.add_file("/Finance", "Invoice INV-2209.pdf", INVOICE, by=DANA_ID)
        news, _ = invoke(agents, ex, "changes_new", {}, chat_level=1)
        [change] = news["changes"]
        assert change["modified_by"] == DANA

    def test_a_reset_cursor_starts_again_from_now_and_says_so(self, agents, dropbox):
        ex, _ = executor(dropbox)
        watch, _ = invoke(agents, ex, "changes_watch",
                          {"folder_path": "/Finance"}, chat_level=1)
        dropbox.invalidate_cursors()
        result, status = invoke(agents, ex, "changes_new", {}, chat_level=1)
        assert status == "success", result
        assert result["changes"] == []
        assert [r["watch_ref"] for r in result["reset"]] == [watch["watch_ref"]]
        assert "not seen" in result["reset"][0]["note"]
        # The new cursor works: the next change is caught once.
        dropbox.add_file("/Finance", "Invoice INV-2209.pdf", INVOICE)
        news, _ = invoke(agents, ex, "changes_new", {}, chat_level=1)
        assert [c["name"] for c in news["changes"]] == ["Invoice INV-2209.pdf"]
        assert news["reset"] == []

    def test_many_changes_come_a_page_at_a_time(self, agents, dropbox):
        ex, _ = executor(dropbox)
        invoke(agents, ex, "changes_watch",
                          {"folder_path": "/Finance"}, chat_level=1)
        for n in range(30):
            dropbox.add_file("/Finance", f"Receipt {n:02d}.pdf", INVOICE)
        first, _ = invoke(agents, ex, "changes_new", {}, chat_level=1)
        assert len(first["changes"]) == 25 and first["more"] is True
        rest, _ = invoke(agents, ex, "changes_new", {}, chat_level=1)
        assert [c["name"] for c in rest["changes"]] == [f"Receipt {n:02d}.pdf" for n in range(25, 30)]
        assert rest["more"] is False

    def test_a_watch_on_something_that_is_not_there_records_nothing(self, agents, dropbox):
        ex, provider = executor(dropbox)
        result, status = invoke(agents, ex, "changes_watch",
                                {"folder_path": "/Nope"}, chat_level=1)
        assert status == "error" and result["kind"] == "not_found"
        assert provider.data == {}


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, dropbox):
        ex, provider = executor(dropbox, access_token="expired")
        result, status = invoke(agents, ex, "files_search", {"query": "lease"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "changes_watch", {}, chat_level=1)
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "sharing_invite", {
            "item_id": "/Contracts", "recipients": [DANA]}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = Chat(Records())
        result, status = invoke(agents, ex, "account_status", {})
        assert status == "success" and result["connected"] is False
        assert "No Dropbox account" in result["problem"]

    def test_dropbox_asking_to_slow_down_is_its_own_kind(self, agents, dropbox):
        dropbox.rate_limited = True
        ex, _ = executor(dropbox)
        result, status = invoke(agents, ex, "files_search", {"query": "lease"})
        assert status == "error" and result["kind"] == "rate_limited"
        assert "30 seconds" in result["error"]
