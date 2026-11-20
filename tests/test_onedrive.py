"""The OneDrive agent (2.0.0: OneDrive with Word, Excel and Forms), its program spoken to in the standard's messages,
against a loopback Microsoft Graph as strict as the real one where the agent depends on it (graph_onedrive_stub.py):
Sidra Office Supplies' drive on a work account, or on a personal one, with a lease under Contracts, a budget
workbook, a Word proposal with Dana's comment, a form's responses, and a floor plan Dana shared from her own drive.
"""

from __future__ import annotations

import io
import time
from datetime import datetime
from pathlib import Path

import docx
import pytest
import yaml
from docx.oxml.ns import qn

from tests.conftest import resource
from tests.graph_onedrive_stub import GRAPH, GraphOneDriveStub, Stamp
from tests.harness import Chat, ChatSinks, Records, probe, run

DANA = "dana@sidra.example"
LEASE = b"%PDF-1.4\n% Riverside office lease\n"
FLOOR_PLAN = b"%PDF-1.4\n% showroom floor plan\n"
DELIVERY = "Delivery takes fifteen working days from the signed order."
TEN_DAYS = "Delivery takes ten working days from the signed order."
ARABIC = "يتم التسليم خلال عشرة أيام عمل من تاريخ الطلب الموقع."
NOTES = Path(r"C:\Users\Ahmed Mukhtar\AppData\Local\Claude-Data")
SATISFIED = "How satisfied were you with your delivery?"
IMPROVE = "Anything we should improve?"


def proposal(comments=(("Dana Harbour", "Can this be ten days?"),), delivery=DELIVERY):
    document = docx.Document()
    document.add_heading("Harbourline proposal", level=1)
    document.add_paragraph("Sidra Office Supplies will furnish twenty desks.")
    paragraph = document.add_paragraph()
    paragraph.add_run(delivery[:15]).bold = True
    paragraph.add_run(delivery[15:])
    document.add_paragraph("Prices are valid until 30 September 2026.")
    for number, (author, text) in enumerate(comments):
        comment = document.add_comment(paragraph.runs, text=text, author=author, initials=author[:2])
        comment._comment_elm.set(qn("w:date"), f"2026-09-0{number + 1}T10:00:00Z")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def texts(raw):
    return [p.text for p in docx.Document(io.BytesIO(raw)).paragraphs]


def response(n, name, satisfied, improve, minute=0):
    email = f"{name.split()[0].lower()}@harbourline.example" if name else "anonymous"
    start = datetime(2026, 9, n, 10, minute)
    return [n, Stamp(start), Stamp(start.replace(minute=minute + 3)), email, name, satisfied, improve]


def fill(stub):
    stub.lease = stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
    stub.proposal = stub.add_file("/", "Harbourline proposal.docx", proposal(), by=DANA)
    stub.dana_drive, stub.floor_plan = stub.share_with_me("Showroom floor plan.pdf", FLOOR_PLAN, DANA)
    return stub


@pytest.fixture
def work():
    stub = fill(GraphOneDriveStub().start())
    stub.orders = stub.add_workbook("/Sales", "Sidra orders.xlsx")
    stub.add_table(stub.orders, "Orders", "Orders", ["Order", "Customer", "Amount"],
                   [["SO-1041", "Harbourline", 18400], ["SO-1042", "Qasr Trading", 9200]])
    stub.feedback = stub.add_workbook("/Forms", "Customer feedback.xlsx")
    stub.add_table(stub.feedback, "Form1", "Form1",
                   ["ID", "Start time", "Completion time", "Email", "Name", SATISFIED, IMPROVE],
                   [response(1, "Dana Whitfield", "Very satisfied", ""),
                    response(2, "Omar Haddad", "Satisfied", "Call before delivering"),
                    response(3, "Lina Park", "Neutral", "The desk arrived scratched")])
    yield stub
    stub.stop()


@pytest.fixture
def personal():
    stub = fill(GraphOneDriveStub(personal=True).start())
    yield stub
    stub.stop()


def chat(stub, *more, sinks=None):
    provider = Records(secrets={"microsoft": [stub.secret(), *more] if more else stub.secret()})
    return Chat(provider, sinks), provider


def call(agents, ex, name, inputs=None, level=3):
    return run(ex.invoke(agents["onedrive"], name, inputs or {}, chat_level=level))


def ok(found):
    result, status = found
    assert status == "success", result
    return result


def refused(found, kind=None):
    result, status = found
    assert status == "error", result
    if kind:
        assert result.get("kind") == kind, result
    return result


def search_one(agents, ex, query):
    items = ok(call(agents, ex, "files_search", {"query": query}))["items"]
    assert len(items) == 1, items
    return items[0]


