"""The Note agent, its program started and spoken to in the standard's messages, against records kept in memory.
Each test pins one feature the manifest declares — which is what makes this file worth copying alongside the agent.
"""

from pathlib import Path

import yaml

from tests.harness import Agents, Chat, ChatSinks, Records, probe, requirements, run


#: The folder the agent's own folder is in.
ROOT = Path(__file__).resolve().parents[1] / "agents"


def manifest():
    return yaml.safe_load((ROOT / "note" / "manifest.yaml").read_text(encoding="utf-8"))


def function(doc, function_id):
    return next(f for f in doc["functions"] if f["id"] == function_id)


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        """Verified the way installation verifies: the program started by
        itself says ready, naming exactly the manifest's functions."""
        agent = agents["note"]
        assert probe(agent) == []

    def test_each_folder_is_the_agent_its_manifest_names(self):
        for agent_id, folder in Agents().folders.items():
            assert folder.name == agent_id

    def test_the_manifest_declares_every_feature_the_platform_enforces(self):
        doc = manifest()
        assert {f["level"] for f in doc["functions"]} == {0, 1, 2, 3}
        assert function(doc, "notes_find")["schedulable"] is True
        assert "model" in function(doc, "notes_summarize")["calls"]
        resources = doc["resources"]
        note = next(r for r in resources["data"] if r["id"] == "note")
        assert note["user_access"] == ["create", "update"]
        document = next(r for r in resources["files"] if r["id"] == "document")
        assert document["user_access"] == ["create"]
        assert len(requirements(ROOT / "note")) == 2
        assert "notebook" in doc["authorization"]["scopes"]

    def test_it_explains_itself_by_example(self):
        """Three prompts a person can send as they are, with a title
        each — what the agent's page shows first."""
        examples = manifest()["agent"].get("examples") or []
        assert len(examples) >= 3
        for example in examples:
            assert example["title"].strip() and example["prompt"].strip()
            assert len(example["prompt"]) <= 500


class TestTheStorageSplit:
    """Where each field is kept, pinned by tests.

    `keys` are plaintext, filterable and scalar; `values` are encrypted
    at rest and may hold objects. A note's content is a plain scalar in
    keys; the sync token is encrypted in the secret's values. These tests
    fail loudly if anybody moves either."""

    def test_content_is_a_scalar_in_keys(self):
        doc = manifest()
        note = next(r for r in doc["resources"]["data"] if r["id"] == "note")
        content = next(f for f in note["fields"] if f["name"] == "content")
        assert content["storage"] == "keys", (
            "the template keeps content as plain text a filter can reach")
        assert content["type"] == "string", (
            "keys are scalar; the platform refuses an object in keys")

    def test_the_secret_keeps_its_token_in_values(self):
        doc = manifest()
        connection = next(s for s in doc["resources"]["secrets"] if s["id"] == "connection")
        storage = {f["name"]: f["storage"] for f in connection["fields"]}
        # The contrast worth learning: a token is encrypted and never
        # shown to a person again; a note body is plain text.
        assert storage["api_token"] == "values"
        assert storage["base_url"] == "keys"

    def test_a_saved_note_can_be_read_back_whole(self, agents):
        executor = Chat(Records())

        async def scenario():
            saved, status = await executor.invoke(agents["note"], "notes_save", {
                "notebook": "work", "title": "Handoff",
                "content": "Call the customer before Thursday."})
            assert status == "success", saved
            note, status = await executor.invoke(
                agents["note"], "notes_get", {"note_ref": saved["note_ref"]})
            assert status == "success", note
            assert note["content"] == "Call the customer before Thursday."

        run(scenario())


class TestTheDependencies:
    def test_they_live_in_the_agents_own_environment(self, agents):
        """titlecase and humanize are carried in the agent's program and
        used by the code — a lowercase title comes back cased."""
        executor = Chat(Records())

        async def scenario():
            saved, status = await executor.invoke(
                agents["note"], "notes_save",
                {"notebook": "work", "title": "handoff notes"})
            assert status == "success", saved
            note, status = await executor.invoke(
                agents["note"], "notes_get", {"note_ref": saved["note_ref"]})
            assert status == "success", note
            assert note["title"] == "Handoff Notes"

        run(scenario())


