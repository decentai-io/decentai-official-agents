"""The Google Drive agent (2.0.0: Drive with Docs, Sheets and Forms) as its own program, against a loopback Google
that is as strict as the real one where the agent depends on it (tests/gdrive_stub.py). Sidra Office Supplies keeps a
lease and a budget in My Drive, a price list in its "Sidra Sales" shared drive, a proposal as a Google Doc with two
tabs, an Orders spreadsheet in a decimal-comma locale, and a feedback form.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
import yaml

from tests.conftest import resource
from tests.gdrive_stub import FOLDER, ROOT, GoogleStub
from tests.harness import Chat, ChatSinks, Records, probe, run

DANA = "dana@sidra.example"
LEASE = b"%PDF-1.4\n% Riverside office lease\n"
PRICES = b"%PDF-1.4\n% price list 2026\n"
AGENT = "google_drive"


@pytest.fixture
def google():
    stub = GoogleStub().start()
    stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
    stub.add_file("/Finance", "Q3 budget.xlsx", b"PK\x03\x04 budget",
                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    sales = stub.add_drive("Sidra Sales")
    stub.sales = sales
    stub.prices = stub.add_file("/Price lists", "Price list 2026.pdf", PRICES, root=sales)
    stub.share_with_me("Showroom floor plan.pdf", b"%PDF floor plan", DANA)
    stub.doc = stub.add_document("/", "Q3 proposal", [
        {"title": "Proposal", "body": [("TITLE", "Q3 proposal"), ("NORMAL_TEXT", "Delivery within 15 working days."),
                                       ("NORMAL_TEXT", "Prices in AED.", {"footnote": "fn1"}),
                                       {"table": [[["Item"], ["Qty"]]]}, ("NORMAL_TEXT", "Signed by Dana.")],
         "headers": {"h.1": [("NORMAL_TEXT", "Sidra Office Supplies")]},
         "footers": {"f.1": [("NORMAL_TEXT", "Confidential")]},
         "footnotes": {"fn1": [("NORMAL_TEXT", "Prices exclude VAT.")]}},
        {"title": "ملاحظات", "id": "t.ar", "body": [("NORMAL_TEXT", "ملاحظات التسليم"),
                                                    ("NORMAL_TEXT", "Second tab line.")]}])
    stub.sheet = stub.add_spreadsheet("/Finance", "Orders 2026", {"Orders": [
        ["Customer", "Item", "Qty", "Total"],
        ["Riverside Clinic", "Chairs", 12, 1840.0],
        ["Gulf Dental", "Desks", 3, 12.5],
        ["Riverside Clinic", "Lamps", 4, 220.25]]}, locale="de_DE")
    yield stub
    stub.stop()


def chat(stub, *tokens, approve=True, asked=None):
    secrets = [stub.secret(t) for t in tokens or ("at-1",)]
    provider = Records(secrets={"google": secrets if len(secrets) > 1 else secrets[0]})
    sinks = ChatSinks(approve=(lambda request: approve), ask=asked)
    return Chat(provider, sinks), provider


def call(agents, ex, name, inputs=None, level=3):
    return run(ex.invoke(agents[AGENT], name, inputs or {}, chat_level=level))


def ok(agents, ex, name, inputs=None, level=3):
    result, status = call(agents, ex, name, inputs, level)
    assert status == "success", result
    return result


def direct(stub, path):
    """The stub reached straight, as Google's own host: (url, headers)."""
    port = stub._server.server_address[1]
    return f"http://127.0.0.1:{port}{path}", {"Authorization": "Bearer at-1", "Host": f"www.googleapis.com:{port}"}


def manifest():
    return yaml.safe_load((Path(__file__).resolve().parents[1] / "agents" / AGENT / "manifest.yaml")
                          .read_text(encoding="utf-8"))


# -- the package -------------------------------------------------------------------------------------------------------

class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        assert probe(agents[AGENT]) == []

    def test_one_agent_of_fifty_functions_with_honest_levels(self):
        doc = manifest()
        functions = {f["id"]: f for f in doc["functions"]}
        assert doc["agent"]["version"] == "2.0.1" and 40 <= len(functions) <= 55
        assert len(doc["agent"]["instructions"]) <= 4000 and len(doc["agent"]["examples"]) <= 6
        for f in functions.values():
            if f.get("schedulable"):
                assert f["level"] <= 1 and "model" not in f["calls"], f["id"]
            if "record.create:google" in f.get("calls", []):
                assert f["level"] >= 1, f["id"]
        assert {f["id"] for f in functions.values() if f.get("schedulable")} == {
            "changes_new", "comments_new", "rows_new", "responses_new"}
        assert functions["files_download"]["level"] == 0
        assert {functions[i]["level"] for i in ("files_upload", "files_trash", "sheets_append", "docs_insert",
                                                "edits_apply")} == {2}
        assert {functions[i]["level"] for i in ("sharing_invite", "sharing_link", "comments_add")} == {3}
        assert {"watches_list", "watches_close"} <= set(functions)

    def test_the_google_account_is_the_shared_block_and_many_may_be_connected(self):
        google = resource(manifest(), "secrets", "google")
        assert google["binding"] == {"cardinality": "many", "required": True}
        assert "https://www.googleapis.com/auth/forms.responses.readonly" in google["oauth"]["scopes"]

    def test_document_text_lives_in_values_never_in_keys(self):
        edit = {f["name"]: f["storage"] for f in resource(manifest(), "data", "edit")["fields"]}
        assert edit["old_text"] == edit["new_text"] == edit["title"] == "values"
        share = {f["name"]: f["storage"] for f in resource(manifest(), "data", "share")["fields"]}
        assert share["who"] == "values"