# -- the package ------------------------------------------------------------------------------------------------------

class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        assert probe(agents["onedrive"]) == []

    def test_its_manifest_keeps_the_catalog_limits(self, agents):
        manifest = agents["onedrive"].manifest
        assert manifest["agent"]["version"] == "2.1.0"
        assert 35 <= len(manifest["functions"]) <= 50
        assert len(manifest["agent"]["instructions"]) <= 4000 and len(manifest["agent"]["examples"]) <= 6
        for function in manifest["functions"]:
            assert function["id"].split("_")[0] in ("account", "files", "sharing", "word", "excel", "forms",
                                                    "watches"), function["id"]
            # Every function takes the account it works on.
            assert "account" in function["inputs"]["properties"], function["id"]

    def test_it_declares_the_one_microsoft_secret_every_microsoft_agent_shares(self, agents):
        shared = NOTES / "claude_pzs8sxrjxfjjc" / "side-session-notes" / \
            "local_b2eed548-08bf-4c4d-aff8-e51a2fab81a9" / "shared-secret-blocks.yaml"
        mine = resource(agents["onedrive"].manifest, "secrets", "microsoft")
        assert mine["binding"] == {"cardinality": "many", "required": True}
        assert "MailboxSettings.Read" in mine["oauth"]["scopes"]
        if shared.is_file():
            assert mine == yaml.safe_load(shared.read_text(encoding="utf-8"))["microsoft"][0]

    def test_it_declares_every_host_a_download_or_upload_goes_to(self, agents):
        hosts = set(agents["onedrive"].manifest["network"]["hosts"])
        assert {"graph.microsoft.com", "*.sharepoint.com", "*.1drv.com", "*.microsoftpersonalcontent.com",
                "*.svc.ms", "*.storage.live.com", "*.livefilestore.com", "*.onedrive.com"} == hosts

    def test_levels_follow_who_sees_the_change(self, agents):
        levels = {f["id"]: f["level"] for f in agents["onedrive"].manifest["functions"]}
        assert {f for f, level in levels.items() if level == 3} == {"sharing_invite", "sharing_link",
                                                                    "sharing_revoke"}
        assert {f for f, level in levels.items() if level == 2} == {
            "files_upload", "files_create_folder", "files_move", "files_move_many", "files_copy", "files_delete",
            "files_restore", "word_apply", "excel_write", "excel_add_rows"}
        assert levels["files_download"] == 0
        schedulable = [f for f in agents["onedrive"].manifest["functions"] if f.get("schedulable")]
        assert [f["id"] for f in schedulable] == ["watches_check"]
        assert schedulable[0]["level"] == 1 and "model" not in schedulable[0]["calls"]
        assert all("record.create:microsoft" not in f["calls"] for f in schedulable)

    def test_what_people_wrote_is_kept_encrypted(self, agents):
        manifest = agents["onedrive"].manifest
        storage = {(r["id"], f["name"]): f["storage"] for r in manifest["resources"]["data"] for f in r["fields"]}
        for field in [("share", "who"), ("share", "link"), ("edit", "old_text"), ("edit", "new_text"),
                      ("edit", "note"), ("write", "written"), ("write", "previous"), ("watch", "note")]:
            assert storage[field] == "values", field


# -- accounts ---------------------------------------------------------------------------------------------------------

class TestAccounts:
    def test_status_tells_a_work_drive_from_a_personal_one(self, agents, work, personal):
        ex, _ = chat(work, personal.secret(ref="secret_personal"))
        result = ok(call(agents, ex, "account_status"))
        kinds = {a["account"]: (a["drive_type"], a["personal"]) for a in result["accounts"]}
        assert kinds == {work.account: ("business", False), personal.account: ("personal", True)}

    def test_with_two_accounts_the_call_names_one(self, agents, work, personal):
        ex, _ = chat(work, personal.secret(ref="secret_personal"))
        assert "Several" in refused(call(agents, ex, "files_search", {"query": "lease"}), "auth")["error"]
        found = ok(call(agents, ex, "files_search", {"query": "lease", "account": "outlook"}))
        assert [i["name"] for i in found["items"]] == ["Riverside office lease.pdf"]

    def test_an_expired_token_says_reconnect(self, agents, work):
        provider = Records(secrets={"microsoft": work.secret(access_token="expired")})
        result = refused(run(Chat(provider).invoke(agents["onedrive"], "files_search", {"query": "lease"})), "auth")
        assert "reconnect" in result["error"]


# -- files ------------------------------------------------------------------------------------------------------------

