# JSON (`json_data`)

An export arrives from a system nobody documents. Ask what is in it and
the answer is the key map: every path the records actually use, how many
records have it, and which are missing it. Ask whether it matches the
schema the other side promised, and the failures come back with their
paths.

Reads uploaded JSON and JSON Lines files, answers by path, validates
against a JSON Schema, compares two versions, and converts between JSON
and CSV so a spreadsheet — or the Spreadsheets agent — can take over.

No external service and no credential. Files come in as platform
files — a person's upload in the chat, or something another agent
saved — by `file_ref`.

## Supported formats

| Kind | Read | Produced |
|---|---|---|
| JSON | yes | yes (`shape_to_json`) |
| JSON Lines (one value per line) | yes, recognised without being told | — |
| CSV | as the input to `shape_to_json` | yes (`shape_to_table`) |
| Anything else | no — reported with the line and column where parsing stopped | — |

## Functions

| Function | Level | What it does |
|---|---|---|
| `read_inspect` | 0 | Valid or not, JSON or JSON Lines, top-level type, record count, nesting depth, and the key map — every path, how many records have it, the types seen, one example; the key map is built from the first `max_records` (5,000 by default) |
| `read_select` | 0 | The values at a path, each with the exact path it was found at; a large value is cut and marked |
| `check_validate` | 1 | The file against a JSON Schema, inline or attached. Failures carry the path, the rule and the message. `each_record` checks the records one by one. Recorded |
| `compare_versions` | 0 | What changed between two files, path by path: added, removed, changed, with before and after |
| `shape_to_table` | 2 | A CSV from the records (or those at `record_path`), nested values as dotted columns; `columns` picks and orders them |
| `shape_to_json` | 2 | A JSON file from a CSV, one object per row |

Records: `validation` (what was checked and what failed) and `output`
(every file produced, by name, and what it was made from). Files:
`source` (inputs and schemas), or a file handed to the call; a produced
file goes back as the call's file.

## The path language, in full

There is no filtering and no arithmetic. A path names places; the
caller does the thinking.

| Written | Means |
|---|---|
| `$` | the whole document |
| `a.b` | key `b` inside key `a` |
| `a[0]` | one item, counting from zero; `a[-1]` is the last |
| `a[*]` | every item of the array |
| `*` | every value of an object, or every item of an array |

`read_select` counts up to a thousand matches; past that it returns
the first of them and says the count was capped.

## Two rules that keep a round trip honest

`shape_to_table` flattens `customer.city` into a column of that name
and `shape_to_json` expands it back into an object, so a file can go
out to a spreadsheet and come back nested.

An identifier stays text. A value with a leading zero (`007`) or more
than fifteen digits is a reference number, not a quantity: turning it
into a number loses it, so `infer_types` leaves it alone. Numbers,
`true`, `false` and empty cells are converted.

## Sample data

Sidra Office Supplies: an export of four orders with the faults the
example prompts ask about (one order has no total, no customer city and
no lines) and the order schema to check it against. Both are JSON
files in `samples/` (`samples.yaml`).

## Limits, and what is not verified

- The document is parsed whole, so a file must fit in memory: 12 MB is
  the ceiling, under the platform's own upload limit.
- A file that is neither JSON nor JSON Lines is reported, never
  half-read. A CSV is told apart from broken JSON and named as a CSV.
- Comparison walks paths and compares scalars. Arrays are compared by
  position, so one item inserted at the front reads as many changes —
  that is arithmetic, not a judgement about what happened.
- `check_validate` reports the validator's verdict and nothing else. A
  schema that is itself invalid is refused as the schema's fault.
- The key map collapses array indexes (`lines[].qty`) and stops at 300
  paths and 24 levels deep; `read_inspect` says when it was capped.
- The tests run the agent as its own program, against fixtures written
  out in full in the test file.