class TestFiles:
    def test_a_json_round_trip_keeps_priority_and_content(self, agents):
        provider = Records()
        executor = Chat(provider)

        async def scenario():
            saved, status = await executor.invoke(agents["note"], "notes_save", {
                "notebook": "Work", "title": "Handoff", "priority": 2,
                "content": "Call the customer before Thursday."})
            assert status == "success", saved
            exported, status = await executor.invoke(
                agents["note"], "archive_export",
                {"notebook": "work", "format": "json"}, chat_level=2)
            assert status == "success", exported
            assert exported["filename"] == "work-notes.json"

            imported, status = await executor.invoke(
                agents["note"], "archive_import",
                {"notebook": "copy", "file_ref": exported["file_ref"]}, chat_level=2)
            assert status == "success", imported
            copied = provider.data["note"][imported["note_refs"][0]]["keys"]
            assert copied["priority"] == 2
            # The whole point of the storage split, checked end to end:
            # a body kept in `values` would arrive here as null.
            assert copied["content"] == "Call the customer before Thursday."

        run(scenario())

    def test_a_csv_round_trip_keeps_content_too(self, agents):
        """Byte for byte, commas and line breaks included.

        A substring assertion would pass while the body came back wrapped
        in quotes with its newlines turned into two characters, which is
        exactly what happens if the writer encodes the content a second
        time on top of csv's own escaping."""
        provider = Records()
        executor = Chat(provider)
        body = ("Call the customer before Thursday.\n"
                "Ask about the chairs, the desks, and \"the carpet\".")

        async def scenario():
            await executor.invoke(agents["note"], "notes_save", {
                "notebook": "work", "title": "Handoff", "priority": 2,
                "content": body})
            exported, status = await executor.invoke(
                agents["note"], "archive_export",
                {"notebook": "work", "format": "csv"}, chat_level=2)
            assert status == "success", exported
            imported, status = await executor.invoke(
                agents["note"], "archive_import",
                {"notebook": "copy", "file_ref": exported["file_ref"]}, chat_level=2)
            assert status == "success", imported
            copied = provider.data["note"][imported["note_refs"][0]]["keys"]
            assert copied["content"] == body

        run(scenario())

    def test_the_shipped_sample_document_can_actually_be_imported(self, agents):
        """A sample file has to be something the agent can read.

        The sheet ships it for one purpose — load the samples, then ask
        to import it — and the file resource declares which types it
        accepts. This asserts the three agree: the file on disk, the
        mime types in the manifest, and the parser in the tool."""
        sheet = yaml.safe_load(
            (ROOT / "note" / "samples.yaml").read_text(encoding="utf-8"))
        shipped = sheet["files"][0]
        document = ROOT / "note" / shipped["path"]
        assert document.is_file(), document

        declared = manifest()["resources"]["files"]
        slot = next(f for f in declared if f["id"] == shipped["resource"])
        assert "text/csv" in slot["constraints"]["mime_types"]
        assert document.suffix == ".csv"

        provider = Records()
        executor = Chat(provider)

        async def scenario():
            uploaded = provider.create_file(
                shipped["resource"], document.name,
                document.read_text(encoding="utf-8"))
            imported, status = await executor.invoke(
                agents["note"], "archive_import",
                {"notebook": "handover", "file_ref": uploaded},
                chat_level=2)
            assert status == "success", imported
            assert imported["imported"] == 3
            bodies = [provider.data["note"][ref]["keys"]["content"]
                      for ref in imported["note_refs"]]
            assert any("\n" in body for body in bodies), "a quoted multi-line cell survives"

        run(scenario())

    def test_a_bad_import_creates_no_notes(self, agents):
        provider = Records()
        executor = Chat(provider)

        async def scenario():
            uploaded = provider.create_file(
                "document", "bad.csv",
                "title,notebook,priority,content\nGood,x,2,ok\nBad,x,nope,ok\n")
            result, status = await executor.invoke(
                agents["note"], "archive_import",
                {"notebook": "copy", "file_ref": uploaded}, chat_level=2)
            assert status == "error"
            assert "Invalid priority" in result["error"]
            # Validated whole before anything is written: a bad row must
            # not leave a half-imported notebook behind.
            assert provider.data.get("note", {}) == {}

        run(scenario())