# -- accounts ----------------------------------------------------------------------------------------------------------

class TestAccounts:
    def test_status_names_the_organization_from_googles_hosted_domain(self, agents, google):
        ex, _ = chat(google)
        result = ok(agents, ex, "account_status", level=0)
        assert result["connected"] and result["account"] == GoogleStub.ACCOUNT
        assert result["organization"] == "sidra.example" and result["personal"] is False
        assert result["used_bytes"] > 0

    def test_several_accounts_are_chosen_by_address(self, agents, google):
        google.add_account("ops@sidra.example", "at-2", hd="sidra.example")
        ex, _ = chat(google, "at-1", "at-2")
        result, status = call(agents, ex, "files_search", {"query": "lease"})
        assert status == "error" and "Several Google accounts" in result["error"]
        ok(agents, ex, "files_search", {"query": "lease", "account": "ops"})
        assert google.log[-1]["token"] == "at-2"

    def test_with_none_connected_a_read_says_how_to_connect(self, agents, google):
        ex = Chat(Records())
        result, status = call(agents, ex, "files_search", {"query": "x"})
        assert status == "error" and "account_connect" in result["error"]


# -- finding, in every drive -------------------------------------------------------------------------------------------

class TestFinding:
    def test_search_reaches_my_drive_and_the_shared_drive(self, agents, google):
        ex, _ = chat(google)
        result = ok(agents, ex, "files_search", {"query": "pdf"})
        names = {i["name"]: i for i in ok(agents, ex, "files_search", {"kind": "pdf"})["items"]}
        assert {"Riverside office lease.pdf", "Price list 2026.pdf"} <= set(names)
        assert names["Price list 2026.pdf"]["drive_id"] == google.sales
        assert names["Price list 2026.pdf"]["folder"] == "Price lists"
        searched = google.requests("GET", "/drive/v3/files$")[-1]["query"]
        assert searched["corpora"] == "allDrives" and searched["supportsAllDrives"] == "true"
        assert isinstance(result["items"], list)

    def test_a_full_text_search_sends_no_order_google_would_refuse(self, agents, google):
        ex, _ = chat(google)
        found = ok(agents, ex, "files_search", {"query": "working days"})["items"]
        assert [i["name"] for i in found] == ["Q3 proposal"]

    def test_a_shared_drive_folder_is_listed_in_its_drive(self, agents, google):
        ex, _ = chat(google)
        top = ok(agents, ex, "files_list", {"folder_id": google.sales})
        assert [i["name"] for i in top["items"]] == ["Price lists"]
        assert google.requests("GET", "/drive/v3/files$")[-1]["query"]["corpora"] == "drive"

    def test_listing_pages_and_names_the_folder_once(self, agents, google):
        ex, _ = chat(google)
        first = ok(agents, ex, "files_list", {"max_results": 2})
        assert len(first["items"]) == 2 and first["next_page_token"]
        rest = ok(agents, ex, "files_list", {"max_results": 2, "page_token": first["next_page_token"]})
        assert [i["kind"] for i in first["items"]] == ["folder", "folder"] and rest["items"]
        assert google.requests("GET", r"/drive/v3/files/.+") and all(
            r["query"].get("fields") != "name" for r in google.requests("GET", r"/drive/v3/files/.+"))

    def test_the_stub_hides_a_shared_drive_file_without_supports_all_drives(self, google):
        base, headers = direct(google, "/drive/v3/files/" + google.prices)
        assert requests.get(base, headers=headers).status_code == 404
        assert requests.get(base, params={"supportsAllDrives": "true"}, headers=headers).status_code == 200

    def test_shared_with_me_and_shared_drives(self, agents, google):
        ex, _ = chat(google)
        shared = ok(agents, ex, "files_shared_with_me")["items"]
        assert shared[0]["shared_by"] == DANA
        assert ok(agents, ex, "files_drives")["drives"] == [{"drive_id": google.sales, "name": "Sidra Sales"}]


# -- bringing files in -------------------------------------------------------------------------------------------------

