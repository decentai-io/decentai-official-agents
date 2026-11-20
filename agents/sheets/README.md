# Spreadsheets (`sheets`)

A branch sends its monthly sales as a CSV saved by Arabic Windows Excel: two title lines above the header,
semicolons between the fields, and amounts written `12.450,50`. Ask for the totals by branch and month and the
answer is computed here, in code, with every amount read the way its column writes it — not a hundred times too
large, and not guessed when the column doesn't say.

Reads CSV, Excel (`.xlsx`), JSON and JSON Lines files; finds duplicates, gaps and spelling variants; reconciles two
lists by the rules you state; totals, pivots, filters and sorts into new files; checks JSON against a schema; and
turns JSON into a table and back. Version 2.0.0 absorbs the JSON agent (`json_data`).

No external service and no credential. Files come in as platform files — a person's upload in the chat, or a file
another agent saved — by `file_ref`. Produced files go back as the call's files; the original is never modified.

## Supported formats

| Kind | Read | Produced |
|---|---|---|
| CSV, TSV | yes: encoding found (UTF-8 with or without its mark, UTF-16, cp1256 for Arabic, cp1252), delimiter found (`,` `;` tab `\|`) | yes: UTF-8 with a byte-order mark, so Excel shows Arabic |
| Excel `.xlsx` | yes: every sheet, merged cells, formulas with their computed values | yes |
| JSON, JSON Lines | yes | yes (`json_filter`, `json_from_table`) |
| Legacy `.xls`, `.ods`, Numbers | no — reported as unsupported | — |

## Functions

| Function | Level | What it does |
|---|---|---|
| `read_inspect` | 0 | Sheets, the header row found, rows, merged ranges; per column its type, blanks, formulas, leading zeros, decimal mark and currencies; a CSV's encoding and delimiter |
| `read_rows` | 0 | Rows as text, paged (`from_row`, `next_row`); `formulas=true` shows formulas |
| `check_quality` | 1 | Duplicates by key, blank required cells, spelling variants. Recorded |
| `reconcile_match` | 1 | Two lists matched by key rules; near matches uncertain, never merged; a workbook with a change log |
| `clean_dedupe` | 1 | A workbook without duplicates, the removed rows listed |
| `clean_convert` | 1 | Numbers stored as text made numbers ("prices as numbers"); what wasn't converted listed with why |
| `combine_files` | 1 | Several files into one workbook with a summary and totals |
| `calc_aggregate` | 1 | Sum, count, avg, min, max by group; at most 500 groups |
| `calc_pivot` | 1 | Group × column sums (or counts) with row, column and grand totals; optionally a workbook |
| `arrange_rows` | 1 | Filter and sort into a new xlsx or csv |
| `json_inspect` | 0 | Valid or not, JSON or JSON Lines, records, and the key map |
| `json_select` | 0 | Values at a path |
| `json_validate` | 1 | A JSON Schema check, formats included, per record. Recorded |
| `json_compare` | 0 | What changed, path by path |
| `json_filter` | 1 | Records kept by conditions, into a JSON file |
| `json_to_table` | 1 | JSON to CSV, nested keys as dotted columns |
| `json_from_table` | 1 | CSV or a sheet to JSON, dotted columns nested again |

A function that produces a file also writes a `job` record (what it read, what it wrote, its summary), so it is
level 1; nothing here acts outside the platform. None is schedulable: each works on a file someone hands it.

## Numbers, the way the column writes them

- **The decimal mark is read per column.** `1.234,56` and `12,50` say a decimal comma; `1,234.56` and `12.50` a
  decimal point; the Arabic decimal mark `٫` and thousands mark `٬` say themselves; Arabic-Indic digits are digits.
- **An ambiguous column is refused, not guessed.** A column of nothing but `1,250` and `2,500` could be thousands or
  decimals: the agent names the values and asks for `decimal` (`.` or `,`). A column writing both marks is refused
  the same way.
- **A currency beside an amount** (`AED 1,250.00`, `1.250,00 د.إ`) is noted; a column of two currencies is never
  totalled. `json_from_table` keeps such an amount as text.
- **Identifiers stay text:** a leading zero (`00412`), a leading `+` (`+971501234567`), or more than fifteen digits.
- All arithmetic is in exact decimals.

## Honest about the file

- **Nothing is cut silently.** A sheet past 100,000 rows of data, or a file past 1,500,000 cells, is refused with its
  count. A file larger than a chat file may be (8 MiB less 64 KiB) is refused with its size before it is read.
- **The header is found** under titles, a report date or blank rows, or named with `header_row`; row numbers are the
  sheet's own.
- **Merged cells** carry their value over the whole range, so a merged region label is not read as blanks.
- **Formulas are never moved as broken text.** A formula copied to another sheet or file points at other cells, so a
  produced workbook holds the value the source computed, with a note on the cell giving the formula and where it
  stood; a formula the file never computed is left empty and said. A text cell that starts with `=` is told apart
  from a formula by its type, and stays text.
- **A blank key matches nothing**, in duplicates and in reconciliation: two rows with no email are not one person.
- **Reconciliation is by hash**, near-linear: two lists of tens of thousands of rows take seconds.
- **JSON is JSON:** `1` is not `true`, `"1"` is not `1`, `NaN` and `Infinity` are refused where they stand and never
  written; a JSON Lines line that doesn't parse is named by its number.
- Parsing and writing run off the agent's loop, so one large file doesn't stall the chat's other calls.
- A produced sheet whose words are mostly Arabic is set right to left.

## The JSON path language

`$` the whole document; `a.b` a nested key; `a[0]` one item (`a[-1]` the last); `a[*]` every item; `*` every value of
an object. There is no arithmetic in a path; `json_filter` conditions compare in code.

## Sample data

Sidra Office Supplies (fictional): a supplier price list with a duplicated row, a missing price and a price that is
not a number, and the product catalog to reconcile it with; an Arabic branch sales report as Gulf Excel saves it
(titles above the header, semicolons, a decimal comma); and an order export with gaps and the schema it should meet.
All in `samples/` (`samples.yaml`).

## Limits, and what is not verified

- `.xlsx` only; `.xls` and `.ods` are refused as unsupported.
- A formula's value is the one the file last computed; the agent doesn't recalculate.
- Delimiter and single-byte encoding are inferred from the text (cp1256 when Arabic words dominate, else cp1252);
  a CSV that is neither is read as cp1252, and the encoding found is always said.
- Produced workbooks are verified by reading them back with the library that wrote them; rendered appearance is not
  inspected.
- The tests run the agent as its own program, against fixtures written out in full in the test file.