class TestFinding:
    def test_search_finds_own_and_shared_files(self, agents, work):
        ex, _ = chat(work)
        lease = search_one(agents, ex, "lease")
        assert lease["folder"] == "/Contracts" and "drive_id" not in lease
        plan = search_one(agents, ex, "floor plan")
        assert plan["drive_id"] == work.dana_drive and plan["shared_by"] == DANA

    def test_a_folder_is_listed_a_page_at_a_time(self, agents, work):
        for n in range(5):
            work.add_file("/Archive", f"Invoice {n}.pdf", b"%PDF")
        ex, _ = chat(work)
        first = ok(call(agents, ex, "files_list", {"folder_path": "/Archive", "max_results": 3}))
        assert len(first["items"]) == 3 and first["next_page_token"]
        second = ok(call(agents, ex, "files_list", {"page_token": first["next_page_token"]}))
        assert [i["name"] for i in second["items"]] == ["Invoice 3.pdf", "Invoice 4.pdf"]

    @pytest.mark.parametrize("mode, how", [("full", "sharedWithMe"), ("one", "sharedWithMe and search"),
                                           ("empty", "search"), ("gone", "search")])
    def test_shared_with_me_survives_its_retirement(self, agents, work, mode, how):
        work.share_with_me("Showroom budget.xlsx", b"PK", DANA)
        work.shared_mode = mode
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_shared_with_me"))
        assert sorted(i["name"] for i in result["items"]) == ["Showroom budget.xlsx", "Showroom floor plan.pdf"]
        assert all(i["drive_id"] == work.dana_drive for i in result["items"]) and result["found_by"] == how

    def test_a_busy_service_is_waited_out(self, agents, work):
        work.throttle("/search", times=1, retry_after="1")
        ex, _ = chat(work)
        assert search_one(agents, ex, "lease")["name"] == "Riverside office lease.pdf"
        assert len(work.asked(GRAPH, "/search")) == 2

    def test_versions_are_listed(self, agents, work):
        work.save_as(work.lease, LEASE + b"% signed\n", DANA)
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_versions", {"item_id": work.lease}))
        assert result["total"] == 2 and result["versions"][0]["modified_by"] == DANA


class TestDownloading:
    def test_a_work_file_comes_from_sharepoint_without_the_token(self, agents, work):
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_download", {"item_id": work.lease}))
        assert ex.files[result["file_ref"]]["content"] == LEASE
        fetched = work.asked(work.download_host, "/download/")
        assert len(fetched) == 1 and "Authorization" not in fetched[0]["headers"]

    def test_a_personal_file_comes_from_microsoftpersonalcontent(self, agents, personal):
        ex, _ = chat(personal)
        result = ok(call(agents, ex, "files_download", {"item_id": personal.lease}))
        assert ex.files[result["file_ref"]]["content"] == LEASE
        assert work_hosts(personal) == {"my.microsoftpersonalcontent.com"}

    def test_without_a_download_url_the_redirect_is_followed_without_the_token(self, agents, personal):
        personal.hide_download_url = True
        ex, _ = chat(personal)
        result = ok(call(agents, ex, "files_download", {"item_id": personal.lease}))
        assert ex.files[result["file_ref"]]["content"] == LEASE
        assert work_hosts(personal) == {"public.bn1302.files.1drv.com"}

    def test_a_pdf_conversion_comes_from_the_media_proxy(self, agents, work):
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_download", {"item_id": work.proposal, "as_pdf": True}))
        assert result["filename"] == "Harbourline proposal.pdf"
        assert ex.files[result["file_ref"]]["content"].startswith(b"%PDF-1.7")
        assert work.asked(work.MEDIA_HOST, "/transform/pdf/")

    def test_a_file_someone_shared_is_fetched_from_their_drive(self, agents, work):
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_download", {"item_id": work.floor_plan, "drive_id": work.dana_drive}))
        assert ex.files[result["file_ref"]]["content"] == FLOOR_PLAN

    def test_the_old_manifests_hosts_would_have_refused_the_download(self, agents, work):
        agent = agents["onedrive"].declaring(["graph.microsoft.com"])
        ex, _ = chat(work)
        result = refused(run(ex.invoke(agent, "files_download", {"item_id": work.lease}, chat_level=3)))
        assert "sidra-my.sharepoint.com" in result["error"]

    def test_a_file_over_the_chat_limit_is_refused_before_a_byte_is_read(self, agents, work):
        big = work.add_file("/Media", "Showroom walkthrough.mp4", b"\0" * (8 * 1024 * 1024))
        ex, _ = chat(work)
        result = refused(call(agents, ex, "files_download", {"item_id": big}), "too_large")
        assert "8.0 MB" in result["error"] and "sharepoint.com" in result["error"]
        assert not work.asked(work.download_host, "/download/")


