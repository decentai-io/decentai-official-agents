# Google Drive (`google_drive`) 2.0.0

"Find the Riverside lease in our shared drive and bring me a PDF", "change the delivery paragraph of the Q3 proposal",
"add today's order to the Orders sheet", "how did people answer the feedback form?", "tell me every morning about new
sign-ups": one agent for a person's Google Drive, shared drives included, and the Docs, Sheets and Forms in it.

2.0.0 absorbs the Google Docs, Google Sheets and Google Forms agents (1.0.0), which are retired. It rides the same Google
connection as Gmail and Google Calendar: connect once, grant the saved account to each. Several accounts may be
connected; every function takes `account` (the address, or a part of it that names one account).

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | Connected accounts; the address, organization (Google's `hd`) or personal, storage used |
| `account_connect` | 1 | Connect another Google account |
| `files_search` | 0 | Name or content, across My Drive and every shared drive (`corpora=allDrives`), by kind; paged |
| `files_list` | 0 | A folder by id (any drive) or path, folders first; paged |
| `files_shared_with_me` | 0 | Shared with the account, newest first, with who shared; paged |
| `files_drives` | 0 | The shared drives, with ids usable as folders |
| `files_get` | 0 | One item |
| `files_versions` | 0 | Earlier versions (revisions), newest first |
| `files_download` | 0 | A copy into the chat: a Google file exported (docx, xlsx, pptx, pdf, txt) or one tab as CSV with a BOM; refused before download over the chat's file limit, with the link |
| `files_upload` | 2 | A chat file into a folder: multipart up to 5 MB, a resumable session above; a free name, replace or fail; `convert` to a Google file |
| `files_create_folder`, `files_move`, `files_copy`, `files_trash`, `files_restore` | 2 | Filing; trash, never delete for good |
| `sharing_permissions` | 0 | Who can open it, each permission's id, inherited access marked |
| `sharing_invite` | 3 | People by address, read/comment/write; Google emails them unless `notify: false` |
| `sharing_link` | 3 | Within the account's organization (its hosted domain), or anyone when asked |
| `sharing_revoke` | 3 | One permission; never the owner's or inherited access |
| `changes_watch` / `changes_new` | 1 / 1, schedulable | Drive's change log from a start page token: all drives, one shared drive, or one folder |
| `docs_read` | 0 | Numbered paragraphs across every tab, with headers, footers and footnotes |
| `docs_create`, `docs_insert` | 2 | A new document; paragraphs added at the end or after one, refused if the doc changed since it was read |
| `edits_propose` / `edits_apply` / `edits_discard` | 1 / 2 / 1 | One paragraph's change as a record, then applied with `requiredRevisionId` |
| `comments_list` | 0 | A file's comments (open by default), paged without empty pages |
| `comments_add`, `comments_reply`, `comments_resolve` | 3 | Through the Drive comments API |
| `comments_watch` / `comments_new` | 1 / 1, schedulable | Others' new comments and replies |
| `sheets_get` | 0 | Tabs and grid sizes, locale, time zone |
| `sheets_read` | 0 | A window of rows; the header row named, read every time |
| `sheets_append`, `sheets_update` | 2 | RAW unless `formulas`; an overwrite keeps what was there |
| `sheets_create`, `sheets_add_tab` | 2 | A spreadsheet or a tab, with a header row |
| `sheets_find` | 0 | The cells holding a value |
| `sheets_totals` | 0 | Sum, count, min, max, average, by group: counted in code, in exact decimals |
| `rows_watch` / `rows_new` | 1 / 1, schedulable | New rows, seen even when a row is deleted and another added |
| `forms_get` | 0 | Questions, kinds, options |
| `responses_list` | 0 | Responses, paged with Google's own token |
| `responses_tally` | 0 | Per question: answered, each option's count, a scale's average; in code |
| `responses_watch` / `responses_new` | 1 / 1, schedulable | New responses |
| `watches_list`, `watches_close` | 0, 1 | Every watch of the four kinds |

Records: `share` (grants and revocations), `edit` (proposed document changes; their text encrypted in values),
`write` (sheet writes, with what an overwrite replaced), `watch` (one per watch, its cursor in keys).

## Rules it keeps

- **Shared drives everywhere.** `supportsAllDrives` on every request that names a file; listings say
  `includeItemsFromAllDrives` and the right `corpora`.
- **Busy Google is waited out.** 429 and 403 `rateLimitExceeded`/`userRateLimitExceeded` are tried again after
  `Retry-After` (the shared `standard.google`); a missing permission says to reconnect.
- **The chat's file limit.** 8 MiB less 64 KiB. A file Drive says is bigger is refused before a byte is read; an export
  (which states no size) is cut off while it streams. Google exports at most 10 MB.
- **Uploads the way Google asks:** multipart up to 5 MB, resumable above.
- **A link stays in the organization** named by the account's hosted domain, never guessed from the address. A
  personal account has none and is told so; "anyone with the link" is only on request.
- **Documents:** read with `includeTabsContent=true`; edits and inserts sent with `requiredRevisionId`, so a document
  changed in between is refused by Google, never overwritten. A paragraph holding an image, a chip or a footnote mark
  is not rewritten.
- **Sheets:** a read is a window, never the whole tab; the header row is the one named, never a continued read's first
  row. Writes are `RAW` unless the person asks for formulas, so "=IMPORTDATA(…)" pasted from an email stays text. Sums
  and counts are made in code.
- **Forms:** the tally is counted in code; pages are Google's own.
- **Watches** (`standard.watch`): each read alone; one that fails keeps its cursor and is named in `problems`; a gone
  file, tab, form or shared drive closes its watch; cursors are written only once every watch was read.

## Limits, and what only a live account can confirm

Verified against a loopback stub (`tests/gdrive_stub.py`) as strict as Google's documented rules; the first real
connection is the first live run. Not yet seen live: whether `alt=media` downloads redirect (to
`*.googleusercontent.com`, declared in case); the exact error for an export over 10 MB; that the Forms `filter`
compares `lastSubmittedTime`; that `changes.list` without `driveId` reports shared-drive changes with
`includeItemsFromAllDrives`; that `oauth2/v3/userinfo` gives `hd` with the `openid email` scopes; the exact status for a
stale `requiredRevisionId` (handled as a 400 naming the revision, or `FAILED_PRECONDITION`).

The `drive`, `documents` and `spreadsheets` scopes are restricted or sensitive Google scopes: a public app needs Google's
verification before people outside its test users can connect.
