# Note — the example agent

A meeting ends and three decisions were made. Say so, and they are saved
in the "meetings" notebook; next month, ask what was decided about the
office move and the note comes back. That is the whole domain — chosen to
be small enough that the *platform's rules* are the only thing to notice.

This agent exists to use **every feature the agents standard enforces**
(the platform's `documentation/agents.md`). Read `manifest.yaml` top to
bottom to learn what an agent may declare; read the tools to see how
little code each feature needs.

## What each function is here to demonstrate

| Function | Level | The feature |
|---|---|---|
| `notes_save` | 1 | A contained write: `record.create`/`record.update` on a data resource, and a scope (`notebook`) a policy can narrow. Uses `titlecase` — a dependency pinned in `requirements.txt`, carried inside the program. **`call.ask`** (`chat`): when a note with the same title is already there, it asks whether to update it or keep both — before any write, and keeping both when nobody can answer. |
| `notes_get` | 0 | A read by reference; optional output fields travel only when present, because the output schema is enforced. |
| `notes_find` | 0 | **`schedulable`**: a schedule may run it unattended, with no model and no approval. Uses `humanize`. **`call.say`** (`chat`): shows its matches as a table. Reads the person's one `settings` row and lists in the order it says. |
| `notes_summarize` | 1 | **`model`**: the one way agent code thinks — `call.model` reaches the chat's model through the runtime; the stored key never enters the agent's process. |
| `archive_export` | 2 | Files out: the export is handed to the person as the call's file (`call.give`); the runtime keeps it as the chat's file. |
| `archive_import` | 2 | Files in: a file handed to the call, or a document the person keeps in the `document` resource (`user_access: [create]`, the samples' file), validated whole before any note is written. Reports what came in, in its own name, with the imported rows as a table. |
| `sync_status` | 0 | Secrets: lists and reads the connection — `keys` readable, `values` encrypted — and asks nobody: with none kept it says so. |
| `sync_push` | 3 | External and irreversible: above the chat's trust a person approves first. With no connection kept, the person is asked to fill it in on the resource's declared form. |

Also on show: two pinned pip dependencies carried inside the program,
the hosts it connects to (`network`, which for Note is none), JSON Schema
on every input and output, sample data a person can load in one step, and
the agent's own `instructions`.

[`tests/test_note.py`](../../tests/test_note.py) runs every one of these
functions in its program, over the standard's messages.

## Why the agent is `note` and the tool is `notes`

A function's id is its tool's id and its method's name: `notes_save`.
Had the tool been called `note` too, the agent would read `note` and
`note_save` side by side — names people misremember. Names that stutter
are worth one minute of thought at the start.

## The one thing to get right before anything else

`content` is a **string**, stored in **`keys`**. Both halves of that are
deliberate:

- `keys` are plaintext, filterable and **scalar**. The body is kept
  where a filter can reach it, and so it cannot be an object.
- `values` are encrypted at rest and may hold objects, but cannot be
  filtered on, and `record.list` never returns them. A notebook of
  private notes could keep the body there and give up searching it.

Meanwhile the sync connection keeps its `api_token` in `values` and its
`base_url` in `keys` — exactly right, because a token is something you
must never show again and a hostname is how a person tells two
connections apart.

## Order

The store lists records in the order they were written, and keeps no
time of a change: a `settings` order of `created` or `updated` both list
the newest written first.

## The remote is simulated

`sync_*` use a real secret but never make a network request: the digest
`sync_push` returns is computed from the notes. That keeps the example
safe and deterministic while still exercising the whole secret path —
installing it asks a deployment to trust nothing outside itself.

## Copy it

1. Copy this directory; give the copy a lowercase, stable id, the same
   as the directory's name and `agent.id`.
2. Rename `NoteAgent`: the program finds the one agent class in
   `agents/<id>/agent.py`.
3. Keep only the resources your agent genuinely needs, the narrowest
   `calls` and level for every function.
4. Write `instructions` for the model: which function to start with,
   what never to invent, what to ask.
5. Copy the tests beside it, make them about your agent, and run them.
6. Build it (`publish/build.sh`), install it, use every function once,
   and run the standard's checklist before publishing.