class TestDownload:
    def test_a_shared_drive_file_comes_in_as_it_is(self, agents, google):
        ex, _ = chat(google)
        result = ok(agents, ex, "files_download", {"item_id": google.prices}, level=0)
        assert ex.files[result["file_ref"]]["content"] == PRICES

    def test_a_google_doc_is_exported_to_the_format_asked(self, agents, google):
        ex, _ = chat(google)
        result = ok(agents, ex, "files_download", {"item_id": google.doc, "format": "pdf"}, level=0)
        assert result["filename"] == "Q3 proposal.pdf"
        assert ex.files[result["file_ref"]]["content"].startswith(b"%PDF")
        refused, status = call(agents, ex, "files_download", {"item_id": google.doc, "format": "xlsx"}, level=0)
        assert status == "error" and "docx, pdf, txt" in refused["error"]

    def test_a_tab_comes_in_as_csv_with_a_bom_and_arabic_intact(self, agents, google):
        google.append_row(google.sheet, "Orders", ["عيادة النخيل", "كراسي", 2, 300.0])
        ex, _ = chat(google)
        result = ok(agents, ex, "files_download", {"item_id": google.sheet, "format": "csv", "tab": "Orders"}, level=0)
        raw = ex.files[result["file_ref"]]["content"]
        assert raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        assert "عيادة النخيل,كراسي,2" in text and text.splitlines()[0] == "Customer,Item,Qty,Total"

    def test_a_file_over_the_chat_limit_is_refused_before_download(self, agents, google):
        big = google.add_file("/Media", "Showroom video.mp4", b"x", "video/mp4")
        google.files[big]["size"] = str(40 * 1024 * 1024)
        ex, _ = chat(google)
        result, status = call(agents, ex, "files_download", {"item_id": big}, level=0)
        assert status == "error" and result["kind"] == "too_large"
        assert "40.0 MB" in result["error"] and f"docs.google.com/file/d/{big}" in result["error"]
        assert not [r for r in google.requests("GET", big) if r["query"].get("alt") == "media"]

    def test_an_export_google_will_not_make_is_refused_with_its_link(self, agents, google):
        google.files[google.doc]["_export_size"] = 11 * 1024 * 1024
        ex, _ = chat(google)
        result, status = call(agents, ex, "files_download", {"item_id": google.doc}, level=0)
        assert status == "error" and result["kind"] == "too_large" and "docs.google.com" in result["error"]


# -- filing ------------------------------------------------------------------------------------------------------------

class TestFiling:
    def test_a_small_file_goes_up_in_one_multipart_request_under_its_arabic_name(self, agents, google):
        ex, _ = chat(google)
        ref = ex.file("عقد الإيجار.pdf", LEASE)
        result = ok(agents, ex, "files_upload", {"file_ref": ref, "folder_path": "/Contracts"})
        assert result["item"]["name"] == "عقد الإيجار.pdf" and result["item"]["folder"] == "Contracts"
        assert google.uploads == ["multipart"]
        assert google.by_path("/Contracts/عقد الإيجار.pdf")["_content"] == LEASE

    def test_a_file_over_5_mb_goes_up_in_a_resumable_session(self, agents, google):
        ex, _ = chat(google)
        content = b"%PDF-1.4\n" + b"0" * (6 * 1024 * 1024)
        result = ok(agents, ex, "files_upload", {"file_ref": ex.file("scan.pdf", content), "folder_id": google.sales})
        assert google.uploads == ["resumable"]
        assert google.files[result["item"]["item_id"]]["_content"] == content
        assert result["item"]["drive_id"] == google.sales

    def test_the_stub_refuses_a_multipart_upload_over_5_mb(self, google):
        url, headers = direct(google, "/upload/drive/v3/files")
        body = b"--b\r\nContent-Type: application/json\r\n\r\n{\"name\": \"x\"}\r\n--b\r\n\r\n" + b"0" * (6 << 20)
        answer = requests.post(url, params={"uploadType": "multipart"}, data=body,
                               headers={**headers, "Content-Type": "multipart/related; boundary=b"})
        assert answer.status_code == 413

    def test_a_name_already_in_the_folder_gets_a_number(self, agents, google):
        ex, _ = chat(google)
        ref = ex.file("Riverside office lease.pdf", b"%PDF new")
        result = ok(agents, ex, "files_upload", {"file_ref": ref, "folder_path": "/Contracts"})
        assert result["item"]["name"] == "Riverside office lease 1.pdf"
        replaced = ok(agents, ex, "files_upload", {"file_ref": ref, "folder_path": "/Contracts",
                                                   "on_conflict": "replace"})
        assert replaced["replaced"] is True

    def test_folders_moves_copies_trash_restore_and_versions(self, agents, google):
        ex, _ = chat(google)
        folder = ok(agents, ex, "files_create_folder", {"name": "2026", "folder_path": "/Contracts"})["item"]
        again, status = call(agents, ex, "files_create_folder", {"name": "2026", "folder_path": "/Contracts"})
        assert status == "error" and again["kind"] == "conflict"
        lease = google.by_path("/Contracts/Riverside office lease.pdf")["id"]
        moved = ok(agents, ex, "files_move", {"item_id": lease, "to_folder_id": folder["item_id"],
                                              "new_name": "Lease 2026.pdf"})["item"]
        assert moved["name"] == "Lease 2026.pdf" and google.files[lease]["parents"] == [folder["item_id"]]
        copied = ok(agents, ex, "files_copy", {"item_id": lease, "to_folder_id": google.sales})["item"]
        assert copied["name"] == "Copy of Lease 2026.pdf" and copied["drive_id"] == google.sales
        assert ok(agents, ex, "files_trash", {"item_id": lease})["trashed"] is True
        assert ok(agents, ex, "files_restore", {"item_id": lease})["trashed"] is False
        versions = ok(agents, ex, "files_versions", {"item_id": lease}, level=0)["versions"]
        assert len(versions) == 3 and versions[0]["modified"] >= versions[-1]["modified"]

    def test_a_write_waits_for_the_persons_yes(self, agents, google):
        ex, _ = chat(google, approve=False)
        result, status = call(agents, ex, "files_trash", {"item_id": google.prices}, level=1)
        assert status == "error" and result["kind"] == "refused"
        assert not google.files[google.prices]["trashed"]


# -- sharing -----------------------------------------------------------------------------------------------------------