class TestFiling:
    def test_a_small_file_goes_up_in_one_request_and_never_overwrites(self, agents, work):
        ex, _ = chat(work)
        ref = ex.file("Riverside office lease.pdf", b"%PDF new lease")
        result = ok(call(agents, ex, "files_upload", {"file_ref": ref, "folder_path": "/Contracts"}))
        assert result["item"]["name"] == "Riverside office lease 1.pdf" and result["how"] == "one request"

    def test_words_are_saved_as_a_new_file(self, agents, work):
        # Seen live: "save this as a file in my OneDrive" had no way but a chat file.
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_upload", {"text": "ملخص الاجتماع: hello from the agents review",
                                                      "name": "ملخص.txt", "folder_path": "/Contracts"}))
        assert result["item"]["name"] == "ملخص.txt"
        assert work.content(result["item"]["item_id"]).decode("utf-8").startswith("ملخص الاجتماع")
        refused(call(agents, ex, "files_upload", {"text": "no name", "folder_path": "/Contracts"}))
        refused(call(agents, ex, "files_upload", {"folder_path": "/Contracts"}))

    def test_a_large_file_goes_up_through_a_session_in_320_kib_fragments(self, agents, work):
        data = bytes(range(256)) * (5 * 1024 * 1024 // 256 + 77)
        ex, _ = chat(work)
        ref = ex.file("عرض الأسعار.pdf", data)
        result = ok(call(agents, ex, "files_upload", {"file_ref": ref, "folder_path": "/Contracts"}))
        assert result["how"] == "upload session" and result["item"]["name"] == "عرض الأسعار.pdf"
        assert work.content(result["item"]["item_id"]) == data
        parts = work.asked(work.upload_host, "/up/", "PUT")
        assert len(parts) == 2 and all("Authorization" not in p["headers"] for p in parts)

    def test_folders_moves_and_renames(self, agents, work):
        ex, _ = chat(work)
        folder = ok(call(agents, ex, "files_create_folder", {"name": "2026", "folder_path": "/Contracts"}))["item"]
        moved = ok(call(agents, ex, "files_move", {"item_id": work.lease, "to_folder_id": folder["item_id"],
                                                   "new_name": "Lease 2026.pdf"}))["item"]
        assert moved["folder"] == "/Contracts/2026" and moved["name"] == "Lease 2026.pdf"
        refused(call(agents, ex, "files_create_folder", {"name": "2026", "folder_path": "/Contracts"}), "conflict")

    def test_move_many_moves_the_rest_when_one_fails(self, agents, work):
        notes = work.add_file("/", "Notes.txt", b"n")
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_move_many", {"item_ids": [work.lease, "01MISSING", notes],
                                                         "to_folder_path": "/Archive/2026"
                                                         if work.add_folder("/Archive/2026") else ""}))
        assert [m["name"] for m in result["moved"]] == ["Riverside office lease.pdf", "Notes.txt"]
        assert result["failed"][0]["kind"] == "not_found"

    def test_a_copy_is_followed_until_it_is_done(self, agents, work):
        ex, _ = chat(work)
        result = ok(call(agents, ex, "files_copy", {"item_id": work.lease, "to_folder_path": "/Archive"
                                                    if work.add_folder("/Archive") else ""}))
        assert result["status"] == "completed" and result["item"]["name"] == "Riverside office lease.pdf"
        assert result["monitor"].startswith("http://sidra.sharepoint.com")

    def test_a_personal_copy_sends_no_conflict_behaviour(self, agents, personal):
        personal.copy_polls = 0
        ex, _ = chat(personal)
        result = ok(call(agents, ex, "files_copy", {"item_id": personal.lease, "to_folder_path": "/",
                                                    "new_name": "Lease copy.pdf"}))
        assert result["status"] == "completed" and result["item"]["name"] == "Lease copy.pdf"

    def test_a_slow_copy_comes_back_in_progress_and_is_asked_again(self, agents, work):
        work.copy_polls = 40
        ex, _ = chat(work)
        started = ok(call(agents, ex, "files_copy", {"item_id": work.lease, "to_folder_path": "/"}))
        assert started["status"] == "in_progress"
        work.monitors[started["monitor"].rsplit("/", 1)[1]]["polls"] = 0
        assert ok(call(agents, ex, "files_copy_status", {"monitor": started["monitor"]}))["status"] == "completed"
        refused(call(agents, ex, "files_copy_status", {"monitor": "http://evil.example/monitor/1"}))

    def test_delete_goes_to_the_recycle_bin_and_personal_restores(self, agents, work, personal):
        ex, _ = chat(work)
        deleted = ok(call(agents, ex, "files_delete", {"item_id": work.lease}))
        assert work.lease in work.recycle and "OneDrive on the web" in deleted["note"]
        assert refused(call(agents, ex, "files_restore", {"item_id": work.lease}), "unsupported")
        ex, _ = chat(personal)
        ok(call(agents, ex, "files_delete", {"item_id": personal.lease}))
        restored = ok(call(agents, ex, "files_restore", {"item_id": personal.lease}))["item"]
        assert restored["name"] == "Riverside office lease.pdf" and personal.lease not in personal.recycle


