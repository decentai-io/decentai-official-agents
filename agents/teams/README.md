# Microsoft Teams (`teams`)

"What did I miss?" answered from your unread chats, counted in code. "What did Dana say?" read as text, oldest
first. "Tell her the drawings are coming Thursday" shown to you word for word and sent only when you say so. "Has she
replied?" answered from the chat itself. A watch on a schedule hands on each new chat message once.

A work or school (Microsoft 365) account's Teams over Microsoft Graph. A personal Microsoft account has no Teams API:
every function says so before anything is asked of Microsoft. It rides the same Microsoft connection as every other
Microsoft agent; several accounts may be connected, and every function takes `account`.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | The connection, whose account it is, and whether it can use Teams |
| `chats_list` | 0 | Chats by their latest message, newest first (Graph's own order), paged; `with` narrows by a person |
| `chats_read` | 0 | A chat's messages as text, oldest first, up to 200 at once, paged further back |
| `chats_missed` | 0 | Unread (or recent) chats with what others said since a time |
| `chats_send` | 3 | Text to a chat, or to a person by address (their one-to-one chat, found or started), with @mentions |
| `chats_replies` | 0 | Whether anyone answered something this agent sent, read back past 50 messages |
| `teams_list`, `channels_list` | 0 | Teams, and a team's channels |
| `channels_read`, `channels_replies` | 0 | A channel's posts and a post's replies (needs an administrator's consent, below) |
| `channels_post`, `channels_reply` | 3 | A post (with subject and @mentions), or a reply in a post's thread |
| `watches_start` | 1 | Watch one chat, or every chat, for new messages from others |
| `watches_check` | 1 | Schedulable: each new message since the last check, once |
| `watches_list`, `watches_close` | 0, 1 | The watches, and stopping one for good |

Records: `sent` (every attempt, where it went, and what became of it; the words and recipients encrypted) and
`watch`.

## Rules it keeps

- **Nothing is said without a yes,** and nothing twice. A send is one request to Microsoft. It is recorded as
  `sending` before it goes and `sent`, `unknown` or `failed` after. No answer means `unknown`: never retried. The same
  words to the same place after an unknown attempt are looked for in the chat first; if they are not there, they go
  only when the person says to send them again.
- **Times fit the call.** Each request has its own timeout and Microsoft's "busy, wait" (429) is waited out within a
  cap, so a function answers before the platform's clock runs out.
- **Text in, text out.** Teams returns HTML; it is read as text. Plain messages go as text; a message with mentions
  goes as HTML, escaped.

## Channel messages and the administrator

The shared Microsoft sign-in asks only for permissions a person can grant themselves. Reading channel messages needs
`ChannelMessage.Read.All`, which only an administrator can consent to, so `channels_read` and `channels_replies`
are refused with that explanation unless an organization adds it. Posting and replying in channels work without it.
Mentions in a channel find the person's Teams id among the members of the account's chats, since listing a team's
members needs another administrator permission.

## Limits

- No attachments, reactions or edits; no search across all of Teams.
- `chats_missed` reads up to 250 chats and 25 of them in detail; `chats_replies` reads back 500 messages.
- Verified against a loopback stub held to Microsoft's documented query rules; see the first-live-run list in the
  release notes.