class TestSecretsAndLevels:
    def test_the_secret_reaches_only_the_functions_that_declared_it(self, agents):
        provider = Records(secrets={
            "connection": {"base_url": "https://demo.invalid",
                                 "api_token": "test-token"}})
        executor = Chat(provider)

        async def scenario():
            connected, status = await executor.invoke(
                agents["note"], "sync_status", {})
            assert status == "success"
            assert connected == {"connected": True, "remote": "simulated://notes"}

            await executor.invoke(agents["note"], "notes_save", {
                "notebook": "Work", "title": "Secret-backed demo"})
            pushed, status = await executor.invoke(
                agents["note"], "sync_push", {"notebook": "work"}, chat_level=3)
            assert status == "success", pushed
            assert pushed["pushed"] == 1 and len(pushed["digest"]) == 64 and pushed["simulated"] is True

        run(scenario())

    def test_the_level_three_push_needs_the_chats_trust(self, agents):
        """Below its level and with nobody to approve, the executor
        refuses — the approval card is the chat's, never the agent's."""
        provider = Records(secrets={
            "connection": {"base_url": "u", "api_token": "t"}})
        executor = Chat(provider)
        result, status = run(executor.invoke(
            agents["note"], "sync_push", {"notebook": "work"}, chat_level=1))
        assert status == "error"
        assert "approv" in result["error"].lower()