class TestSharing:
    def test_an_organization_link_uses_the_accounts_hosted_domain_not_its_address(self, agents, google):
        google.accounts["at-1"]["hd"] = "sidra-group.example"
        ex, provider = chat(google)
        result = ok(agents, ex, "sharing_link", {"item_id": google.prices})
        assert result["reach"] == "people at sidra-group.example"
        granted = google.perms[google.prices][-1]
        assert granted["type"] == "domain" and granted["domain"] == "sidra-group.example"
        record = next(iter(provider.data["share"].values()))
        assert "who" in record["values"] and "who" not in record["keys"]

    def test_a_personal_account_has_no_organization_and_is_told_so(self, agents, google):
        del google.accounts["at-1"]["hd"]
        ex, _ = chat(google)
        result, status = call(agents, ex, "sharing_link", {"item_id": google.prices})
        assert status == "error" and "personal Google account" in result["error"]
        anyone = ok(agents, ex, "sharing_link", {"item_id": google.prices, "scope": "anyone"})
        assert anyone["reach"] == "anyone with the link"

    def test_invite_then_permissions_then_revoke_but_never_the_owner(self, agents, google):
        ex, _ = chat(google)
        lease = google.by_path("/Contracts/Riverside office lease.pdf")["id"]
        assert ok(agents, ex, "sharing_invite", {"item_id": lease, "recipients": [DANA], "role": "comment"})[
            "invited"] == [DANA]
        refused, status = call(agents, ex, "sharing_invite", {"item_id": lease, "recipients": ["Dana"]})
        assert status == "error" and "email addresses" in refused["error"]
        rows = ok(agents, ex, "sharing_permissions", {"item_id": lease}, level=0)["permissions"]
        dana = next(r for r in rows if r["who"] == DANA)
        owner = next(r for r in rows if r["via"] == "owner")
        assert dana["role"] == "comment"
        assert call(agents, ex, "sharing_revoke", {"item_id": lease, "permission_id": owner["permission_id"]})[1] == \
            "error"
        assert ok(agents, ex, "sharing_revoke", {"item_id": lease, "permission_id": dana["permission_id"]})["revoked"]

    def test_inherited_shared_drive_access_is_not_revoked_here(self, agents, google):
        ex, _ = chat(google)
        rows = ok(agents, ex, "sharing_permissions", {"item_id": google.prices}, level=0)["permissions"]
        inherited = next(r for r in rows if r["via"] == "inherited")
        result, status = call(agents, ex, "sharing_revoke", {"item_id": google.prices,
                                                             "permission_id": inherited["permission_id"]})
        assert status == "error" and "shared drive" in result["error"]


# -- busy Google -------------------------------------------------------------------------------------------------------

class TestRateLimits:
    def test_429_with_retry_after_is_waited_out(self, agents, google):
        google.fail("GET", r"^/drive/v3/files$", 429, times=2, retry_after="0")
        ex, _ = chat(google)
        assert ok(agents, ex, "files_search", {"query": "lease"})["items"]

    def test_403_user_rate_limit_is_waited_out_even_for_a_write(self, agents, google):
        google.fail("POST", r"/permissions$", 403, reason="userRateLimitExceeded", message="User Rate Limit Exceeded")
        ex, _ = chat(google)
        assert ok(agents, ex, "sharing_link", {"item_id": google.prices})["link"]
        assert len(google.requests("POST", r"/permissions$")) == 2

    def test_a_missing_permission_says_reconnect(self, agents, google):
        google.fail("GET", r"^/drive/v3/files$", 403, reason="insufficientPermissions",
                    message="Request had insufficient authentication scopes.")
        ex, _ = chat(google)
        result, status = call(agents, ex, "files_search", {"query": "lease"})
        assert status == "error" and result["kind"] == "scope"


# -- docs --------------------------------------------------------------------------------------------------------------

