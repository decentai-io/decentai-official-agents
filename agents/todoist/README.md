# Todoist (`todoist`)

You keep your to-dos in Todoist. Ask what is due today or overdue, and the assistant reads your list. Tell it to add
a task or a sub-task to a project with a date and a priority, move it, or tick it off, and it does it there, or tells
you the project names you really have. Tick things off in the Todoist app, recurring tasks included, and a schedule
carries them into your other lists without you asking.

It speaks Todoist's unified API v1 (`api.todoist.com/api/v1`) through the repository's shared `standard.http` client.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | Confirms the connection: email, full name, time zone |
| `projects_list` | 0 | Active projects, with parent, shared, Inbox and link |
| `projects_create` | 2 | A new project, optionally under an existing parent |
| `sections_list` | 0 | Sections of one project or of all |
| `labels_list` | 0 | The person's labels |
| `tasks_list` | 0 | Open tasks by project, section, label, a parent's sub-tasks (`parent_id`), or a filter query (`today \| overdue`); at most 25 per answer, `cursor` continues |
| `tasks_get` | 0 | One task with its description, comment count, completion and sub-tasks |
| `tasks_create` | 2 | Adds a task, or a sub-task with `parent_id`: content, description, project, section, `due_string` or `due_date`, priority, labels |
| `tasks_update` | 2 | Changes content, description, due, priority or labels (never where it is) |
| `tasks_move` | 2 | Moves a task, with its sub-tasks, to exactly one of a project, a section, or under another task |
| `tasks_complete` | 2 | Ticks a task off; a recurring one moves to its next date |
| `tasks_reopen` | 2 | Puts a completed task back |
| `tasks_delete` | 3 | Deletes a task and its sub-tasks: it can't be taken back |
| `comments_add` | 3 | Comments on a task: everyone the project is shared with sees it |
| `sync_watch` | 1 | Starts a watch on completions, in all projects or one, from now or from `since` |
| `sync_completed` | 1 | What was completed since the last check, oldest first; **schedulable** |
| `watches_list` | 0 | The completion watches, with project, account and source |
| `watches_close` | 1 | Stops a watch for good |

Every function takes an optional `account` (the account's address, or a unique part of it) for when several Todoist
accounts are connected; a watch remembers its account. Records: `watch`, one per completion watch.

## Names, never guesses

A project, section or label is always one the account already has. A name that matches nothing is refused with the
real names listed, and nothing is sent. Todoist itself would quietly create an unknown label or drop the task in the
Inbox. Names match in any case, and an id a list returned works too.

**Priority** is the app's word: `P1` is the most urgent, `P4` the default. The API counts the other way (API 4 is the
app's P1); the agent converts both ways. **Due dates**: `due_string` takes Todoist's own words (`tomorrow`,
`every Monday`, `no date` to clear); `due_date` takes `YYYY-MM-DD`.

**Moving** goes through `POST /tasks/{id}/move` with exactly one of `project_id`, `section_id` or `parent_id`; an update
never moves a task. A section named alone is looked for in every project; `section_project` narrows it.

## How the completion watch counts

Completions are read from the **activity log** (`GET /activities?event_type=completed`): one event per completion, so
a recurring task, which stays open at its next date, is handed on every time an occurrence is ticked off. Its row
carries `next_due_date`. If the plan has no activity log (Todoist answers 403), the watch reads
`GET /tasks/completed/by_completion_date` instead, which never lists a recurring task's occurrences, and `sync_watch`
says so in `warning` and `recurring_seen: false`.

The watch keeps a cursor in Todoist's own clock: the time of the newest completion handed on, and the ids exactly on
that time, so a completion in the same instant is neither lost nor shown twice. A check sorts oldest first, hands on at
most `max_results`, and `more` says the next continues. Every watch is read on its own (`standard.watch`): one that
fails is named in `problems` and keeps its place while the others are handed on; one whose project was deleted or
archived closes itself and is named in `closed`. Cursors are written only after every watch was read. A 1.0.0 watch
(`status: watching`, reading completed tasks) keeps working and is marked `active`.

## Busy, failed, unknown

- 429 is waited out as `Retry-After` (or Todoist's `error_extra.retry_after`) says, within 30 seconds; beyond that
  the answer is `kind: rate`. Reads are also tried again on 5xx and dropped connections.
- Every write carries an `X-Request-Id` that each attempt repeats, so a write Todoist answers busy is done once. A
  write with no answer is `unknown` and never sent again.
- `auth`: not connected, or the token was revoked: reconnect. `not_found`: no such task or project. `invalid`: a name
  that does not exist, a bad date, a move naming no place or two.

## Setup

1. In the [Todoist App Management console](https://developer.todoist.com/appconsole.html), create an app.
2. Set its **OAuth redirect URL** to the one the platform shows under **Settings → Connected apps** for `todoist`.
3. Copy the app's client id and secret there as provider `todoist`.
4. Install this agent, open its Credentials tab, and **Connect**. The sign-in asks for `data:read_write` and
   `data:delete`.

Todoist tokens do not expire. A revoked one answers `kind: auth`.

## What it will not do, and what is not verified

- It does not assign tasks to other people, set reminders or durations, or upload attachments.
- Only personal labels are checked; shared labels from shared projects are refused by name.
- Tested against a loopback stub held to Todoist's OpenAPI description (v1 paths only, cursor paging, the move
  endpoint's one-of rule, 429 with `Retry-After`), not yet against a live account.