def work_hosts(stub):
    return {e["host"] for e in stub.log if e["host"] != GRAPH}


# -- sharing ----------------------------------------------------------------------------------------------------------

class TestSharing:
    def test_an_invitation_on_a_work_account_is_recorded_with_who_encrypted(self, agents, work):
        ex, provider = chat(work)
        result = ok(call(agents, ex, "sharing_invite", {"item_id": work.lease, "recipients": [DANA],
                                                        "message": "The signed lease."}))
        assert result["emailed"] is True and result["invited"] == [DANA]
        record = provider.data["share"][result["share_ref"]]
        assert record["values"]["who"] == DANA and "who" not in record["keys"]
        body = work.asked(GRAPH, "/invite")
        assert len(body) == 1

    def test_a_personal_account_whose_invitation_mail_fails_still_shares(self, agents, personal):
        personal.invite_mail_fails = True
        ex, _ = chat(personal)
        result = ok(call(agents, ex, "sharing_invite", {"item_id": personal.lease, "recipients": [DANA]}))
        assert result["emailed"] is False and "problem sharing" in result["note"]
        assert result["link"].startswith("https://1drv.ms/")
        assert sum(1 for p in personal.permissions[personal.lease] if (p.get("invitation") or {}).get("email")
                   == DANA) == 1

    def test_a_recipient_microsoft_could_not_notify_is_named(self, agents, personal):
        personal.unverified = {DANA}
        ex, _ = chat(personal)
        result = ok(call(agents, ex, "sharing_invite", {"item_id": personal.lease, "recipients": [DANA]}))
        assert result["emailed"] is False and result["not_notified"][0]["fix"].startswith("http://g.live.com")

    def test_names_and_the_root_are_refused(self, agents, personal):
        ex, _ = chat(personal)
        refused(call(agents, ex, "sharing_invite", {"item_id": personal.lease, "recipients": ["Dana"]}))
        assert "top of a drive" in refused(call(agents, ex, "sharing_invite", {
            "item_id": personal.root, "recipients": [DANA]}))["error"]

    def test_an_organization_link_on_a_personal_account_is_refused_clearly(self, agents, personal):
        ex, _ = chat(personal)
        result = refused(call(agents, ex, "sharing_link", {"item_id": personal.lease, "link_scope": "organization"}),
                         "unsupported")
        assert "work or school" in result["error"]
        assert "anyone" in refused(call(agents, ex, "sharing_link", {"item_id": personal.lease}))["error"]
        assert not personal.asked(GRAPH, "/createLink")
        made = ok(call(agents, ex, "sharing_link", {"item_id": personal.lease, "link_scope": "anonymous"}))
        assert made["reach"] == "anyone with the link"

    def test_a_work_link_is_for_the_organization_and_revoking_it_is_recorded(self, agents, work):
        ex, provider = chat(work)
        made = ok(call(agents, ex, "sharing_link", {"item_id": work.lease}))
        assert made["reach"] == "people in your organization"
        rows = ok(call(agents, ex, "sharing_permissions", {"item_id": work.lease}))["permissions"]
        link = next(r for r in rows if r["via"] == "link")
        assert "cannot be removed" in refused(call(agents, ex, "sharing_revoke", {
            "item_id": work.lease, "permission_id": "owner"}))["error"]
        gone = ok(call(agents, ex, "sharing_revoke", {"item_id": work.lease, "permission_id": link["permission_id"]}))
        assert provider.data["share"][gone["share_ref"]]["keys"]["status"] == "revoked"


# -- Word -------------------------------------------------------------------------------------------------------------