class TestDocs:
    def test_every_tab_with_headers_footers_and_footnotes_is_read(self, agents, google):
        ex, _ = chat(google)
        result = ok(agents, ex, "docs_read", {"document_id": google.doc}, level=0)
        rows = result["paragraphs"]
        assert [t["title"] for t in result["tabs"]] == ["Proposal", "ملاحظات"]
        assert [(r["part"], r["text"]) for r in rows if r["part"] != "body"] == [
            ("header", "Sidra Office Supplies"), ("footer", "Confidential"), ("footnote", "Prices exclude VAT.")]
        assert rows[2]["text"] == "Prices in AED.[^1]" and rows[2]["has_objects"] is True
        assert rows[3]["in_table"] is True
        assert [r["text"] for r in rows if r["tab"] == "ملاحظات"] == ["ملاحظات التسليم", "Second tab line."]
        assert google.requests("GET", "/v1/documents/")[-1]["query"]["includeTabsContent"] == "true"

    def test_a_long_document_is_read_on_from_next_from(self, agents, google):
        ex, _ = chat(google)
        first = ok(agents, ex, "docs_read", {"document_id": google.doc, "max_paragraphs": 4}, level=0)
        rest = ok(agents, ex, "docs_read", {"document_id": google.doc, "from": first["next_from"]}, level=0)
        assert [p["index"] for p in first["paragraphs"] + rest["paragraphs"]] == list(range(1, 12))
        assert "next_from" not in rest

    def test_propose_then_apply_in_the_second_tab(self, agents, google):
        ex, provider = chat(google)
        proposed = ok(agents, ex, "edits_propose", {"document_id": google.doc, "paragraph": 11,
                                                    "new_text": "السطر الثاني."}, level=1)
        record = provider.data["edit"][proposed["edit_ref"]]
        assert "السطر الثاني." in record["values"].values() and "new_text" not in record["keys"]
        assert ok(agents, ex, "edits_apply", {"edit_ref": proposed["edit_ref"]})["applied"]
        assert google.doc_text(google.doc, 1) == ["ملاحظات التسليم", "السطر الثاني."]
        assert google.doc_text(google.doc, 0)[5] == "Signed by Dana."

    def test_a_header_paragraph_is_edited_in_its_own_segment(self, agents, google):
        ex, _ = chat(google)
        proposed = ok(agents, ex, "edits_propose", {"document_id": google.doc, "paragraph": 7,
                                                    "new_text": "Sidra Office Supplies LLC"}, level=1)
        ok(agents, ex, "edits_apply", {"edit_ref": proposed["edit_ref"]})
        assert google.docs[google.doc]["tabs"][0]["headers"]["h.1"][0]["text"] == "Sidra Office Supplies LLC"

    def test_a_document_changed_since_the_proposal_is_refused(self, agents, google):
        ex, provider = chat(google)
        proposed = ok(agents, ex, "edits_propose", {"document_id": google.doc, "paragraph": 2,
                                                    "new_text": "Delivery within 10 working days."}, level=1)
        google.edit_as(google.doc, 0, 6, "Signed by Omar.")
        result, status = call(agents, ex, "edits_apply", {"edit_ref": proposed["edit_ref"]})
        assert status == "error" and result["kind"] == "conflict"
        assert provider.data["edit"][proposed["edit_ref"]]["keys"]["status"] == "refused"

    def test_a_change_landing_while_the_edit_is_sent_is_refused_by_google(self, agents, google):
        ex, _ = chat(google)
        proposed = ok(agents, ex, "edits_propose", {"document_id": google.doc, "paragraph": 2,
                                                    "new_text": "Delivery within 10 working days."}, level=1)
        google.before_batch = lambda doc_id: google.edit_as(doc_id, 0, 6, "Signed by Omar.")
        result, status = call(agents, ex, "edits_apply", {"edit_ref": proposed["edit_ref"]})
        assert status == "error" and result["kind"] == "conflict" and "Nothing in the document" in result["error"]
        assert google.doc_text(google.doc, 0)[1] == "Delivery within 15 working days."

    def test_paragraphs_are_added_at_the_end_and_after_one_with_their_style(self, agents, google):
        ex, _ = chat(google)
        ok(agents, ex, "docs_insert", {"document_id": google.doc, "paragraphs": ["Payment: 30 days.", "شكراً"]})
        assert google.doc_text(google.doc, 0)[-2:] == ["Payment: 30 days.", "شكراً"]
        ok(agents, ex, "docs_insert", {"document_id": google.doc, "paragraphs": ["Scope"], "after_paragraph": 1,
                                       "style": "HEADING_1"})
        body = google._paragraphs(google.docs[google.doc]["tabs"][0]["body"])
        assert (body[1]["text"], body[1]["style"]) == ("Scope", "HEADING_1")
        assert body[-1]["style"] == "NORMAL_TEXT"
        ok(agents, ex, "docs_insert", {"document_id": google.doc, "paragraphs": ["Draft"], "after_paragraph": 0})
        assert google.doc_text(google.doc, 0)[0] == "Draft"

    def test_an_insert_into_a_changed_document_is_refused(self, agents, google):
        google.before_batch = lambda doc_id: google.edit_as(doc_id, 0, 6, "Signed by Omar.")
        ex, _ = chat(google)
        result, status = call(agents, ex, "docs_insert", {"document_id": google.doc, "paragraphs": ["Late"]})
        assert status == "error" and result["kind"] == "conflict"

    def test_a_new_document_with_its_paragraphs(self, agents, google):
        ex, _ = chat(google)
        made = ok(agents, ex, "docs_create", {"title": "Meeting notes", "paragraphs": ["Agenda", "Actions"],
                                              "folder_id": google.sales})
        assert google.doc_text(made["document_id"]) == ["Agenda", "Actions"]
        assert google.files[made["document_id"]]["driveId"] == google.sales


# -- comments ----------------------------------------------------------------------------------------------------------

