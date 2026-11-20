# Google Calendar (`google_calendar`) 2.0.0

Google Calendar and Google Tasks in one agent, as Google's own app pairs them. It absorbs `google_tasks` 1.0.0.

You need an hour with Dana next week, afternoons only, Sunday to Thursday. The assistant checks both calendars, says
whose calendar it could not see, and proposes slots. Pick one: it checks the slot is still free, books it with a Meet
link, and sends the invitation, which is the step you approve. Your Google Tasks sit beside it, and a schedule can tell
you each morning what was ticked off.

Same Google connection as every Google agent: connect an account once and grant it to each. Several accounts can be
connected; every function takes `account` (the address, or a part of it that names one).

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | Each account: connected, its zone, today's date there, whether Tasks answers |
| `calendars_list` | 0 | Every calendar the account sees; `calendar_id` (default `primary`) picks one elsewhere |
| `events_list` | 0 | Events in a window (default today), in the calendar's zone or `timezone`; recurring ones expanded; Google's page tokens |
| `events_get` | 0 | One event with each attendee's real response |
| `events_availability` | 0 | Free/busy of the account's calendars and attendees; `unchecked` names calendars that can't be read |
| `events_find_times` | 1 | Slots computed in code: working days (Mon–Fri default, Sun–Thu possible), working hours, avoid windows, buffers; each kept as a proposal |
| `events_create` | 2 | An event on your own calendar with nobody invited (also all-day, repeating, shown as free) |
| `events_book` | 3 | A meeting with attendees from a proposal or a time; slot re-checked first; optional Meet link; `send_updates` all, externalOnly or none |
| `events_update` | 3 | Edit or move; start and end move together, the length kept unless told |
| `events_attendees` | 3 | Add or remove people |
| `events_respond` | 3 | Accept, decline or tentative |
| `events_cancel` | 3 | Cancel for everyone when you organise it, otherwise decline; says which (`done`) |
| `lists_list`, `lists_create` | 0, 2 | Task lists |
| `tasks_list`, `tasks_get` | 0 | Tasks a page at a time in your own order (any length); one task with its subtasks |
| `tasks_create`, `tasks_update`, `tasks_complete`, `tasks_reopen`, `tasks_move` | 2 | Changes you can undo; due dates are dates |
| `tasks_delete` | 3 | Deletes a task and its subtasks |
| `watches_create`, `watches_list`, `watches_close` | 1, 0, 1 | A watch on one list or every list, for `completed` or `changed` |
| `watches_check` | 1 | Schedulable: what each watch saw since last time, once; this agent's own edits left out |

Records: `proposal` (slots offered), `booking` (what this agent did to which event; the title and attendees
encrypted), `watch` (a cursor in Google's time).

## How it behaves

- **Zones.** A time without an offset is in the calendar's zone (read once per chat), or `timezone`. "Today" is today
  there, never in UTC.
- **Idempotent booking.** The event id is chosen from the request (or `request_id`), so asking twice, or after an
  answer was lost, finds the first event (`already: true`). A lost answer is looked up by that id before anything is
  sent again.
- **Watches** keep Google's time to the millisecond with the ids on that instant; a watch whose list is gone closes;
  one that fails keeps its place while the others are handed on; cursors are written once every watch was read.
- **Busy Google.** 429, and 403 `rateLimitExceeded`, are waited out (`Retry-After`), then reported as `rate`.

## What it will not do

Guess an attendee's address; call a calendar it could not read free; cancel someone else's meeting (it declines);
move an event it doesn't organise unless the organiser let guests modify it; send a level-3 change without approval.

## Limits, and what only a live account can confirm

The tests run the agent as its own program against a loopback Google (`tests/gcal_stub.py`) as strict as Google's
documented rules. Not yet verified live: free/busy keyed `primary` as asked; an attendee's response sent by patching
the whole attendee list; `destinationTasklist` moves; whether `updatedMin` is inclusive at the millisecond.
`find_times` looks at 31 days at most. Watches cover Google Tasks only, not calendar events.
