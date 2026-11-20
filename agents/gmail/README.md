# Gmail (`gmail`)

Dana at Harbourline asks for a quotation for twenty desks. Ask the assistant to find her email, draft the reply with
the quotation PDF from the chat attached, and remind you on Thursday if she has not answered. The draft appears in
Gmail before it goes anywhere; the send needs your go-ahead; and Thursday's reminder fires only if the thread is still
quiet.

One or more Google accounts, connected by signing in: the platform runs the consent, keeps the refresh token and
refreshes it; the agent receives a short-lived access token and the address. `account_list` names them; every
function takes an optional `account` (the address, or a part of it that names one account only); a draft and a watch
remember the account they were made on.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | The connection, its address and the addresses it sends as |
| `account_list` | 0 | The connected accounts by address |
| `search_find` | 0 | Gmail's own query operators → one row per message, newest first, with thread id, labels and link |
| `search_thread` | 0 | A conversation whole: each part decoded by its own charset, HTML made readable when there is no text part, long bodies marked `truncated` with a `next_offset`; attachments listed by `part_id`, inline images left out |
| `search_message` | 0 | One message, or the rest of a long one, from `offset` |
| `search_save_attachment` | 0 | One attachment into the chat (at most 8 MiB − 64 KiB; a bigger one is refused before download, with a link) |
| `draft_compose` | 2 | A new message as a Gmail draft, with chat files attached and the send-as signature |
| `draft_reply` | 2 | A reply in the thread (`reply_all` keeps everyone and the Cc; the account itself is never a recipient), quoted |
| `draft_forward` | 2 | A forward with the original's attachments and any chat files |
| `draft_send` | 3 | Sends a prepared draft; succeeds only on Gmail's word; an unanswered send is `unknown` and never sent again |
| `draft_discard` | 2 | Deletes a draft this agent made; its text stays in the record |
| `mail_labels` | 0 | The labels |
| `mail_label` | 2 | Labels on or off conversations or messages; `create_missing` makes a new one |
| `mail_archive` | 2 | Out of the inbox |
| `mail_mark` | 2 | Read, unread, starred, unstarred, important, not important |
| `mail_trash` | 3 | To the trash (Gmail deletes it for good after 30 days) |
| `mail_restore` | 2 | Back out of the trash |
| `watch_await_reply` | 1 | Records that the user waits for a reply on a thread |
| `watch_check` | 1, schedulable | Unanswered and answered threads; a gone thread closes its watch |
| `watch_inbox` | 1 | Starts an inbox watch (from now, or from `since`); `only_from` / `skip_from` |
| `watch_new_mail` | 1, schedulable | What arrived since the last check, oldest first, every message once |
| `watches_list` | 0 | Every watch, both kinds |
| `watches_close` | 1 | Stops a watch for good |

Records: `draft` (what was prepared, in values, and what became of it), `watch` (threads awaited) and `inbox` (how far
the inbox has been read).

## Writing

A draft is one RFC 5322 message: `multipart/alternative` (the text, and the same text as escaped HTML with the
send-as signature), inside `multipart/mixed` when files are attached, each non-ASCII file name per RFC 2231. Each file
is at most a chat file's size, and all of them at most Gmail's 25 MB. A draft up to 1 MiB goes as JSON; a larger one
as a multipart upload to `/upload/gmail/v1/users/me/drafts` (Gmail takes at most 35 MB there). A reply sets the thread
id, `In-Reply-To`, `References` and the "Re:" subject, so Gmail keeps it in the conversation.

## Watching

A schedule runs `watch_check` and `watch_new_mail`, through the shared watch library: each watch is read on its own,
one that fails is listed in `problems` and keeps its place, and no cursor moves until every watch was read.

The inbox watch follows Gmail's history from a `startHistoryId`. Gmail keeps history for about a week, sometimes only
hours; when the id has expired (404), the watch reads the inbox again from the time of the last message it handed on,
through every page, and returns to history from the id the mailbox had before that reading. Nothing is skipped however
long the gap; a burst larger than `max_results` is handed on across checks. Mail from the account and its send-as
addresses never counts. Inbox watches made by 1.x go on from their time.

## What it will not do

Send, trash or forward on its own; delete mail for good; change Gmail settings or filters; bring a file larger than a
chat file into the chat. It needs `gmail.modify` and nothing more of Gmail.