class TestComments:
    def test_open_comments_page_without_empty_pages(self, agents, google):
        for n in range(30):
            comment = google.comment_as(google.doc, DANA, f"Comment {n}")
            if n % 5:
                google.reply_as(google.doc, comment["id"], DANA, "", action="resolve")
        ex, _ = chat(google)
        seen, token = [], ""
        while True:
            page = ok(agents, ex, "comments_list", {"file_id": google.doc, "max_results": 4,
                                                    **({"page_token": token} if token else {})}, level=0)
            assert page["comments"] or not seen
            seen += [c["text"] for c in page["comments"]]
            token = page.get("next_page_token", "")
            if not token:
                break
        assert seen == [f"Comment {n}" for n in range(0, 30, 5)]

    def test_add_reply_resolve(self, agents, google):
        ex, _ = chat(google)
        added = ok(agents, ex, "comments_add", {"file_id": google.doc, "text": "Check this.",
                                                "quote": "Delivery within 15 working days."})
        bad, status = call(agents, ex, "comments_add", {"file_id": google.doc, "text": "x", "quote": "not there"})
        assert status == "error"
        ok(agents, ex, "comments_reply", {"file_id": google.doc, "comment_id": added["comment_id"], "text": "Done."})
        assert ok(agents, ex, "comments_resolve", {"file_id": google.doc, "comment_id": added["comment_id"]})[
            "resolved"]
        assert google.comments[google.doc][0]["resolved"] is True

    def test_the_watch_hands_on_others_comments_and_a_gone_file_closes_it(self, agents, google):
        ex, provider = chat(google)
        lease = google.by_path("/Contracts/Riverside office lease.pdf")["id"]
        doc_watch = ok(agents, ex, "comments_watch", {"file_id": google.doc}, level=1)["watch_ref"]
        lease_watch = ok(agents, ex, "comments_watch", {"file_id": lease}, level=1)["watch_ref"]
        google.comment_as(google.doc, DANA, "Is 15 days right?")
        ok(agents, ex, "comments_add", {"file_id": google.doc, "text": "Mine, not news."})
        del google.files[lease]
        result = ok(agents, ex, "comments_new", {}, level=1)
        assert [r["text"] for r in result["rows"]] == ["Is 15 days right?"]
        assert [c["watch_ref"] for c in result["closed"]] == [lease_watch]
        assert provider.data["watch"][lease_watch]["keys"]["status"] == "closed"
        assert ok(agents, ex, "comments_new", {}, level=1)["rows"] == []
        assert provider.data["watch"][doc_watch]["keys"]["status"] == "active"


# -- watches: none is lost when another fails --------------------------------------------------------------------------

class TestWatches:
    def test_a_failing_watch_keeps_its_cursor_and_the_others_are_handed_on(self, agents, google):
        ex, provider = chat(google)
        lease = google.by_path("/Contracts/Riverside office lease.pdf")["id"]
        failing = ok(agents, ex, "comments_watch", {"file_id": lease}, level=1)["watch_ref"]
        ok(agents, ex, "comments_watch", {"file_id": google.doc}, level=1)
        google.comment_as(lease, DANA, "On the lease")
        google.comment_as(google.doc, DANA, "On the proposal")
        before = dict(provider.data["watch"][failing]["keys"])
        google.fail("GET", rf"/files/{lease}/comments$", 403, message="The user does not have sufficient permissions.")
        result = ok(agents, ex, "comments_new", {}, level=1)
        assert [r["text"] for r in result["rows"]] == ["On the proposal"]
        assert [p["watch_ref"] for p in result["problems"]] == [failing]
        assert provider.data["watch"][failing]["keys"] == before
        again = ok(agents, ex, "comments_new", {}, level=1)
        assert [r["text"] for r in again["rows"]] == ["On the lease"]

    def test_watches_are_listed_and_closed(self, agents, google):
        ex, _ = chat(google)
        ref = ok(agents, ex, "changes_watch", {}, level=1)["watch_ref"]
        ok(agents, ex, "rows_watch", {"spreadsheet_id": google.sheet, "tab": "Orders"}, level=1)
        listed = ok(agents, ex, "watches_list", {}, level=0)["watches"]
        assert {w["kind"] for w in listed} == {"changes", "rows"}
        assert ok(agents, ex, "watches_close", {"watch_ref": ref}, level=1)["closed"]
        assert [w["kind"] for w in ok(agents, ex, "watches_list", {}, level=0)["watches"]] == ["rows"]
        assert ok(agents, ex, "changes_new", {}, level=1)["checked"] == 0


class TestChanges:
    def test_others_changes_are_handed_on_once_and_the_accounts_own_are_not(self, agents, google):
        ex, _ = chat(google)
        ok(agents, ex, "changes_watch", {}, level=1)
        google.add_file("/Price lists", "Price list 2027.pdf", b"%PDF", root=google.sales, by=DANA)
        google.add_file("/Contracts", "Mine.pdf", b"%PDF")
        result = ok(agents, ex, "changes_new", {}, level=1)
        assert [(r["name"], r["change"]) for r in result["rows"]] == [("Price list 2027.pdf", "changed")]
        assert ok(agents, ex, "changes_new", {}, level=1)["rows"] == []

    def test_a_folder_watch_sees_only_that_folder(self, agents, google):
        ex, _ = chat(google)
        ok(agents, ex, "changes_watch", {"folder_path": "/Contracts", "include_mine": True}, level=1)
        google.add_file("/Contracts", "Addendum.pdf", b"%PDF")
        google.add_file("/Finance", "Invoice.pdf", b"%PDF")
        assert [r["name"] for r in ok(agents, ex, "changes_new", {}, level=1)["rows"]] == ["Addendum.pdf"]

    def test_a_gone_shared_drive_closes_its_watch(self, agents, google):
        ex, _ = chat(google)
        ok(agents, ex, "changes_watch", {"drive_id": google.sales}, level=1)
        del google.drives[google.sales]
        assert len(ok(agents, ex, "changes_new", {}, level=1)["closed"]) == 1


# -- sheets ------------------------------------------------------------------------------------------------------------