class TestWord:
    def test_a_document_is_read_by_paragraph_with_its_comments_and_fetched_once(self, agents, work):
        ex, _ = chat(work)
        found = ok(call(agents, ex, "word_find", {"query": "Harbourline"}))["documents"]
        assert [d["name"] for d in found] == ["Harbourline proposal.docx"]
        read = ok(call(agents, ex, "word_read", {"item_id": work.proposal}))
        assert read["paragraphs"][2]["text"] == DELIVERY and read["comment_count"] == 1
        comments = ok(call(agents, ex, "word_comments", {"item_id": work.proposal}))["comments"]
        assert comments[0]["paragraph"] == 3 and comments[0]["author"] == "Dana Harbour"
        assert len(work.asked(work.download_host, "/download/")) == 1

    def test_an_edit_is_proposed_then_applied_with_if_match_on_the_content_tag(self, agents, work):
        ex, provider = chat(work)
        edit = ok(call(agents, ex, "word_propose", {"item_id": work.proposal, "paragraph": 3, "new_text": ARABIC}))
        record = provider.data["edit"][edit["edit_ref"]]
        assert record["values"]["new_text"] == ARABIC and "new_text" not in record["keys"]
        work.rename(work.proposal, "Harbourline proposal v2.docx")  # moves the eTag only
        applied = ok(call(agents, ex, "word_apply", {"edit_ref": edit["edit_ref"]}))
        assert applied["applied"] is True and texts(work.content(work.proposal))[2] == ARABIC
        put = work.asked(GRAPH, "/content", "PUT")[0]
        assert put["headers"]["If-Match"].startswith('"c:')

    def test_a_save_by_someone_else_refuses_the_edit(self, agents, work):
        ex, provider = chat(work)
        edit = ok(call(agents, ex, "word_propose", {"item_id": work.proposal, "paragraph": 3, "new_text": TEN_DAYS}))
        work.save_as(work.proposal, proposal(delivery="Delivery takes a month."), DANA)
        result = refused(call(agents, ex, "word_apply", {"edit_ref": edit["edit_ref"]}), "conflict")
        assert "changed" in result["error"]
        assert provider.data["edit"][edit["edit_ref"]]["keys"]["status"] == "refused"

    def test_a_save_between_the_check_and_the_put_is_refused_by_microsoft(self, agents, work):
        ex, _ = chat(work)
        edit = ok(call(agents, ex, "word_propose", {"item_id": work.proposal, "paragraph": 3, "new_text": TEN_DAYS}))
        work.before_put = lambda item_id: work.save_as(item_id, proposal(), DANA)
        refused(call(agents, ex, "word_apply", {"edit_ref": edit["edit_ref"]}), "conflict")

    def test_a_locked_document_is_reported_as_locked_and_the_edit_waits(self, agents, work):
        ex, provider = chat(work)
        edit = ok(call(agents, ex, "word_propose", {"item_id": work.proposal, "paragraph": 3, "new_text": TEN_DAYS}))
        work.locked.add(work.proposal)
        result = refused(call(agents, ex, "word_apply", {"edit_ref": edit["edit_ref"]}), "locked")
        assert "locked" in result["error"] and "open" in result["error"]
        assert provider.data["edit"][edit["edit_ref"]]["keys"]["status"] == "proposed"
        work.locked.clear()
        ok(call(agents, ex, "word_apply", {"edit_ref": edit["edit_ref"]}))
        assert texts(work.content(work.proposal))[2] == TEN_DAYS

    def test_a_shared_document_is_read_in_its_owners_drive(self, agents, work):
        drive, item = work.share_with_me("Dana's draft.docx", proposal(), DANA, role="write")
        ex, _ = chat(work)
        read = ok(call(agents, ex, "word_read", {"item_id": item, "drive_id": drive}))
        assert read["drive_id"] == drive and read["total_paragraphs"] == 4


# -- Excel ------------------------------------------------------------------------------------------------------------

