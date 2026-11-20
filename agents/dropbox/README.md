# Dropbox (`dropbox`)

"Where is the Riverside lease, and who can open it?" — found by name or
content, with everyone who has access: the owner, the people invited,
anyone reaching it through a shared folder, and every link and how far
it reaches. Ask for a copy and it comes into the chat as a file, where
Documents can read it. Ask to put a file in Contracts and share it with
Dana, and each step waits for your go-ahead. Ask to hear when invoices
land in a folder, and a scheduled check hands on the ones that did.

The Google Drive and OneDrive agents' twin over Dropbox API v2, with
versions, restore and a change watch.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | The connection, its team, whether it has a team space, and how full it is |
| `files_search` | 0 | Files and folders matching a query, by name or content |
| `files_list` | 0 | What is in a folder, folders first, by path or id |
| `files_shared_with_me` | 0 | Files and folders others shared, newest share first, with who shared them and whether a folder is in the Dropbox yet |
| `files_get` | 0 | One item's details |
| `files_versions` | 0 | A file's earlier versions, a deleted file's too |
| `files_download` | 0 | A copy handed back as a file of the chat; Word and PowerPoint can come in as PDF |
| `files_upload` | 2 | A file of the chat into a Dropbox folder; never overwrites unless told |
| `files_create_folder` | 2 | A new folder |
| `files_move` | 2 | Renames, moves, or both |
| `files_restore` | 2 | A file put back at one of its versions; a deleted file brought back |
| `files_delete` | 3 | To Dropbox's deleted files; for everyone, in a shared folder |
| `sharing_permissions` | 0 | Who can open an item, and how |
| `sharing_invite` | 3 | Gives named people read or write access, with an optional message; Dropbox emails them. A folder becomes a shared folder first |
| `sharing_link` | 3 | A link for the team, or for anyone when asked |
| `sharing_revoke` | 3 | Removes one person, group or link |
| `changes_watch` | 1 | Starts watching a folder and everything under it, from now |
| `changes_new` | 1, schedulable | What changed since the last check, for every watch |
| `watches_list` | 0 | The watches, and whether each is active |
| `watches_close` | 1 | Stops a watch for good |

Every function but the watch checks takes `account` (the address) when
several Dropbox accounts are connected; a watch remembers its own.

Records: `share` — every grant and revocation this agent made, and a
half-done share it could not undo (`unfinished`); `watch` — the folders
watched, Dropbox's cursor for each, and the account and root it was
made on. Who and what was shared are kept encrypted.

## Rules it keeps

- **Nothing in the Dropbox changes without a yes.** Upload, folder,
  move, rename and restore change only the person's own Dropbox and can
  be undone (level 2); delete, share and un-share reach others (level 3).
- **Delete means Dropbox's deleted files.** They can be restored with
  `files_restore` for as long as the account's plan keeps them; nothing
  here deletes for good.
- **An upload never overwrites by default.** A name already taken gets
  Dropbox's own numbered name ("name (1).ext"); replacing is asked for
  by name (`on_conflict: replace`), and Dropbox keeps the old version.
- **Sharing a folder leaves nothing half done.** Making it a shared
  folder and adding people are two writes; when the second is refused,
  the folder is unshared again. If even that fails, the share is
  recorded as `unfinished` and the answer says what to undo. Dropbox's
  background jobs are waited for without holding up the chat.
- **A link is for the team.** "Anyone with the link" is made only when
  the person asks for it in those words. A personal account has no team,
  so the agent asks rather than widening it on its own. An item that
  already has a link gets that link changed, not a second one.
- **The owner's access, and access that comes from a shared parent
  folder, are not revoked here.**
- **People are addresses.** A name is refused until it is resolved.
- **A file is at most 8 MiB less 64 KiB each way** (the most a chat
  file can be). A larger one is refused before it is fetched, with its
  size and a Dropbox link to it that works for four hours.
- **A busy Dropbox is waited out** when it asks for a short wait
  (`Retry-After`); a longer one is answered as `rate`, and a write that
  got no answer is `unknown`, never sent twice.

## Team spaces

A Dropbox team with a team space roots its accounts at the team's
namespace. The agent reads the account's root (`users/get_current_account`)
and, where it differs from the person's own, sends
`Dropbox-API-Path-Root` on every call, so team folders are seen. Paths
then start at the team space; the person's own folder is
`account_status`'s `home_path`, and a path given from their own folder
("/Contracts/lease.pdf") is found there too. Without a team space no
header is sent.

## Watching a folder

The assistant records one watch (`changes_watch`) and schedules
`changes_new` every few minutes. A check calls no model, so one that
finds nothing costs nothing. The watch holds Dropbox's own
`list_folder` cursor, so each change is handed on once: files added or
changed (Dropbox does not say which), and anything deleted, with who
changed a file when it sits in a shared folder. Dropbox pages changes
25 at a time; `more` says the next check continues.

- Each watch is read on its own. One that fails (Dropbox busy, say) is
  listed under `problems` and keeps its place, so its changes come next
  time; the others are handed on now.
- A watch whose folder was deleted or moved closes itself and is listed
  under `closed`.
- If Dropbox declares a cursor stale, the watch starts again from now
  and is listed under `reset`: whatever changed in between was not seen.
- `watches_list` shows the watches; `watches_close` stops one.

## Where it differs from the drive twins

Dropbox has real paths, so every function takes a path ("/Contracts/
lease.pdf") as well as an id ("id:…"), and each row carries both. A
shared folder not yet added to the Dropbox has no path; its id is its
namespace ("ns:…"). A Word or PowerPoint file converts to PDF through
Dropbox's preview; spreadsheets do not (Dropbox previews them as HTML).
A Dropbox Paper document has no bytes of its own and comes in exported.
A file shared with the account but never added to it is fetched through
its link. Dropbox keeps no web address for an item, so `link` is filled
only where Dropbox gave one. Names in any script, Arabic included,
travel escaped in Dropbox's `Dropbox-API-Arg` header and come back whole.

## Setup

1. In the [Dropbox App Console](https://www.dropbox.com/developers/apps),
   create an app with **Scoped access** and **Full Dropbox** access.
2. Under **Permissions**, tick `account_info.read`,
   `files.metadata.read`, `files.content.read`, `files.content.write`,
   `sharing.read` and `sharing.write`, and submit.
3. Under **Settings → OAuth 2 → Redirect URIs**, add the redirect URI
   DecentAI shows under Settings → Connected apps.
4. Paste the app key and app secret into DecentAI under
   **Settings → Connected apps** as provider `dropbox` (client id and
   client secret).

Then, per Dropbox account, anyone: **Agents → Dropbox → Credentials →
Connect account**, sign in, done. The platform asks Dropbox for offline
access, so the connection keeps refreshing.

Leave `api_base_url` empty; it exists for the tests.

## What it will not do

- Delete for good, or empty Dropbox's deleted files.
- Bring a file larger than a chat file into the chat, or upload one.
- Restore a whole folder (Dropbox restores files; folders are restored
  from Dropbox on the web).
- Share with anyone outside the team unless asked in those words.

## Limits

- A new app works for its developer and up to 500 users before Dropbox's
  production review.
- Live Dropbox behaviour is verified against a loopback stub of the API
  subset it uses; the first real connection is the first live run.
