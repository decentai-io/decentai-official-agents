# Google Sheets (`google_sheets`)

"Who signed up for the workshop since yesterday?" — answered from the
spreadsheet itself, not a copy: the rows added since the last look,
each keyed by its column name. Ask what is on the Orders tab and the
rows come back; ask to add today's order or correct a quantity, and the
change waits for your go-ahead, with a record of what was written and
what the cells held before.

Not the `sheets` agent, which works on a CSV or Excel FILE handed to the
platform. This one works in a live Google spreadsheet other people are
working in too, over the Sheets API (v4). It rides the same Google
connection as the other Google agents: connect once, grant the one
saved account to each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | The connection, and which address it is |
| `spreadsheets_find` | 0 | Spreadsheets by name, most recently changed first, with links |
| `spreadsheets_get` | 0 | Title, link, and every tab with its grid size |
| `values_read` | 0 | A tab or a range (`cells`, such as `A1:D20`) as rows, the header apart (`header: false` for none); cells over 500 characters clipped, at most `max_rows` (50 by default, 100 at most) and about 200 cells shown |
| `values_append` | 3 | Rows added after the tab's table; nothing below it is overwritten |
| `values_update` | 3 | Cells overwritten from a start cell; what they held is read first and kept |
| `rows_watch` | 1 | Starts watching a tab for new rows, from now; `header_row` names the header (1 by default, 0 for none) |
| `rows_new` | 1 | The rows added since the last check; schedulable |

Records:

- `write` — every append and overwrite: the spreadsheet, tab, range,
  rows and cells, what was written, and for an overwrite what was there
  before (in the encrypted half, clipped to what a record can hold).
- `watch` — a rows watch: the spreadsheet, the tab (by title and by tab
  id), the header row, and the cursor — how many rows have been handed
  on.

## The watch

This is how form responses, sign-ups and orders typed in by someone
else reach the assistant.

1. `rows_watch` records the tab and counts the rows already there. They
   are not news.
2. A schedule runs `rows_new`. Each check reads
   only the rows after the cursor (plus the header row, re-read every
   time so a new column gets its name), hands them on as
   `{row, cells: {column name: value}}` naming their watch, and moves
   the cursor past them. A check that finds nothing returns an empty
   `rows`.
3. At most `max_results` rows per watch per check; one more is read, so
   `more: true` means a later check continues — nothing is skipped.

**What a row count cannot see.** It sees rows added at the end of a tab.
It does not see a cell edited in the middle, and it cannot tell rows
deleted from rows moved. When a tab has *fewer* rows than the cursor,
`rows_new` hands on nothing for that watch, moves the cursor to the rows
there are now, and says so in `reset` with a sentence; it never guesses
which rows are new. A tab that was deleted, or a spreadsheet the account
can no longer open, closes its watch and is reported in `reset` too. A
renamed tab is still found by its tab id.

## Rules it keeps

- **Nothing in a sheet changes without a yes.** Append and overwrite are
  level 3: the sheet is shared, and a change there is seen at once.
- **An overwrite keeps what it replaced.** The cells are read before the
  write, returned as `previous`, and kept on the record.
- **An append never lands on data.** `INSERT_ROWS`, not Google's default
  `OVERWRITE`.
- **A write that gets no answer is `kind: unknown` and never retried** —
  it may have landed; look before writing again.
- **Tabs are named, ranges are cells.** An unknown tab is refused with
  the tab names there are; an update past the tab's grid is refused and
  points at append.
- Cells are sent as a person would type them (`USER_ENTERED`), so
  "1840.00", "2026-09-14" and "=SUM(D2:D20)" become a number, a date and
  a formula. Reads return the formatted text a person sees.

## Setup

Once per organization, by an administrator, in the Google Cloud console:

1. Enable the **Google Sheets API** and the **Google Drive API** (Drive
   is how spreadsheets are found and which address is connected).
2. OAuth consent screen: add the scopes every Google agent in this
   catalog requests — `openid`, `userinfo.email`, `gmail.modify`,
   `calendar`, `drive`, `spreadsheets`, `documents`,
   `forms.body.readonly`, `forms.responses.readonly`, `tasks` — and
   publish it (a consent screen left in Testing expires refresh tokens
   after seven days).
3. Credentials → OAuth client ID → **Web application**, with the
   redirect URI DecentAI shows under Settings → Connected apps; paste
   the client id and secret into DecentAI under **Settings → Connected
   apps** as provider `google`.

The [Gmail agent's README](../gmail/README.md#setup) gives the same
steps in full, including the consent screen's test users.

Then, per Google account: **Agents → Google Sheets → Credentials →
Connect account**, and grant the saved account to the other Google
agents as well. An account connected before the scope list grew must be
reconnected once to grant the new scopes. The `api_base_url` field
exists so the tests can point the agent at a loopback stub; leave it
empty.

## Limits, and what is not verified

- Live Google behaviour is **not yet verified**: the tests run the agent
  as its own program against a loopback stub of the Sheets and Drive
  subset it uses. The first real connection is the first live run.
- Not checked against the live API: that `values:batchGet` takes the
  repeated `ranges` parameter exactly as sent; where Google decides a
  tab's "table" ends for an append (a fully blank row may end it early,
  so new rows could land below the first block rather than the last);
  and that a read clamped to the grid is always accepted.
- `rows_watch`, and a check that finds the last known row empty, count
  the tab by reading all of it — fine for thousands of rows, slow for a
  very large tab.
- `spreadsheets_get` reports grid sizes, not how many rows hold data.
- Up to 25 rows and 40 cells a row per write; a record keeps what it
  wrote up to about 7,000 characters, then says it was clipped.
- The `drive` and `spreadsheets` scopes are restricted or sensitive
  Google scopes: a public app needs Google's verification before people
  outside test users can connect.
