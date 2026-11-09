# Mail (`mail`)

Someone wants the assistant to read their mail and has never heard of
a developer console. They type their address and an app password, and
ask for the latest message from a supplier.

Any mail account, over the protocols mail has always spoken: read over
IMAP, sent over SMTP, both encrypted, from where this agent runs to
the account's own mail server. Nothing is registered with a provider
and nobody reviews anything.

It does what the Gmail and Outlook agents do, and the assistant uses
the three alike.

## Connecting an account

| Field | What goes in it |
|---|---|
| Email address | the account's address |
| Provider | `gmail`, `yahoo`, `icloud`, `fastmail`, or `other` |
| App password | a password made in the provider's security settings for one program to use — not the account's own, which most providers refuse here |
| IMAP server, SMTP server | for `other` only: the provider's mail servers, each with its port, as `imap.example.com:993` and `smtp.example.com:465` (or `:587`) |

| Provider | Where an app password is made |
|---|---|
| Gmail | 2-Step Verification on, then `https://myaccount.google.com/apppasswords` |
| Yahoo | Account security, Generate app password |
| iCloud | `https://account.apple.com`, Sign-In and Security, App-Specific Passwords |
| Fastmail | Settings, Privacy & Security, App passwords |

Microsoft's accounts are not here: Outlook.com takes no password over
IMAP, and the Outlook agent is the way in.

More than one account can be connected. `account_list` names them, and
every function that reads or writes mail takes an optional `account`
(the address, or a part of it). Left out, the only one answers; none
yet, and the person is asked to connect one; several is refused, with
their names. A draft, a watch and an inbox watch keep the account they
were made on.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | Signs in and opens the inbox; when that fails, says why and where an app password is made |
| `account_list` | 0 | The connected accounts, by address |
| `search_find` | 0 | Messages in one folder, newest first: by sender, recipient, subject, words anywhere, dates, unread |
| `search_thread` | 0 | One conversation whole, from the inbox and from what was sent, with its attachments listed |
| `search_save_attachment` | 2 | One attachment, handed to the chat as a file (25 MB at most) |
| `draft_reply`, `draft_compose` | 1 | A draft, kept by the platform and shown to the person. Nothing is sent |
| `draft_send` | 3 | Sends a draft, on the person's go-ahead. `sent` only when the mail server took it; `unknown` when it was handed over and no answer came, and never tried again |
| `draft_discard` | 1 | Marks a draft discarded |
| `watch_await_reply`, `watch_check` | 1 | Remembers a conversation and says whether somebody else has written since. `watch_check` is schedulable |
| `watch_inbox`, `watch_new_mail` | 1 | Remembers how far the inbox was read and hands on what arrived since, once. `watch_new_mail` is schedulable |

Drafts, reply watches and inbox watches are records.

## What differs from Gmail's own agent

- **No query language.** `search_find` takes a sender, a subject, words
  and dates as separate inputs.
- **One folder a search.** The account's archive of everything where
  it has one (Gmail's All Mail), else the inbox; `folder: sent` for
  what was sent.
- **A conversation is rebuilt.** A mail server keeps messages, not
  conversations: a conversation is the messages that name one another
  in `References`. Ids are the messages' own `Message-ID`s.
- **A draft lives on the platform,** not in the account's Drafts
  folder: what is sent is what the person was shown.
- **A copy of what was sent** is put in the Sent folder where the
  server kept none itself.
- **No link to a message:** a mail server has no web page for one.

## Where it connects

The mail servers of the four providers it knows by name, each on the
port mail is read on (993) and the ports it is sent on (465, 587;
iCloud only 587), and for another provider the servers the account
names (`from_secret`).
Every connection is opened with the standard's `Tunnel`, through the
sandbox's way out, which opens those hosts on those ports and nothing
else. The way out passes the bytes and reads none of them: the
connection is encrypted between this agent and the mail server.

**A server the account names is reached on the port written with it.**
The standard's `from_secret` names a field, not a port, and the way out
opens the host read from it on the web's ports (80, 443) only, unless
the field's value itself is `host:port`. So for another provider the
servers are written with their ports (`imap.example.com:993`,
`smtp.example.com:465`), and the agent connects on the port written
there. A server written without one is tried on mail's own ports, as
before (993; 465, then 587), which the way out refuses: the refusal
says to write the port.

## What it never does

It deletes no mail, moves none and marks none as read: folders are
opened to be read. The one thing it writes to a mailbox is the copy of
what it sent.
