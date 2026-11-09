# OneDrive (`onedrive`)

"Where is the Riverside lease, and who can open it?" — found by name,
wherever it was filed, with everyone who has access: the owner, the
people invited, any link and how far it reaches. Ask for a copy and it
comes into the chat as its file, where Documents can read it. Ask to put a
file in Contracts and share it with Dana, and each step waits for your
go-ahead.

One Microsoft 365 OneDrive over Microsoft Graph. It rides the same
Microsoft connection as every other Microsoft agent: connect once,
grant the one saved account to each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | The connection, the drive's kind, and how full it is |
| `files_search` | 0 | Files and folders matching a query, anywhere in the drive |
| `files_list` | 0 | What is in a folder, by path or id |
| `files_shared_with_me` | 0 | Files others shared, with who shared them |
| `files_get` | 0 | One item's details |
| `files_download` | 2 | A copy into the chat, as its file, for another agent to read; Office files can come in as PDF |
| `files_upload` | 3 | A file handed to the call (attached, or saved by another agent) into a OneDrive folder; never overwrites unless told |
| `files_create_folder` | 3 | A new folder |
| `files_move` | 3 | Renames, moves, or both |
| `files_move_many` | 3 | Moves several items into one folder in one call; one that fails is reported, the rest still move |
| `files_delete` | 3 | To the recycle bin, from which it can be restored |
| `sharing_permissions` | 0 | Who can open an item, and how |
| `sharing_invite` | 3 | Gives named people read or write access; Microsoft emails them |
| `sharing_link` | 3 | A view or edit link, for the organization unless asked otherwise |
| `sharing_revoke` | 3 | Removes one permission |

Records: `share` — every grant and revocation this agent made, so what
was shared, with whom, and when it stopped has an answer.

## Rules it keeps

- **Nothing in the drive changes without a yes.** Upload, folder,
  move, rename, delete, share and un-share are all level 3.
- **An upload never overwrites by default.** A name already taken
  becomes "name 1.ext"; replacing is asked for by name
  (`on_conflict: replace`), and OneDrive keeps the old version;
  `on_conflict: fail` refuses instead.
- **A link is for the organization.** "Anyone with the link" is made
  only when the user asks for it in those words.
- **The owner's access, and access inherited from a folder, are not
  revoked here** — the first cannot be, the second belongs to the
  folder.
- **People are addresses.** A name is refused until it is resolved
  from a mail thread or asked for.

## Limits

- Files up to 25 MB each way, in one request.
- Only the account's own OneDrive and files shared with it; SharePoint
  site libraries are not browsed.
- A file shared from someone else's drive can be read and downloaded,
  not changed or re-shared here.
- Live Microsoft 365 behaviour is verified against a loopback stub of
  the Graph subset it uses; the first real connection is the first live
  run.
