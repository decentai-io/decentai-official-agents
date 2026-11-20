# OneDrive (`onedrive`) 2.0.0

"Where is the Riverside lease, and who can open it?" — found by name or content, in your drive or among what others
shared with you, with everyone who has access. Bring a copy into the chat; file a chat file into a folder; share it
with Dana. Read the Harbourline proposal paragraph by paragraph with its comments, and change one paragraph on your
go-ahead. Read and add to an Excel table; read a form's responses from the workbook it syncs to; and hear each morning
what changed.

One agent for a Microsoft account's OneDrive, work or school (Microsoft 365) or personal (outlook.com), and the Word,
Excel and Forms files in it. It replaces Word Online, Excel Online and Microsoft Forms (1.x), whose watches must be
made again here. It rides the same Microsoft connection as every Microsoft agent; several accounts may be connected,
and every function takes an optional `account`.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account_status` | 0 | Each connected account, its drive's kind (personal or business), how full it is |
| `files_search` | 0 | Files and folders by name or content, shared ones included (with their `drive_id`) |
| `files_list` | 0 | A folder, by path or id, a page at a time |
| `files_shared_with_me` | 0 | What others shared; Microsoft's retiring list, filled in by search |
| `files_get` / `files_versions` | 0 | One item's details; its earlier versions |
| `files_download` | 0 | A copy into the chat (at most 8 MB; bigger is refused with its link); Office files as PDF |
| `files_upload` | 2 | A chat file into a folder: one request under 4 MiB, an upload session above; renamed on a clash |
| `files_create_folder`, `files_move`, `files_move_many`, `files_copy` | 2 | Folders, moves and renames, copies (followed until done) |
| `files_copy_status` | 0 | A copy's progress |
| `files_delete` / `files_restore` | 2 | To the recycle bin; back from it (personal accounts) |
| `sharing_permissions` | 0 | Who can open an item, and how |
| `sharing_invite`, `sharing_link`, `sharing_revoke` | 3 | Share with people, make a link, stop sharing; each recorded |
| `word_find`, `word_read`, `word_comments` | 0 | Word documents, by paragraph, with their comments |
| `word_propose` / `word_discard` | 1 | Record (or drop) a change to one paragraph |
| `word_apply` | 2 | Save it, only if the document's content is unchanged since |
| `excel_find`, `excel_get`, `excel_read`, `excel_rows` | 0 | Workbooks, their sheets and tables, blocks and rows |
| `excel_write`, `excel_add_rows` | 2 | Cells (several blocks in one session) and table rows; each recorded |
| `forms_find`, `forms_responses` | 0 | A form's responses from the workbook it syncs to |
| `watches_document`, `watches_rows`, `watches_responses` | 1 | Watch a document, a table, a form |
| `watches_check` | 1 | Schedulable: what is new in every watch, once |
| `watches_list` / `watches_close` | 0 / 1 | The watches; stop one |

Records: `share` (every grant and revocation), `edit` (Word edits), `write` (workbook writes, with what the cells
held), `watch`. What people wrote, and who a file was shared with, are kept encrypted (`values`).

## Rules it keeps

- **Downloads never carry the token.** Graph hands each file a pre-authenticated address on another host
  (SharePoint, `*.files.1drv.com`, `my.microsoftpersonalcontent.com`, `*-mediap.svc.ms` for PDF); every such host is
  declared, and fetched without the Authorization header. Upload sessions likewise.
- **A shared file is reached in its owner's drive** (`drive_id` with `item_id`), never as one of yours.
- **Changes in your own drive are level 2; anything others see is level 3.** A link is for your organization unless
  you ask for "anyone with the link"; a personal account has no organization links and is told so.
- **An invitation Microsoft will not email** (it happens on personal accounts) still grants access, and you are told
  to send the link yourself.
- **A Word edit is saved only if the content is unchanged** (the cTag: a rename does not count), with `If-Match`;
  a document open elsewhere is reported as locked, and the edit waits.
- **Text that looks like a formula is written as text** unless you ask for a formula.
- **Excel and Forms need a work or school account**: Microsoft's workbook API does not exist for personal ones.
- **Watches never lose what they read**: each is read alone, a failing one keeps its place, a gone file closes its
  watch.

## Will not

- Add, answer or resolve Word comments; edit a paragraph holding tracked changes, a picture or an equation.
- Browse SharePoint site libraries other than files shared with you; list the recycle bin.
- Bring a file over 8 MB into the chat.

Tested against a loopback Graph as strict as Microsoft's documented rules; see the first-live-run list in the
release notes for what only a real account can confirm.
