# Tasks (`tasks`)

A meeting ends with six promises and no list. The office lease renews for
another three years unless someone gives notice ninety days before. Both
are the same question — what must I do, and by when — and this agent
keeps both.

Paste the notes: the assistant reads out the action items with the
passage that states each, you confirm them, and they become tasks with
owners and dates. Record a subscription, a lease, an insurance policy or
a passport once, with the clause it came from, and ask each month what
falls due: the answer names the day you must act — the notice deadline,
not the renewal date — and whose decision it is.

No external service. The model is used only to read notes and documents;
every quote it returns is checked verbatim against the text. All date
arithmetic is code.

## Functions

**Tasks** — obligations, and the action items read out of notes.

| Function | Level | What it does |
|---|---|---|
| `tasks_create` | 1 | A task, with owner and due only as given; a priority; repeating daily, weekly or monthly |
| `tasks_update` | 1 | Status, owner, due, note, priority, repeat; done needs evidence, blocked needs a blocker; done on a repeating task makes the next one |
| `tasks_list` | 0 | By owner, status, source, priority; due date first, then priority |
| `tasks_overdue` | 0, schedulable | As of a date (today in the person's zone): overdue, due soon, blocked, unassigned, undated |
| `extract_propose` | 1, uses the chat's model | Proposals from notes with verified quotes, commitment or suggestion, owner and date only when the notes give them, and each date reading said |
| `extract_confirm` | 1 | Proposals the user agreed to become tasks; owner and due may be supplied now; unverified ones only when accepted; safe to repeat |
| `extract_reject` | 1 | A proposal marked not wanted |

**Renewals** — anything that renews or expires: a subscription, a
contract, an insurance policy, a warranty, a maintenance plan, a lease,
a licence, a passport or visa.

| Function | Level | What it does |
|---|---|---|
| `agreements_extract` | 0, uses the chat's model | Reads a document's text; proposes the counterparty, dates, term, notice period and auto-renewal with verbatim clauses; names ambiguities and asks; records nothing |
| `agreements_create` | 1 | Records it with its clauses; derives the renewal and notice deadlines; says what is still to ask |
| `agreements_get` | 0 | One renewal with its clauses, deadlines and decisions |
| `agreements_update` | 1 | Changes dates, notice period or basis, owner, auto-renewal, value, note; recomputes and moves the open deadlines |
| `agreements_set_status` | 1 | Renewed or cancelled (each needs evidence and a name; renewed also the new renewal date), or expired |
| `deadlines_upcoming` | 0, schedulable | What is due or passed within a window, what awaits a decision, what cannot be computed |
| `deadlines_add` | 1 | A further deadline: warranty end, expiry, review, custom |
| `deadlines_complete` | 1 | Records a deadline acted on, with evidence; never cancels anything |
| `decisions_record` | 1 | The intended decision (renew, cancel, renegotiate), by whom and when |

Records: `task` and `agreement` (both editable on the page), `extraction`
(proposals, with what became of each), `clause`, `deadline`, `decision`.

## Proposals are not tasks

`extract_propose` asks the chat's model for the action items and, for
each, the exact passage that states it. Then the code checks:

- the quote must appear verbatim in the notes, or the proposal is marked
  `verified: false`;
- the owner must be a whole name the notes (or the `people` list)
  actually contain, or it is cleared — an owner is never invented, and
  "Al" is not "Alice";
- the due date is accepted when the notes state it whole (ISO, or a
  day, month and year), or read from `meeting_date` (a weekday name, a
  day and month without a year); a read date comes with `due_reading`,
  the sentence that says what it was read as, and the call lists them
  all in `date_readings`. "Next Friday" and "next week" stay as
  `due_text` for a person to decide.

`verified` is kept on the proposal. `extract_confirm` refuses an
unverified one unless the person has seen its quote and the call says
`accept_unverified`. Confirming is safe to repeat: the proposal is
marked before its task is made and the task names its proposal, so a
retry after a lost answer hands back the same task, never a second.

## Recurring tasks and priority

A task may repeat `daily`, `weekly` or `monthly`; it needs a due date to
count from. Marking it done makes the next one, once (a second "done"
finds it): due one step after the previous due date, or the first step
after the day it was done when it was done late. A monthly task keeps
its day of the month: the 31st becomes the 28th in February and the
31st again in March. Priority is `high`, `normal` (the default) or
`low`, and orders tasks due the same day.

A commitment is something a named person said they will do; a suggestion
is something someone said should be done. Both are proposals until
`extract_confirm`, where the user can add the owner or date the notes
lacked. A task confirmed without an owner is listed as unassigned until
someone takes it.

## Two dates, not one

Something that renews on 1 December with 60 days' notice has a renewal
deadline on 1 December and a notice deadline on 2 October (calendar
days) or 8 September (business days, Monday to Friday). The agent
computes the notice deadline only when it has all three: the exact
renewal date, the notice period in days, and the basis. While any is
missing it records what it has and returns `asks` naming what to get
from the user or the document. It never picks a basis, never completes
"December" into a day, and never turns "two months" into a number of
days.

`agreements_extract` asks the model for the dates and terms as written
and the clauses they come from. Code then checks each quote against the
text (`verified`), parses each date (only an exact day, month and year
counts), reads the notice period ("60 days", "60 business days", "sixty
(60) calendar days") and marks a clause `ambiguous` when the notice
period has no basis or the renewal date has no day. Nothing is recorded
until `agreements_create`.

## What needs me

Two deterministic checks, both fit for a schedule. Neither asks a model.

- `tasks_overdue`: what is late, what is due
  within `within_days`, what is blocked, and what has no owner or no date.
- `deadlines_upcoming`: every open deadline inside
  the window or already passed, with calendar and business days left;
  `awaiting_decision` for renewals whose notice deadline is near with no
  decision recorded; `incomplete` for those whose deadline could not be
  computed, and why.

## What is and is not a cancellation

- A reminder is a reminder.
- `deadlines_complete` on the notice deadline records that notice was
  given, with the message reference; the renewal stays active.
- `decisions_record` records an intention.
- Only `agreements_set_status` with the confirmation reference and a name
  marks something renewed or cancelled. Renewed takes the new renewal
  date and derives the next notice deadline.

## Sample data

What Sidra Office Supplies' last two meetings left to do: five tasks
(one blocked, one done, one with no owner) and two proposals waiting
to be confirmed. Three renewals with their clauses and deadlines: an office
lease whose notice deadline is 1 December 2026, an accounting
subscription whose notice deadline is 16 October 2026, and a printer
maintenance contract whose notice was due by 2 August 2026.

## Limits

- Dates are calendar dates; there are no times of day. "Today" is the
  person's when the call is given their `timezone`, and UTC otherwise.
- Business days follow the renewal's `work_week`: Monday to Friday by
  default, or Sunday to Thursday; public holidays are not known.
- Notice periods in months are reported as ambiguous, not converted.
- Extraction quality depends on the chat's model; the checks catch a
  misquote, an invented name and an ambiguous date, not a missed item.
- Documents are read from supplied text (use the Documents agent for PDF
  and Word); scanned pages without a text layer cannot be read.
- Values are exact decimals kept as text ("4800.50"), with the currency
  the person or document gave; none is assumed, and nothing is converted
  between currencies.
