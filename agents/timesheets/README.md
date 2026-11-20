# Timesheets (`timesheets`)

Friday afternoon, and the week has to be accounted for. Ask for this
week's meetings to become timesheet entries: the showroom meetings
with Harbourline land on the showroom project, the stand-ups on admin,
and the lunch no rule knows about comes back as a question rather than
a guess. Then ask where the gaps are — Wednesday, two hours — fill it,
submit the week, and get the workbook to send.

## Functions

| Function | Level | What it does |
|---|---|---|
| `projects_add` | 1 | A project, by short code; billable unless said otherwise, with an optional hour budget |
| `projects_list` | 0 | Projects with hours logged and budget left |
| `projects_update` | 1 | Rename, change the client, billable or budget, archive |
| `projects_add_rule` | 1 | Place meetings on a project when the title, an attendee or the location contains a text |
| `projects_rules` | 0 | The rules |
| `projects_remove_rule` | 2 | Deletes a rule; what it placed stays |
| `entries_log` | 1 | Hours on a project on a day (today in the person's zone by default) — a number, or a start and end |
| `entries_from_events` | 1 | Calendar meetings into entries, by the rules; returns what it could not place |
| `entries_list` | 0 | A week's entries, or a date range's, with the total |
| `entries_update` | 1 | Changes an entry in an open week |
| `entries_remove` | 2 | Deletes an entry from an open week |
| `weeks_summary` | 0 | The week by project and day, billable hours, and the gaps |
| `weeks_gaps` | 0, schedulable | This week's short working days up to yesterday, and a sentence that says so; no model |
| `weeks_submit` | 1 | Hands the week in; its entries lock |
| `weeks_reopen` | 1 | Unlocks a submitted week |
| `weeks_workbook` | 0 | The week as an Excel workbook, read back cell by cell before it is handed back as the call's file |
| `timer_start` | 1 | Starts timing work on a project; one timer at a time |
| `timer_stop` | 1 | Stops it and logs the time, to the quarter hour, on the day it started; over 10 hours it asks first |
| `timer_status` | 0 | What is running, since when |
| `timer_cancel` | 1 | Stops it without logging |

Records: `project`, `rule`, `entry` (hours on one project on one day),
`week` (a week handed in, and when) and `timer` (time running on a
project).

## From the calendar, by your rules

The calendar agents list meetings; this agent takes those rows as they
are. Each meeting's length is rounded to the quarter hour and logged
on the day it took place. Which project it goes on is decided by the
user's rules, in plain terms — the title contains "Harbourline", an
attendee's address contains "harbourline.example", the location
contains "Riverside". A meeting no rule places, or one that two rules
place on different projects (the "Harbourline stand-up"), comes back
unassigned with the reason; the user's answers go back in as
`assign`, an event id and a project code for each. A meeting already
on the timesheet is not logged again, an all-day, cancelled or
declined one is not time spent, and one in a submitted week is
skipped.

## Rules it keeps

- **Nothing guesses a project.**
- **A day holds at most 24 hours.**
- **Today is the person's.** Every function that defaults to today or
  this week takes a `timezone` (IANA, such as Asia/Dubai); without one
  it is UTC, and the answer says which day it took.
- **A gap is a working day that has happened.** Days after `as_of`
  (today) are not gaps; `daily_hours` sets what a full day is (8), and
  `work_week` which days are working days: `mon_fri` (the default) or
  `sun_thu`.
- **A submitted week is locked.** Nothing is logged, changed or removed
  in it until it is reopened.
- **Hours add up.** They are kept to two decimals, summed as decimals
  and handed out as text ("1.50"), so the week's total is its days'
  totals and a budget's remainder is exact.
- **The workbook holds what was written.** Every cell is read back and
  compared before the file is returned; a meeting title that starts
  like a formula ("=…") is written as text, never run.
- **A timer is logged once.** It is marked stopped before its entry is
  made, and the entry names its timer, so a stop retried after a lost
  answer finishes the same one.
- **Sending is someone else's job.** The workbook is a file; the mail
  agent sends it.

## Sample data

Lina's week of 7 September at Sidra Office Supplies: three projects
(the Harbourline showroom, billable with a 60-hour budget; the
Riverside office move; admin), the five rules that place her meetings,
and four days logged — Monday, Tuesday and Thursday full, Wednesday
with only two hours, and Friday still to come.

## Limits

- One person's timesheet; there is no approver here, and rates and
  invoicing are out of scope.
- Weeks are ISO weeks, Monday to Sunday, so a Sunday-to-Thursday
  working week's Sunday is the last day of the ISO week before it.
- A timer is logged on the day it started; a timer that runs past
  midnight is not split across days.
- Meetings are taken as listed; the agent does not read calendars
  itself.