class TestSheets:
    def test_a_window_is_read_with_the_header_named_not_the_whole_tab(self, agents, google):
        ex, _ = chat(google)
        first = ok(agents, ex, "sheets_read", {"spreadsheet_id": google.sheet, "tab": "Orders", "max_rows": 2},
                   level=0)
        assert first["header"] == ["Customer", "Item", "Qty", "Total"]
        assert first["rows"][0][:2] == ["Riverside Clinic", "Chairs"] and first["next_row"] == 4
        rest = ok(agents, ex, "sheets_read", {"spreadsheet_id": google.sheet, "tab": "Orders",
                                              "from_row": first["next_row"]}, level=0)
        # The finding: a continued read took its first data row as the header.
        assert rest["header"] == ["Customer", "Item", "Qty", "Total"]
        assert rest["rows"][0][0] == "Riverside Clinic" and rest["rows"][0][3] == "220,25"
        window = google.requests("GET", "values:batchGet")[-1]["ranges"]
        assert window == ["'Orders'!A1:Z1", "'Orders'!A4:Z53"]

    def test_text_that_looks_like_a_formula_stays_text(self, agents, google):
        ex, provider = chat(google)
        ok(agents, ex, "sheets_append", {"spreadsheet_id": google.sheet, "tab": "Orders",
                                         "rows": [["=IMPORTDATA(\"https://evil.example\")", "Pens", 5, 9.75]]})
        assert google.requests("POST", ":append")[-1]["query"]["valueInputOption"] == "RAW"
        tab = google.tab(google.sheet, "Orders")
        assert tab["formulas"] == {} and tab["cells"][(5, 1)].startswith("=IMPORTDATA") and tab["cells"][(5, 3)] == 5
        ok(agents, ex, "sheets_append", {"spreadsheet_id": google.sheet, "tab": "Orders",
                                         "rows": [["Total", "", "", "=SUM(D2:D5)"]], "formulas": True})
        assert tab["formulas"] == {(6, 4): "=SUM(D2:D5)"}
        assert len(provider.data["write"]) == 2

    def test_an_overwrite_keeps_what_the_cells_held(self, agents, google):
        ex, provider = chat(google)
        result = ok(agents, ex, "sheets_update", {"spreadsheet_id": google.sheet, "tab": "Orders", "start_cell": "C3",
                                                  "rows": [[4]]})
        assert result["previous"] == [[3]] and google.tab(google.sheet, "Orders")["cells"][(3, 3)] == 4
        record = provider.data["write"][result["write_ref"]]
        assert record["values"]["previous"]["rows"] == [[3]]
        past, status = call(agents, ex, "sheets_update", {"spreadsheet_id": google.sheet, "tab": "Orders",
                                                          "start_cell": "A1000", "rows": [["x"], ["y"]]})
        assert status == "error" and "sheets_append" in past["error"]

    def test_a_new_spreadsheet_and_a_new_tab(self, agents, google):
        ex, _ = chat(google)
        made = ok(agents, ex, "sheets_create", {"title": "Sign-ups", "tabs": ["Workshop", "Waitlist"],
                                                "header": ["Name", "Email"], "folder_path": "/Finance"})
        assert google.grid(made["spreadsheet_id"], "Waitlist") == [["Name", "Email"]]
        ok(agents, ex, "sheets_add_tab", {"spreadsheet_id": made["spreadsheet_id"], "title": "2027",
                                          "header": ["Name"]})
        assert [t["title"] for t in google.sheets[made["spreadsheet_id"]]["tabs"]] == ["Workshop", "Waitlist", "2027"]

    def test_a_value_is_found_with_its_cell(self, agents, google):
        ex, _ = chat(google)
        found = ok(agents, ex, "sheets_find", {"spreadsheet_id": google.sheet, "text": "riverside"}, level=0)
        assert [m["cell"] for m in found["matches"]] == ["A2", "A4"]

    def test_totals_are_exact_in_a_decimal_comma_locale(self, agents, google):
        ex, _ = chat(google)
        result = ok(agents, ex, "sheets_totals", {"spreadsheet_id": google.sheet, "tab": "Orders",
                                                  "columns": ["Total", "C"], "group_by": "Customer"}, level=0)
        total, qty = result["columns"]
        assert (total["sum"], total["count"], total["min"], total["max"]) == ("2072.75", 3, "12.5", "1840")
        assert qty["sum"] == "19"
        assert {(g["group"], g["sum"]) for g in result["groups"] if g["column"] == "D"} == {
            ("Riverside Clinic", "2060.25"), ("Gulf Dental", "12.5")}


