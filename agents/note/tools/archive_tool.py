"""Files: an export handed to the person as the call's file, and an
import read from a file they handed in or keep — reported in the
agent's own name (call.say)."""

import csv
import io
import json
import re

from standard import ResourceDenied, ToolBase

COLUMNS = ["title", "notebook", "priority", "content"]
MAX_IMPORT_ROWS = 1000
#: The longest text the store keeps in one field.
MAX_TEXT_CHARS = 8192


class ArchiveTool(ToolBase):
    id = "archive"

    async def export(self, call):
        notebook = str(call.inputs["notebook"]).lower()
        fmt = call.inputs["format"]

        records = await call.records.list("note", {"notebook": notebook})
        rows = [{column: record["fields"].get(column) for column in COLUMNS} for record in records]

        await call.progress(f"Exporting {len(rows)} note(s) as {fmt}")

        if fmt == "json":
            content = json.dumps(rows, ensure_ascii=False, indent=2)
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
            writer.writeheader()
            # The content goes in RAW. csv quotes and escapes what needs
            # it — commas, quotes, the newlines in a note body — and the
            # reader gives back exactly what was written. Encoding it
            # again (as JSON, say) would survive the file and arrive at
            # import as a quoted literal: a note that went out and came
            # back would gain a pair of quotes and lose its line breaks.
            # A round trip has to be lossless: a note that goes out and
            # comes back is the same note.
            writer.writerows([
                {**row, "content": row.get("content") or ""}
                for row in rows
            ])
            content = buffer.getvalue()

        safe_notebook = re.sub(r"[^a-z0-9._-]+", "-", notebook).strip("-._")
        filename = f"{safe_notebook or 'notebook'}-notes.{fmt}"
        return {
            "file_ref": call.give(filename, content),
            "filename": filename,
            "note_count": len(rows),
        }, "success"

    async def import_(self, call):
        # Function id "archive_import": "import" is a Python keyword, so
        # the method is spelled with a trailing underscore.
        notebook = str(call.inputs["notebook"]).lower()
        file_ref = call.inputs["file_ref"]

        try:
            content = (await call.file(file_ref, "document")).read_text(encoding="utf-8")
        except (ResourceDenied, UnicodeDecodeError):
            return {"error": f"Unknown file_ref '{file_ref}'"}, "error"
        stripped = content.lstrip()
        try:
            if stripped.startswith(("[", "{")):
                rows = json.loads(content)
            else:
                reader = csv.DictReader(io.StringIO(content))
                rows = list(reader)
                if "title" not in (reader.fieldnames or []):
                    return {
                        "error": "The CSV notebook document needs a title column."
                    }, "error"
        except (csv.Error, json.JSONDecodeError) as exc:
            return {"error": f"The notebook document is invalid: {exc}"}, "error"

        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return {"error": "The notebook document must contain a list of notes."}, "error"
        if len(rows) > MAX_IMPORT_ROWS:
            return {
                "error": f"The notebook document contains more than {MAX_IMPORT_ROWS} notes."
            }, "error"

        await call.progress(f"Importing {len(rows)} note(s) into '{notebook}'")

        parsed_notes = []
        for row in rows:
            fields = {
                "notebook": notebook,
                "title": str(row.get("title") or "Untitled"),
            }
            if len(fields["title"]) > MAX_TEXT_CHARS:
                return {"error": f"A title is longer than {MAX_TEXT_CHARS} "
                                 f"characters: '{fields['title'][:60]}…'."}, "error"
            priority = row.get("priority")
            if priority not in (None, ""):
                try:
                    priority = int(priority)
                except (TypeError, ValueError):
                    return {"error": f"Invalid priority for '{fields['title']}'."}, "error"
                if not 1 <= priority <= 5:
                    return {"error": f"Invalid priority for '{fields['title']}'."}, "error"
                fields["priority"] = priority

            # Content is TEXT, the same contract notes_save enforces:
            # keys are scalar, so an object could not be stored, and
            # `values` could not be read back to export in the first place.
            note_content = row.get("content")
            if note_content not in (None, ""):
                if not isinstance(note_content, str):
                    return {"error": f"Invalid content for '{fields['title']}'."}, "error"
                if len(note_content) > MAX_TEXT_CHARS:
                    return {"error": f"The content of '{fields['title']}' is "
                                     f"longer than {MAX_TEXT_CHARS} characters."}, "error"
                fields["content"] = note_content

            parsed_notes.append(fields)

        # Validate the complete document before writing anything. A bad row
        # must not leave the user with a half-imported notebook.
        note_refs = []
        for fields in parsed_notes:
            note_refs.append(await call.records.create("note", fields))

        if note_refs:
            # The agent's own report, now, in its own name: what came in,
            # with the rows to look at. A table holds at most 500 rows.
            await call.say(f"Imported {len(note_refs)} note(s) into '{notebook}'.", table={
                "title": f"Imported into '{notebook}'", "columns": ["title", "priority"],
                "rows": [{"title": fields["title"], "priority": fields.get("priority")}
                         for fields in parsed_notes[:500]]})
        return {"note_refs": note_refs, "imported": len(note_refs)}, "success"
