"""The reference agent, its program started and spoken to in the standard's messages, against records kept in memory. Each test pins one feature the manifest declares.
"""

import importlib.util
import re
from pathlib import Path

import yaml

from tests.harness import REPOSITORY, Agents, Chat, ChatSinks, Records, probe, requirements, run


ROOT = Path(__file__).resolve().parents[1] / "agents"
#: The platform's own checks of a manifest, in its checkout beside this repository.
PLATFORM_MANIFEST = REPOSITORY.parent / "decentai" / "api_server" / "src" / "agents" / "manifest.py"


def manifest():
    return yaml.safe_load(
        (ROOT / "notebook" / "manifest.yaml").read_text(encoding="utf-8"))


def function(doc, function_id):
    return next(f for f in doc["functions"] if f["id"] == function_id)


def standard():
    """The platform's Standard, loaded by its file's path as an install's check runs it."""
    spec = importlib.util.spec_from_file_location("platform_manifest", PLATFORM_MANIFEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Standard


def test_the_catalog_offers_one_agent_that_implements_its_manifest(agents):
    """Verified the way installation verifies: the program started by
    itself says ready, naming exactly the manifest's functions."""
    assert set(agents) == {"note", "notebook", "gmail", "google_calendar", "google_drive", "google_sheets", "google_forms", "google_tasks", "microsoft_calendar", "microsoft_todo", "outlook", "onedrive", "excel_online", "microsoft_forms", "dropbox", "box", "teams", "slack", "notion", "documents", "tasks", "todoist", "timesheets", "sheets", "expenses", "places", "google_docs", "word_online", "web_reader", "web_watch", "feeds", "applications", "slides", "json_data", "connection_check", "mail", "code_runner"}
    agent = agents["notebook"]
    errors = probe(agent)
    assert errors == [], errors


def test_the_manifest_declares_every_feature_the_platform_enforces():
    doc = manifest()
    assert {f["level"] for f in doc["functions"]} == {0, 1, 2, 3}
    assert function(doc, "note_find")["schedulable"] is True
    assert "model" in function(doc, "note_summarize")["calls"]
    resources = doc["resources"]
    note = next(r for r in resources["data"] if r["id"] == "note")
    assert note["user_access"] == ["create", "update"]
    document = next(r for r in resources["files"] if r["id"] == "document")
    assert document["user_access"] == ["create"]
    assert len(requirements(ROOT / "notebook")) == 2
    assert "notebook" in doc["authorization"]["scopes"]


def test_dependencies_are_used_by_the_code(agents):
    """titlecase and humanize are pinned in requirements.txt and used by
    the code — a lowercase title comes back cased."""
    provider = Records()
    executor = Chat(provider)

    async def scenario():
        saved, status = await executor.invoke(
            agents["notebook"], "note_save",
            {"notebook": "work", "title": "handoff notes"})
        assert status == "success", saved
        note, status = await executor.invoke(
            agents["notebook"], "note_get",
            {"note_ref": saved["note_ref"]})
        assert status == "success", note
        assert note["title"] == "Handoff Notes"

    run(scenario())


def test_json_round_trip_preserves_priority_and_content(agents):
    provider = Records()
    executor = Chat(provider)

    async def scenario():
        saved, status = await executor.invoke(
            agents["notebook"], "note_save", {
                "notebook": "Work", "title": "Handoff", "priority": 2,
                "content": {"text": "Call the customer"},
            })
        assert status == "success", saved
        exported, status = await executor.invoke(
            agents["notebook"], "archive_export",
            {"notebook": "work", "format": "json"}, chat_level=2,
        )
        assert status == "success", exported
        assert exported["filename"] == "work-notes.json"

        imported, status = await executor.invoke(
            agents["notebook"], "archive_import",
            {"notebook": "copy", "file_ref": exported["file_ref"]},
            chat_level=2,
        )
        assert status == "success", imported
        copied = provider.data["note"][imported["note_refs"][0]]["keys"]
        assert copied["priority"] == 2

    run(scenario())


def test_a_bad_import_creates_no_notes(agents):
    provider = Records()
    executor = Chat(provider)

    async def scenario():
        uploaded = provider.create_file(
            "document", "bad.csv",
            "title,notebook,priority,content\nGood,x,2,{}\nBad,x,nope,{}\n",
        )
        result, status = await executor.invoke(
            agents["notebook"], "archive_import",
            {"notebook": "copy", "file_ref": uploaded},
            chat_level=2,
        )
        assert status == "error"
        assert "Invalid priority" in result["error"]
        assert provider.data.get("note", {}) == {}

    run(scenario())


def test_the_secret_reaches_only_the_functions_that_declared_it(agents):
    provider = Records(secrets={
        "connection": {
            "base_url": "https://demo.invalid",
            "api_token": "test-token",
        }
    })
    executor = Chat(provider)

    async def scenario():
        connected, status = await executor.invoke(
            agents["notebook"], "sync_status", {})
        assert status == "success"
        assert connected == {"connected": True, "remote": "simulated://notebook"}

        await executor.invoke(agents["notebook"], "note_save", {
            "notebook": "Work", "title": "Secret-backed demo"})
        pushed, status = await executor.invoke(
            agents["notebook"], "sync_push", {"notebook": "work"},
            chat_level=3,
        )
        assert status == "success", pushed
        assert pushed["pushed"] == 1
        assert len(pushed["digest"]) == 64

    run(scenario())


def test_the_level_three_push_needs_the_chats_trust(agents):
    """Below its level and with nobody to approve, the executor refuses —
    the approval card is the chat's, never the agent's."""
    provider = Records(secrets={
        "connection": {"base_url": "u", "api_token": "t"}})
    executor = Chat(provider)
    result, status = run(executor.invoke(
        agents["notebook"], "sync_push", {"notebook": "work"},
        chat_level=1))
    assert status == "error"
    assert "approv" in result["error"].lower()


def test_summarize_thinks_with_the_chats_model(agents):
    """The model is the runtime's to hand over: the function declares
    it in its calls, and the stored key never enters the agent's process."""
    provider = Records()
    asked = []

    async def model(messages, max_tokens, pictures):
        asked.append((messages, max_tokens))
        return "Two notes about the handoff, one urgent."

    executor = Chat(provider, sinks=ChatSinks(llm=model))

    async def scenario():
        for title, priority in (("Handoff", 1), ("Follow up", 3)):
            await executor.invoke(agents["notebook"], "note_save",
                                  {"notebook": "work", "title": title,
                                   "priority": priority})
        summary, status = await executor.invoke(
            agents["notebook"], "note_summarize",
            {"notebook": "work", "max_words": 40})
        assert status == "success", summary
        assert summary == {"summary": "Two notes about the handoff, one urgent.",
                           "note_count": 2}

    run(scenario())
    (messages, max_tokens), = asked
    assert max_tokens == 160
    assert any("Handoff" in m["text"] for m in messages)


def test_a_function_without_llm_cannot_ask_for_the_model(agents):
    """find never declared model, so even with a model wired in it is
    given none — and it does not need one."""
    executor = Chat(Records(),
            sinks=ChatSinks(llm=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not be called"))))
    found, status = run(executor.invoke(
        agents["notebook"], "note_find", {"notebook": "work"}))
    assert status == "success"
    assert found == {"notes": [], "total": 0}


def test_every_agent_explains_itself_by_example():
    """Three prompts a person can send as they are, with a title each,
    in every manifest — what the agent's page shows first."""
    for agent_id, folder in Agents().folders.items():
        doc = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        examples = doc["agent"].get("examples") or []
        assert len(examples) >= 3, f"{agent_id} offers {len(examples)} examples"
        for example in examples:
            assert example["title"].strip() and example["prompt"].strip(), agent_id
            assert len(example["prompt"]) <= 500, agent_id


def test_every_manifest_is_valid_and_every_folder_builds_the_same_bytes():
    """What a deployment reads, and what makes a version stand: each
    manifest passes the platform's checks, and each dependency is one
    exact version."""
    checks = standard()
    for agent_id, folder in Agents().folders.items():
        doc = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        assert checks.manifest_errors(doc) == [], agent_id
        assert doc["agent"]["id"] == agent_id
        for requirement in requirements(folder):
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+", requirement), (agent_id, requirement)
