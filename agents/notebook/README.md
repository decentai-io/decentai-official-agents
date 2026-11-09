# Notebook — the reference agent

A meeting ends and three decisions were made. Say so, and they are saved
in the "meetings" notebook; next month, ask what was decided about the
office move and the note comes back, with the date. Notebook is also the
reference agent: it exercises every feature DecentAI can enforce on a
domain small enough that the rules are the only thing to notice. Read
`manifest.yaml` top to bottom to learn what an agent can declare; read
the tools to see how little code each feature needs.

[Note](../note/README.md) is the same agent in a later form, and the
one the catalog names as the example to copy: it keeps a note's
`content` as a string in `keys`, where Notebook keeps it as an object
in `values` (encrypted, not filterable), and it also shows `call.say`
and `call.ask`.

The platform ships with no agents. Installing this one — from this
repository's catalog, the release an administrator reviewed — is how a
deployment gets its first.

## What each function is here to demonstrate

| Function | Level | The feature |
|---|---|---|
| `note_save` | 1 | A contained write: `record.create`/`record.update` on a data resource, and a scope (`notebook`) a policy can narrow. Uses `titlecase` — a dependency pinned in `requirements.txt`, carried inside the program. |
| `note_get` | 0 | A read by reference; optional output fields travel only when present, because the output schema is enforced. |
| `note_find` | 0 | **`schedulable`**: a schedule may run it unattended, with no model and no approval. Uses `humanize`. |
| `note_summarize` | 1 | **`model`**: the one way agent code thinks — `call.model` reaches the chat's model through the runtime; the stored key never enters the agent's process. A listing carries `keys` only, so it reads each note for its content. |
| `archive_export` | 2 | Files out: the notes as a CSV or JSON file, handed back with the result (`call.give`). The content, kept in `values`, is not in a listing and so not exported. |
| `archive_import` | 2 | Files in: a file handed to the call, or a document a person uploaded themselves (`user_access: [create]`), validated whole before any note is written. |
| `sync_status` | 0 | Secrets: lists and reads a kept connection — `keys` readable, `values` encrypted; it asks nobody, and says "unbound" when none is kept. |
| `sync_push` | 3 | External and irreversible: in an ordinary chat this asks the person for approval. With no connection kept, the person is asked to fill one in. |

Also on show: two real pip dependencies (pinned in `requirements.txt`, and carried inside
the program), JSON Schema on every
input and output, and the agent's own `instructions` — which the model
reads only after opening the agent. `samples.yaml` loads sample data:
two meetings in a "meetings" notebook, a supplier note, a `settings`
row, and the minutes of one meeting as a document
(`samples/ops-meeting-2026-09-07.md`).

## The remote is simulated

`sync_*` consume a real kept secret but never make a network request:
the digest `sync_push` returns is computed from the notes alone. This
keeps the reference safe and deterministic while still exercising the
whole secret path.

## Copy it to create an agent

1. Copy this directory; give the copy a lowercase, stable id.
2. Make the directory name, `agent.id` and the catalog entry that same id.
3. Rename `NotebookAgent`: the program finds the one agent class in `agent.py`.
4. Keep only the resources your agent genuinely needs, and the narrowest
   operations and permission level for every function.
5. Write `instructions` for the model: when to use the tools, what never
   to assume.
6. Add tests (see [`../tests/`](../../tests/); `test_agents.py` runs
   Notebook's) and run them before publishing.