class TestExcel:
    def test_a_personal_account_is_told_it_needs_a_work_account(self, agents, personal):
        ex, _ = chat(personal)
        for name, inputs in [("excel_find", {}), ("excel_rows", {"workbook_id": "x", "table": "Orders"}),
                             ("forms_find", {})]:
            assert "work or school" in refused(call(agents, ex, name, inputs), "unsupported")["error"]
        assert not personal.asked(GRAPH, "/workbook")

    def test_inside_a_workbook_and_its_table(self, agents, work):
        ex, _ = chat(work)
        book = ok(call(agents, ex, "excel_find", {"query": "orders"}))["workbooks"][0]
        inside = ok(call(agents, ex, "excel_get", {"workbook_id": book["item_id"]}))
        assert inside["tables"] == [{"name": "Orders", "worksheet": "Orders", "headers": ["Order", "Customer",
                                                                                         "Amount"], "rows": 2}]
        rows = ok(call(agents, ex, "excel_rows", {"workbook_id": book["item_id"], "table": "Orders"}))["rows"]
        assert rows[1] == {"Order": "SO-1042", "Customer": "Qasr Trading", "Amount": "9200"}

    def test_writes_ride_one_session_and_formula_like_text_stays_text(self, agents, work):
        ex, provider = chat(work)
        result = ok(call(agents, ex, "excel_write", {"workbook_id": work.orders, "writes": [
            {"worksheet": "Orders", "address": "D1:D2", "values": [["Note"], ["=HYPERLINK(\"http://x\")"]]},
            {"worksheet": "Orders", "address": "E1", "values": [["-cheap"]]}]}))
        assert work.cell(work.orders, "Orders", "D2") == "=HYPERLINK(\"http://x\")"
        assert type(work.cell(work.orders, "Orders", "D2")) is str
        sessions = {s for _, _, s in work.session_requests}
        assert len(sessions) == 1 and work.wb_sessions[0]["closed"] and len(result["written"]) == 2
        assert provider.data["write"][result["write_ref"]]["keys"]["status"] == "written"
        ok(call(agents, ex, "excel_write", {"workbook_id": work.orders, "formulas": True, "writes": [
            {"worksheet": "Orders", "address": "F2", "values": [["=C2*2"]]}]}))
        assert type(work.cell(work.orders, "Orders", "F2")).__name__ == "Formula"

    def test_a_throttled_workbook_is_waited_out_and_a_row_is_added_once(self, agents, work):
        work.throttle("/rows/add", times=1, retry_after="1", status=503)
        work.throttle("/tables/Orders/range", times=1, retry_after="1")
        ex, _ = chat(work)
        result = ok(call(agents, ex, "excel_add_rows", {"workbook_id": work.orders, "table": "Orders", "rows": [
            {"customer": "Harbourline", "Order": "SO-1043", "Amount": 18400}]}))
        assert result["added"] == 1 and result["total_rows"] == 3
        assert work.table(work.orders).body("Orders")[2] == ["SO-1043", "Harbourline", 18400]

    def test_a_write_the_gateway_gave_up_on_is_recorded_as_unknown_and_not_sent_again(self, agents, work):
        work.throttle("/range(address='D1:D1')", times=1, status=504, method="PATCH")
        ex, provider = chat(work)
        result = refused(call(agents, ex, "excel_write", {"workbook_id": work.orders, "writes": [
            {"worksheet": "Orders", "address": "D1", "values": [["Note"]]}]}), "unknown")
        assert "may or may not" in result["error"]
        assert len(work.asked(GRAPH, "range(address='D1:D1')", "PATCH")) == 1
        assert [r["keys"]["status"] for r in provider.data["write"].values()] == ["unknown"]

    def test_a_shared_workbook_is_reached_in_its_owners_drive(self, agents, work):
        drive, item = work.share_with_me("Dana budget.xlsx", b"PK", DANA)
        work.workbooks[item] = type(work.table(work.orders))()
        work.add_table(item, "Budget", "Budget", ["Line", "AED"], [["Desks", 18400]])
        ex, _ = chat(work)
        rows = ok(call(agents, ex, "excel_rows", {"workbook_id": item, "drive_id": drive, "table": "Budget"}))
        assert rows["rows"] == [{"Line": "Desks", "AED": "18400"}]


# -- Forms ------------------------------------------------------------------------------------------------------------

class TestForms:
    def test_responses_come_newest_first_with_iso_times(self, agents, work):
        ex, _ = chat(work)
        found = ok(call(agents, ex, "forms_find", {"query": "xlsx"}))
        first = found["workbooks"][0]
        assert first["name"] == "Customer feedback.xlsx" and first["questions"] == [SATISFIED, IMPROVE]
        assert not next(w for w in found["workbooks"] if w["name"] == "Sidra orders.xlsx")["is_response_workbook"]
        result = ok(call(agents, ex, "forms_responses", {"workbook_id": work.feedback}))
        latest = result["responses"][0]
        assert latest["response_id"] == 3 and latest["start_time"] == "2026-09-03T10:00:00"
        assert latest["completion_time"] == "2026-09-03T10:03:00" and latest["answers"][IMPROVE].startswith("The desk")

    def test_arabic_headers_are_recognised_by_place_and_kind(self, agents, work):
        arabic = work.add_workbook("/Forms", "استبيان العملاء.xlsx")
        work.add_table(arabic, "Form1", "Form1",
                       ["المعرف", "وقت البدء", "وقت الإكمال", "البريد الإلكتروني", "الاسم", "ما رأيك؟"],
                       [response(1, "Huda Saleh", "ممتاز", "")[:6], response(2, "", "جيد", "")[:6]])
        ex, _ = chat(work)
        result = ok(call(agents, ex, "forms_responses", {"workbook_id": arabic}))
        assert result["questions"] == ["ما رأيك؟"] and result["responses"][0]["answers"] == {"ما رأيك؟": "جيد"}
        assert result["responses"][0]["email"] == "anonymous"

    def test_forms_find_looks_at_several_workbooks_at_once_within_its_budget(self, agents, work):
        for n in range(6):
            book = work.add_workbook("/Forms", f"Survey {n}.xlsx")
            work.add_table(book, "Sheet1", "Table1", ["A", "B"], [[1, 2]])
        # Three slow requests a workbook, eight workbooks: one after another would be 24 seconds.
        work.delays["/workbook/tables"] = 1.0
        ex, _ = chat(work)
        started = time.monotonic()
        found = ok(call(agents, ex, "forms_find", {"query": "xlsx", "max_results": 8}))
        assert time.monotonic() - started < 12
        assert found["workbooks"][0]["is_response_workbook"] and found["not_checked"] == 0