class TestRowsWatch:
    def test_a_row_deleted_and_another_added_is_still_seen(self, agents, google):
        ex, _ = chat(google)
        ok(agents, ex, "rows_watch", {"spreadsheet_id": google.sheet, "tab": "Orders"}, level=1)
        google.delete_row(google.sheet, "Orders", 3)
        google.append_row(google.sheet, "Orders", ["Palm Clinic", "Chairs", 6, 920.0])
        result = ok(agents, ex, "rows_new", {}, level=1)
        assert [(r["event"], r["cells"]["Customer"]) for r in result["rows"]] == [("row", "Palm Clinic")]
        assert ok(agents, ex, "rows_new", {}, level=1)["rows"] == []
        google.append_row(google.sheet, "Orders", ["Gulf Dental", "Pens", 1, 2.0])
        assert [r["row"] for r in ok(agents, ex, "rows_new", {}, level=1)["rows"]] == [5]

    def test_a_sorted_tab_resets_the_watch_out_loud(self, agents, google):
        ex, _ = chat(google)
        ok(agents, ex, "rows_watch", {"spreadsheet_id": google.sheet, "tab": "Orders"}, level=1)
        tab = google.tab(google.sheet, "Orders")
        tab["cells"] = {k: v for k, v in tab["cells"].items() if k[0] == 1}
        google.append_row(google.sheet, "Orders", ["Zed", "Item", 1, 1.0])
        result = ok(agents, ex, "rows_new", {}, level=1)
        assert [r["event"] for r in result["rows"]] == ["reset"]
        google.append_row(google.sheet, "Orders", ["Next", "Item", 1, 1.0])
        assert [r["cells"]["Customer"] for r in ok(agents, ex, "rows_new", {}, level=1)["rows"]] == ["Next"]

    def test_a_renamed_tab_is_still_watched_and_a_deleted_one_closes(self, agents, google):
        ex, _ = chat(google)
        ok(agents, ex, "rows_watch", {"spreadsheet_id": google.sheet, "tab": "Orders"}, level=1)
        google.tab(google.sheet, "Orders")["title"] = "Orders 2026"
        google.append_row(google.sheet, "Orders 2026", ["Late Co", "Desk", 1, 400.0])
        assert [r["cells"]["Customer"] for r in ok(agents, ex, "rows_new", {}, level=1)["rows"]] == ["Late Co"]
        google.sheets[google.sheet]["tabs"].clear()
        assert len(ok(agents, ex, "rows_new", {}, level=1)["closed"]) == 1


# -- forms -------------------------------------------------------------------------------------------------------------

def feedback_form(google, count=7):
    items = [
        {"title": "Would you order again?", "questionItem": {"question": {"questionId": "q1", "required": True,
         "choiceQuestion": {"type": "RADIO", "options": [{"value": "Yes"}, {"value": "No"}, {"isOther": True}]}}}},
        {"title": "Delivery speed", "questionItem": {"question": {"questionId": "q2",
                                                                  "scaleQuestion": {"low": 1, "high": 5}}}},
        {"title": "Anything else?", "questionItem": {"question": {"questionId": "q3", "textQuestion": {}}}}]
    answers = [("Yes", "5"), ("Yes", "4"), ("No", "2"), ("Yes", "5"), ("Maybe later", "3"), ("No", "1"), ("Yes", "4")]
    responses = []
    for n in range(count):
        choice, speed = answers[n % len(answers)]
        responses.append({"responseId": f"r{n:03d}", "lastSubmittedTime": f"2026-11-0{1 + n % 9}T09:00:{n % 60:02d}Z",
                          "answers": {"q1": {"textAnswers": {"answers": [{"value": choice}]}},
                                      "q2": {"textAnswers": {"answers": [{"value": speed}]}},
                                      **({"q3": {"textAnswers": {"answers": [{"value": "شكراً"}]}}} if n == 0 else {})}})
    return google.add_form("Customer feedback", items, responses)


class TestForms:
    def test_questions(self, agents, google):
        form = feedback_form(google)
        ex, _ = chat(google)
        result = ok(agents, ex, "forms_get", {"form_id": form}, level=0)
        assert [(q["title"], q["kind"]) for q in result["questions"]] == [
            ("Would you order again?", "multiple_choice"), ("Delivery speed", "scale"), ("Anything else?", "short_text")]

    def test_responses_page_with_googles_own_token(self, agents, google):
        form = feedback_form(google, count=12)
        ex, _ = chat(google)
        seen, token = [], ""
        while True:
            page = ok(agents, ex, "responses_list", {"form_id": form, "max_results": 5,
                                                     **({"page_token": token} if token else {})}, level=0)
            seen += [r["response_id"] for r in page["responses"]]
            token = page.get("next_page_token", "")
            if not token:
                break
        assert sorted(seen) == [f"r{n:03d}" for n in range(12)]
        assert all(r["query"]["pageSize"] == "5" for r in google.requests("GET", "/responses$"))

    def test_the_tally_is_counted_in_code(self, agents, google):
        form = feedback_form(google)
        ex, _ = chat(google)
        result = ok(agents, ex, "responses_tally", {"form_id": form}, level=0)
        order_again, speed, other = result["questions"]
        assert result["responses"] == 7
        assert order_again["counts"] == [{"option": "Yes", "count": 4}, {"option": "No", "count": 2},
                                         {"option": "Other", "count": 1}]
        assert speed["average"] == "3.43" and other["answered"] == 1

    def test_new_responses_are_handed_on_once_and_a_gone_form_closes(self, agents, google):
        form = feedback_form(google)
        ex, _ = chat(google)
        ok(agents, ex, "responses_watch", {"form_id": form}, level=1)
        google.forms[form]["responses"].append({
            "responseId": "r900", "lastSubmittedTime": "2026-11-15T10:00:00.5Z",
            "answers": {"q1": {"textAnswers": {"answers": [{"value": "No"}]}}}})
        result = ok(agents, ex, "responses_new", {}, level=1)
        assert [(r["response_id"], r["answers"]) for r in result["rows"]] == [
            ("r900", {"Would you order again?": "No"})]
        assert ok(agents, ex, "responses_new", {}, level=1)["rows"] == []
        del google.forms[form]
        assert len(ok(agents, ex, "responses_new", {}, level=1)["closed"]) == 1
