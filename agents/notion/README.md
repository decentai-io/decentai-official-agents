# Notion (`notion`)

"Which office move tasks are still in progress, who has them, and what does the launch plan still have open?"
Pages are found by title, read as text, and queried with filters. You can add a task, assign it, tick a status,
append a checklist, trash a page or leave a comment. Each change waits for your go-ahead, is checked against the
database's own schema first, and is kept on record.

The agent works on one or more Notion workspaces through the Notion API. It sees only what the person shared with the
integration while signing in. A page it cannot open is a page to share, and its errors say so.

## Functions

Every function takes an optional `account` (the workspace's address) when several workspaces are connected.

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | The connection, the workspace's name and the integration's. Asks nobody |
| `search_find` | 0 | Pages and databases (their data sources) by title, newest edited first; paged |
| `pages_read` | 0 | A page's properties as text, and its body as one line per block, a window at a time |
| `databases_get` | 0 | A database's schema: property names, types, options. For a database of several data sources, lists them |
| `databases_query` | 0 | Rows with properties as text; one simple filter or a compound `filter` (all/any, nested once), a sort; paged |
| `pages_create` | 2 | A row, validated against its data source's schema, or a page under a page; optional paragraphs |
| `pages_update` | 2 | Changes a row's properties (or a page's title); records what each was before |
| `pages_archive` | 2 | Moves a page or row to Notion's trash, or restores it with `restore` |
| `blocks_append` | 2 | Adds paragraphs, to-dos and bullets to the end of a page |
| `comments_list` | 0 | A page's comments |
| `comments_add` | 3 | A page-level comment, shown as written by the integration and seen by others at once |
| `watch_database` | 1 | Records a watch on a database, from now in Notion's clock |
| `watch_changes` | 1, schedulable | Rows created or edited since the last check, for every watch; each watch read on its own |
| `watches_list` | 0 | The watches, with what each follows and how far it was read |
| `watches_close` | 1 | Stops a watch for good |

Records: `write` holds every page, row, property change, trash, block append and comment this agent made, with the
previous values of an update. `watch` holds the databases followed and how far each was read.

## Notion's API version, and data sources

Every request carries `Notion-Version: 2026-03-11`. Since 2025-09-03 a database is a container of one or more data
sources. The schema and the rows belong to a data source (`data_sources/{id}`, `data_sources/{id}/query`), and a new
row's parent is `{type: data_source_id}`. `database_id` takes either id: a data source's (what a search returns) or a
database's (what a link copied from Notion holds). A database with one data source is that source. One with several is
refused with each source named, so the person can say which. 2026-03-11 also renamed the trash flag to `in_trash`;
`archived` is no longer sent.

## Rules it keeps

- **Nothing in the workspace changes without a yes.** Writes the person can undo (create, update, trash, append) are
  level 2. A comment, which others see at once, is level 3.
- **The schema decides what a row may hold.** A property that does not exist, or an option that is not defined, is
  refused with the valid ones named. Notion would silently add a new select option; this agent does not. Computed
  properties (formulas, rollups, created time, unique ids) and files are refused.
- **People by name or address.** A people property (an assignee) takes names, addresses or user ids, matched against
  the workspace's own people. A name that matches no one, or several people, is refused, and the refusal names who can be chosen.
- **Numbers are exact.** `12.50` is sent as `12.50`, never through a float. A decimal comma ("1,250") is refused
  rather than guessed at.
- **Long or non-Latin text is split where Notion allows.** A rich-text piece holds at most 2,000 UTF-16 units, and the
  agent never cuts an emoji or an Arabic letter with its marks.
- **Update keeps the before.** `pages_update` returns and records each changed property's previous value.
- **Nothing is invented.** Page, data source and comment ids come from a search, a query or a page read.
- **Results stay small.** At most 25 rows per call (20 by default). A page is read at most 100 blocks at a time, with
  `from`, `next_from` and a `total`. A block's text is clipped at 2,000 characters and a property's at 500.

## Following a database

1. `watch_database {database_id}` returns a `watch_ref`.
2. The assistant schedules `watch_changes`. It runs with no model.
3. Each row says whether it was `created` or `edited`, and which watch it belongs to. `more: true` means a later check
   continues. A watch that failed (a database no longer shared, say) is listed in `problems` and keeps its place. Its
   rows come on the next run, and the other watches are not held back. A watch whose database was deleted or trashed
   closes itself and is listed in `closed`.

Notion keeps `last_edited_time` to the minute. The cursor is the newest minute handed on, plus the ids of the rows seen
on that minute. The ids are kept encrypted in `values` as an object, so a busy minute cannot overflow a field. A second
edit to a row within the minute it was already reported in carries the same time. That edit surfaces with the row's
next edit in a later minute.

Watches kept by 1.0.0 (status `watching`, a `seen` string) go on where they were. Their data source is looked up once,
and their seen ids move to `values`.

## Errors

| kind | when |
|---|---|
| `auth` | the token was revoked or has expired: reconnect the workspace |
| `denied` | Notion answered 403 `restricted_resource`: share the page with the integration (••• menu, Connections) |
| `not_found` | Notion answered 404: no such id, or not shared (Notion does not tell these apart, so the same hint is given) |
| `rate` | Notion stayed busy (429 or 529) past the wait the agent allows; the words say Notion's Retry-After |
| `conflict` | Notion reported a conflicting change; read again and retry |
| `unknown` | a write got no answer: whether it was done is unknown, and it is never sent again |
| `invalid` | refused before sending (an unknown property, option or person, a bad date or number), or by Notion |

A 429 is waited out as Notion's `Retry-After` says, up to 30 seconds in all, for reads and writes alike, since a
refused request did nothing.

## Setup

Once per organization, by an administrator, at [notion.so/my-integrations](https://www.notion.so/my-integrations):

1. Create a new integration of type **Public**.
2. Capabilities: **Read content**, **Update content**, **Insert content**, **Read comments**, **Insert comments**, and
   **Read user information including email addresses**. Assigning people and naming the account both need it.
3. Add the redirect URI DecentAI shows under **Settings → Connected apps**.
4. Copy the OAuth client id and secret into DecentAI as provider `notion`.

Then, per workspace: **Agents → Notion → Credentials → Connect account**, and choose the pages to share.

## Limits

- Titles only: Notion's search does not look inside page text.
- Comments are page-level; comment authors are user ids, not names.
- A page's total is counted up to 1,000 top-level blocks; a block's children up to 100.
- People are matched among the first 1,000 people of the workspace.
- Tested against a loopback stub of the API subset it uses. The first real connection is the first live run.