# -- watches ----------------------------------------------------------------------------------------------------------

class TestWatches:
    def test_a_document_watch_fires_on_saves_by_others_not_on_renames(self, agents, work):
        ex, _ = chat(work)
        made = ok(call(agents, ex, "watches_document", {"item_id": work.proposal}))
        assert ok(call(agents, ex, "watches_document", {"item_id": work.proposal}))["already"] is True
        work.rename(work.proposal, "Harbourline proposal (final).docx")
        quiet = ok(call(agents, ex, "watches_check", level=1))
        assert quiet["documents"] == [] and quiet["problems"] == []
        work.save_as(work.proposal, proposal(comments=(("Dana Harbour", "Can this be ten days?"),
                                                       ("Omar Haddad", "Agreed with Dana."))), DANA)
        news = ok(call(agents, ex, "watches_check", level=1))
        assert news["documents"][0]["watch_ref"] == made["watch_ref"] and news["documents"][0]["modified_by"] == DANA
        assert [c["author"] for c in news["comments"]] == ["Omar Haddad"]
        assert ok(call(agents, ex, "watches_check", level=1))["documents"] == []

    def test_new_rows_and_responses_are_handed_on_once(self, agents, work):
        ex, _ = chat(work)
        ok(call(agents, ex, "watches_rows", {"workbook_id": work.orders, "table": "Orders"}))
        ok(call(agents, ex, "watches_responses", {"workbook_id": work.feedback}))
        work.table(work.orders).append("Orders", [["SO-1043", "Harbourline", 5000]])
        work.table(work.feedback).append("Form1", [response(4, "Sami Nasser", "Satisfied", "")])
        found = ok(call(agents, ex, "watches_check", level=1))
        assert [r["values"]["Order"] for r in found["rows"]] == ["SO-1043"]
        assert [r["response_id"] for r in found["responses"]] == [4]
        again = ok(call(agents, ex, "watches_check", level=1))
        assert again["rows"] == [] and again["responses"] == []

    def test_one_failing_watch_keeps_its_place_and_the_others_are_handed_on(self, agents, work):
        ex, _ = chat(work)
        ok(call(agents, ex, "watches_rows", {"workbook_id": work.orders, "table": "Orders"}))
        ok(call(agents, ex, "watches_responses", {"workbook_id": work.feedback}))
        work.table(work.orders).append("Orders", [["SO-1043", "Harbourline", 5000]])
        work.table(work.feedback).append("Form1", [response(4, "Sami Nasser", "Satisfied", "")])
        work.throttle("/tables/Orders", times=20, retry_after="120")
        first = ok(call(agents, ex, "watches_check", level=1))
        assert [r["response_id"] for r in first["responses"]] == [4] and first["rows"] == []
        assert first["problems"][0]["kind"] == "rate"
        work.throttles.clear()
        second = ok(call(agents, ex, "watches_check", level=1))
        assert [r["values"]["Order"] for r in second["rows"]] == ["SO-1043"] and second["responses"] == []

    def test_a_gone_document_closes_its_watch(self, agents, work):
        ex, _ = chat(work)
        made = ok(call(agents, ex, "watches_document", {"item_id": work.proposal}))
        work.recycle[work.proposal] = work.items().pop(work.proposal)
        result = ok(call(agents, ex, "watches_check", level=1))
        assert result["closed"][0]["watch_ref"] == made["watch_ref"] and result["problems"] == []
        assert ok(call(agents, ex, "watches_list"))["watches"] == []
        assert len(ok(call(agents, ex, "watches_list", {"include_closed": True}))["watches"]) == 1

    def test_a_watch_is_listed_and_closed(self, agents, work):
        ex, _ = chat(work)
        made = ok(call(agents, ex, "watches_rows", {"workbook_id": work.orders, "table": "Orders"}))
        listed = ok(call(agents, ex, "watches_list"))["watches"]
        assert listed[0]["kind"] == "rows" and listed[0]["table"] == "Orders"
        assert ok(call(agents, ex, "watches_close", {"watch_ref": made["watch_ref"]}))["closed"] is True
        assert ok(call(agents, ex, "watches_check", level=1))["checked"] == 0
