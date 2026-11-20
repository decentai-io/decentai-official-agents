# Slack (`slack`)

"What was said in #showroom this morning?" — read as text, oldest first, with names instead of user ids. "Has anyone
answered my question about the chairs?" — answered from the thread and the conversation after it. "Send Dana a
message that the samples arrived" — Dana found by name or address, the text shown to you word for word, sent as you
only when you say so. "Open the spreadsheet Omar shared" — brought into the chat. And "tell me whenever someone
mentions me or messages me" — a watch that a schedule checks, waking the assistant only when something arrived.

One person's Slack over the Slack Web API, signed in with a **user token**: the agent sees what that person sees,
and what it sends is sent in their name. Several workspaces may be connected; every function takes `account`.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | The connection: workspace, user name and user id, for each account |
| `conversations_list` | 0 | Channels, private channels, group DMs and DMs the person is in; DMs named by the other person |
| `conversations_messages` | 0 | Recent messages, oldest first, with authors, reply counts, shared files and links; `oldest` alone gives the first ones after it |
| `conversations_replies` | 0 | A thread's parent and replies, oldest first |
| `messages_search` | 0 | Slack search (`in:#channel`, `from:@name`, `after:`, `on:`), newest first |
| `messages_answered` | 0 | Whether anyone other than the person wrote after a message, in its thread or the conversation, and who |
| `people_find` | 0 | Someone by address (`users.lookupByEmail`), full name, part of a name, `@handle` or user id; several matches come back as candidates |
| `files_read` | 0 | A file shared in a conversation, brought into the chat (at most 8 MiB less 64 KiB) |
| `messages_send` | 3 | Text posted as the person to a conversation, optionally into a thread |
| `messages_direct` | 3 | A direct message to one person by address, name or user id; the conversation is opened (`conversations.open`) if it never was |
| `reactions_add`, `reactions_remove` | 3 | An emoji reaction as the person, or taken back |
| `watch_start` | 1 | Start watching mentions, DMs, or both, from now; conversations can be left out |
| `watch_new` | 1, schedulable | What arrived since the last check, oldest first, each row a mention or a dm |
| `watches_list` | 0 | The open watches (closed ones too, when asked) |
| `watches_close` | 1 | Stop a watch for good |

Records: `sent` — every message this agent posted, where, and the `ts` Slack gave it (its text kept encrypted, in
`values`). `watch` — what is watched, on which account, and each source's place.

## Rules it keeps

- **Nothing is said without a yes.** Sending, direct messages and reactions are level 3: others see them.
- **Nothing is said twice.** A send that gets no answer is of unknown outcome and not retried; a send Slack refused
  with 429 (so did nothing) is sent again after the wait it named.
- **Nothing is guessed.** Conversation ids, user ids and `ts` values are Slack's own, passed on unchanged. A name
  two people share is never picked between: the candidates come back so the person chooses.
- **Answers stay small.** At most 25 messages per answer, long text clipped, `more` says when there is more.
- **Failures have kinds:** `auth` (reconnect), `scope` (the account was connected without a permission this needs,
  such as `files:read` for an account connected through 1.0.0: reconnect it), `not_found`, `denied`, `rate` (Slack's
  `Retry-After` waited out when short, reported when long), `invalid`, `too_large`, `http`, `unknown`.

## Watching mentions and DMs

Record a watch once with `watch_start`, then schedule `watch_new` every few minutes. Each check, with no model, reads
each watch on its own: one that fails keeps its place and is named in `problems`; the others are handed on.

- **Mentions** come from Slack search (`<@USERID>`), whose index lags. Each check looks 15 minutes behind the newest
  mention it handed on and leaves out the ones it already handed on, so a mention indexed late still comes, once.
- **DMs and group DMs** each keep their own place. Every one is read in turn: those Slack marks as updated first, then
  the least recently read. History pages run newest first; when one read cannot reach back to a conversation's
  place, the next read narrows to below what it saw, so a burst is handed on whole, oldest first, never stepped over.
- The person's own messages and skipped conversations are left out; a mention inside a DM is one `dm` row.

## Slack's rate limits (2025)

Since 29 May 2025 (existing installations from 3 March 2026), an app **distributed to other workspaces outside the
Slack Marketplace** may call `conversations.history` and `conversations.replies` once a minute, 15 messages a page.
An organization's **own app, installed only in its own workspace**, keeps 50+ a minute and up to 999 a page. Register
the Slack app per deployment, in the workspace it serves, to keep the second.

Under the first, the agent still works, more slowly: reading a conversation asks for one page; a long thread or a
`messages_answered` that needs two reads may come back as `rate`; and the watch reads one conversation a minute,
stopping its DMs at the first 429 with every place kept and `more` set, while mentions (search, not limited) are
handed on as usual.

## Setup

1. At <https://api.slack.com/apps>, **Create New App** → *From scratch*, in the workspace it will be used with.
2. Under **OAuth & Permissions**, add the redirect URL the platform shows on **Settings → Connected apps** for
   provider `slack`. Slack takes only an https redirect URL.
3. Under **User Token Scopes** (not Bot Token Scopes) add: `channels:read`, `channels:history`, `groups:read`,
   `groups:history`, `im:read`, `im:history`, `im:write`, `mpim:read`, `mpim:history`, `users:read`,
   `users:read.email`, `search:read`, `chat:write`, `reactions:write`, `files:read`.
4. Leave **token rotation** off. The platform then holds a long-lived user token; this agent never refreshes one.
5. From **Basic Information**, copy the Client ID and Client Secret and register them under **Settings → Connected
   apps** as provider `slack`.
6. Install the agent, open its Credentials tab and connect. Every person connects their own account. An account
   connected through 1.0.0 lacks `im:write`, `reactions:write` and `files:read`: reconnect it to use those.

`api_base_url` on the credential exists for the tests' loopback Slack; leave it empty.

## What it will not do

- No edits, deletes, file uploads, scheduled messages or group DMs opened by the agent.
- Files larger than 8 MiB less 64 KiB are refused before download, with their Slack link; files kept outside Slack
  (Google Drive and the like) are refused with their link, and the token is never sent there.
- The watch reads DM history, which holds top-level messages, not thread replies; a reply in a DM thread is seen only
  if it mentions the person.
- More than 300 mentions between two checks: the oldest beyond three search pages may be missed.
- Tested only against a loopback stub of the Web API subset it uses; the first real connection is the first live run.