class TestTheModel:
    def test_summarize_thinks_with_the_chats_model(self, agents):
        """The model is the runtime's to hand over: the function declares
        it in its calls, and the stored key never enters the agent's process."""
        asked = []

        async def model(messages, max_tokens, pictures):
            asked.append((messages, max_tokens))
            return "Two notes about the handoff, one urgent."

        executor = Chat(Records(),
                                    sinks=ChatSinks(llm=model))

        async def scenario():
            for title, priority in (("Handoff", 1), ("Follow up", 3)):
                await executor.invoke(agents["note"], "notes_save", {
                    "notebook": "work", "title": title, "priority": priority})
            summary, status = await executor.invoke(
                agents["note"], "notes_summarize",
                {"notebook": "work", "max_words": 40})
            assert status == "success", summary
            assert summary == {"summary": "Two notes about the handoff, one urgent.",
                               "note_count": 2}

        run(scenario())
        (messages, max_tokens), = asked
        assert max_tokens == 160
        assert any("Handoff" in m["text"] for m in messages)

    def test_the_model_is_given_the_note_bodies(self, agents):
        """Reading `content` back is what makes the summary worth having
        — and is impossible if the field sits in `values`."""
        seen = []

        async def model(messages, max_tokens, pictures):
            seen.extend(m["text"] for m in messages)
            return "A summary."

        executor = Chat(Records(),
                                    sinks=ChatSinks(llm=model))

        async def scenario():
            await executor.invoke(agents["note"], "notes_save", {
                "notebook": "work", "title": "Handoff",
                "content": "Call the customer before Thursday."})
            await executor.invoke(agents["note"], "notes_summarize",
                                  {"notebook": "work"})

        run(scenario())
        assert any("Call the customer before Thursday." in text for text in seen)

    def test_a_function_without_llm_cannot_ask_for_the_model(self, agents):
        """find never declared model, so even with a model wired in it is
        given none — and it does not need one."""
        executor = Chat(Records(),
            sinks=ChatSinks(llm=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not be called"))))
        found, status = run(executor.invoke(
            agents["note"], "notes_find", {"notebook": "work"}))
        assert status == "success"
        assert found == {"notes": [], "total": 0}


class TestScheduling:
    def test_find_runs_unattended(self, agents):
        """Schedulable: level 0, no model, callable at chat level 0."""
        executor = Chat(Records())

        async def scenario():
            await executor.invoke(agents["note"], "notes_save", {
                "notebook": "work", "title": "Handoff"})
            found, status = await executor.invoke(
                agents["note"], "notes_find", {"notebook": "work"}, chat_level=0)
            assert status == "success", found
            assert found["total"] == 1 and found["notes"][0]["title"] == "Handoff"

        run(scenario())

    def test_find_sees_every_note_past_a_page(self, agents):
        """record.list answers a page at a time; the library follows it to the end."""
        provider = Records()
        for n in range(150):
            provider.create_data("note", {"title": f"Note {n:03}", "notebook": "work"})
        found, status = run(Chat(provider).invoke(agents["note"], "notes_find", {"notebook": "work", "limit": 5}))
        assert status == "success", found
        assert found["total"] == 150 and found["notes"][0]["title"] == "Note 000"


class TestTalkingToThePerson:
    """call.ask and call.say: the runtime's chat, declared in calls."""

    @staticmethod
    def saved_twice(agents, asker):
        provider = Records()
        executor = Chat(provider, sinks=ChatSinks(ask=asker))

        async def scenario():
            first, _ = await executor.invoke(agents["note"], "notes_save", {
                "notebook": "work", "title": "Handoff", "content": "first"})
            second, status = await executor.invoke(agents["note"], "notes_save", {
                "notebook": "work", "title": "Handoff", "content": "second"})
            assert status == "success", second
            return first, second

        first, second = run(scenario())
        return provider, first, second

    def test_a_note_with_the_same_title_is_updated_when_the_person_says_so(self, agents):
        asked = []

        async def asker(question, choices, expects):
            asked.append((question, choices, expects))
            return "Update it"

        provider, first, second = self.saved_twice(agents, asker)
        [(question, choices, expects)] = asked
        assert "already in 'work'" in question and choices == ["Update it", "Keep both"] and expects == "choice"
        assert second == {"note_ref": first["note_ref"], "created": False}
        assert len(provider.data["note"]) == 1

    def test_with_nobody_to_answer_both_are_kept(self, agents):
        """None is an answer: a scheduled run with nobody watching, or
        a question nobody answered. The safe choice writes nothing over
        anything."""
        async def nobody(question, choices, expects):
            return None

        provider, first, second = self.saved_twice(agents, nobody)
        assert second["created"] is True and second["note_ref"] != first["note_ref"]
        assert len(provider.data["note"]) == 2

    def test_find_lists_in_the_order_the_persons_settings_say(self, agents):
        """The one row of `settings` is the person's own, written on the
        records page; find reads it and orders what it lists. The store
        keeps no time of a change: "updated" is newest written first, as
        "created" is."""
        provider = Records()
        executor = Chat(provider)

        async def titles(sort_order=None):
            provider.data.pop("settings", None)
            if sort_order:
                provider.create_data(
                    "settings", {"sort_order": sort_order}, {})
            found, status = await executor.invoke(
                agents["note"], "notes_find", {"notebook": "work"})
            assert status == "success", found
            return [note["title"] for note in found["notes"]]

        async def scenario():
            for title in ("Banana", "Cherry", "Apple"):
                await executor.invoke(agents["note"], "notes_save", {
                    "notebook": "work", "title": title})
            [banana] = [ref for ref, row in provider.data["note"].items()
                        if row["keys"]["title"] == "Banana"]
            await executor.invoke(agents["note"], "notes_save", {
                "note_ref": banana, "notebook": "work", "title": "Banana",
                "content": "changed last"})
            return (await titles(), await titles("title"),
                    await titles("created"), await titles("updated"))

        as_stored, by_title, by_created, by_updated = run(scenario())
        assert as_stored == ["Banana", "Cherry", "Apple"]
        assert by_title == ["Apple", "Banana", "Cherry"]
        assert by_created == ["Apple", "Cherry", "Banana"]
        assert by_updated == ["Apple", "Cherry", "Banana"]

    def test_find_shows_its_matches_as_a_table(self, agents):
        executor = Chat(Records())

        async def scenario():
            await executor.invoke(agents["note"], "notes_save", {
                "notebook": "work", "title": "Handoff"})
            return await executor.invoke(agents["note"], "notes_find", {"notebook": "work"})

        found, status = run(scenario())
        assert status == "success", found
        [said] = executor.said
        assert said["table"]["title"] == "Notes"
        assert said["table"]["columns"] == ["title", "notebook", "priority"]
        assert said["table"]["rows"][0][0] == "Handoff"

    def test_an_import_reports_what_came_in_in_its_own_name(self, agents):
        provider = Records()
        executor = Chat(provider)

        async def scenario():
            uploaded = provider.create_file(
                "document", "two.csv", "title,priority\nOne,1\nTwo,2\n")
            return await executor.invoke(
                agents["note"], "archive_import",
                {"notebook": "copy", "file_ref": uploaded}, chat_level=2)

        imported, status = run(scenario())
        assert status == "success", imported
        [said] = executor.said
        assert said["text"] == "Imported 2 note(s) into 'copy'."
        assert said["table"]["columns"] == ["title", "priority"] and len(said["table"]["rows"]) == 2

    def test_an_export_is_handed_to_the_person_as_a_file(self, agents):
        """The file goes back with the result; the runtime keeps it as
        the chat's file, and the output names it by its ref."""
        executor = Chat(Records())

        async def scenario():
            await executor.invoke(agents["note"], "notes_save", {"notebook": "work", "title": "Handoff"})
            return await executor.invoke(agents["note"], "archive_export",
                                         {"notebook": "work", "format": "csv"}, chat_level=2)

        exported, status = run(scenario())
        assert status == "success", exported
        file = executor.files[exported["file_ref"]]
        assert file["name"] == "work-notes.csv" and file["content"].startswith(b"title,notebook")


class TestWhereItConnects:
    def test_it_says_it_connects_to_nothing(self):
        """A manifest must say where its agent connects. Note connects
        to nothing, and says so."""
        assert manifest()["network"] == {"hosts": []}
