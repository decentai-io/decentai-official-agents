# Outlook (`outlook`)

Dana at Harbourline asks for a quotation for twenty desks. Ask the assistant to read her whole conversation, draft the
reply with the price list attached, find an hour next week when both of you are free, and add "chase Dana" to your To Do
list. Nothing is sent, booked or deleted until you say so.

One agent for a Microsoft 365 (work or school) or Outlook.com (personal) account: mail, calendar and Microsoft To Do.
Version 2.0.0 absorbs the Microsoft Calendar and Microsoft To Do agents.

The platform runs the sign-in, keeps the refresh token and hands the agent a short-lived access token and the address.
A person may connect several accounts: `account_list` names them, every function takes an optional `account`, and a
draft, a proposal and a watch remember the account they were made on.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_list`, `account_status` | 0 | The connected accounts; one's addresses, work or personal, time zone and today's date |
| `mail_list` | 0 | A folder's newest mail, optionally unread only, from one sender, since a date |
| `mail_find` | 0 | Microsoft's search (`from:`, `subject:`, `received>=`), newest first; `page_token` continues |
| `mail_thread` | 0 | A whole conversation, each message's own words, its attachments; paged with `next`, never cut |
| `mail_read` | 0 | One message whole, the HTML read as text; a long body in parts (`next_offset`) |
| `mail_save_attachment` | 0 | A file attachment, or an attached message as `.eml`, into the chat (at most 8 MiB less 64 KiB) |
| `mail_folders` | 0 | Folders with their paths and counts |
| `mail_mark`, `mail_move`, `mail_archive` | 2 | Read/unread and flags; moving to a folder by name or path; the Archive folder |
| `draft_compose`, `draft_reply`, `draft_forward` | 2 | Outlook drafts: new, reply or reply to all (the quoted history kept), forward; chat files attached |
| `draft_attach` | 2 | More chat files on a prepared draft (under 3 MB in one request, larger in an upload session) |
| `draft_send` | 3 | Sends a draft; succeeds only when Outlook confirms it left Drafts, otherwise "unknown" and never resent |
| `draft_discard` | 2 | The draft to Deleted Items |
| `calendar_list`, `calendar_get`, `calendar_calendars` | 0 | What is on (recurring meetings expanded, all-day events as days, any calendar), one event whole |
| `calendar_availability` | 0 | Busy periods of the account and of others |
| `calendar_find_times` | 1 | Free slots on working days, kept as proposals |
| `calendar_create` | 2 | An event of your own, nobody invited |
| `calendar_invite`, `calendar_update`, `calendar_cancel`, `calendar_respond` | 3 | Invitations (a Teams link on work accounts), changes, cancelling (or declining someone else's), answers |
| `tasks_lists`, `tasks_list`, `tasks_get` | 0 | To Do lists and tasks; due and completed dates in your zone |
| `tasks_create`, `tasks_update`, `tasks_complete`, `tasks_reopen`, `tasks_create_list` | 2 | Changes to tasks you can undo |
| `tasks_delete` | 3 | A task deleted for good |
| `watch_reply` / `watch_check` | 1 / 1, schedulable | A reminder while a reply is missing |
| `watch_inbox` / `watch_new_mail` | 1 / 1, schedulable | New mail in a folder, once each |
| `watch_tasks` / `watch_task_changes` | 1 / 1, schedulable | What was ticked off (or changed) in To Do, from its own change feed |
| `watches_list`, `watches_close` | 0, 1 | Every watch; stopping one |

## How it reads

- **Whole conversations.** A thread is read by its conversation id, ordered here (Graph refuses a filter on one property
  with an order on another). Each message comes with its own words (`uniqueBody`), the quoted history being the
  messages before it; `quoted: true` gives every body whole. When the size runs out the result says `complete: false`
  and `next` names where to go on.
- **Text from HTML.** Outlook is asked for text bodies; an HTML body that still arrives is turned into text here.
- **Attachments** are listed by kind (file, attached item, cloud link); inline pictures such as a signature's logo are
  left out.
- **Who is "me".** Every address of the account counts as its own: the mail address, the sign-in name and the aliases.

## Time

The person's zone is the `timezone` input, else the mailbox's own (read from its settings, a Windows name mapped through
the whole of CLDR's table), else UTC, and the result says which. Working days are the mailbox's own, or `working_days`
(Sunday to Thursday works), else Monday to Friday. Due dates are written as their midnight in the person's zone;
completions are dated by the person's day.

## Personal accounts

Other people's free/busy and Teams meeting links need a work or school account; on Outlook.com the agent says so before
writing anything. Finding a time on a personal account reads the account's own calendar and names the others as
unchecked.

## What it will not do

- Send, invite, cancel, answer an invitation or delete a task without your go-ahead.
- Send a draft twice after an unconfirmed send.
- Bring a file larger than 8 MiB less 64 KiB into the chat: it gives the message's link instead.
- Read a shared mailbox, or other people's calendars beyond free/busy.
- Report a completion again because a done task was edited.
