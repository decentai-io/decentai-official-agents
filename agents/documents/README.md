# Documents (`documents`)

A forty-page supplier contract arrives as a PDF, half of it scanned. Ask what the notice period is and the answer
comes with the page and the sentence it was read from, and says if the sentence was read off a picture. Ask what
changed since the last version and the differences are listed, not guessed. Ask for the summary in Arabic and the
PDF comes back in Arabic, shaped and right to left.

Version 2.0.0 merges the Presentations agent (`slides`) into this one: decks are read and produced here.

No external service and no credential. Files come in as platform files (a person's upload in the chat, or one
another agent saved) by `file_ref`; produced files go back as the call's files.

## Supported formats

| Kind | Read | Produced |
|---|---|---|
| PDF with a text layer | yes, page by page, to the last page (up to 2,000) | yes (`produce_pdf`); merged, split, rotated; forms filled |
| Scanned PDF pages, photos, scans (PNG, JPEG, WebP, GIF, TIFF) | through the chat's model (`read_pictures`), marked `from_picture` | — |
| Word `.docx` | yes, in parts of about 5,000 characters, to the end; tables where they stand; headers and footers | yes (`produce_docx`, `produce_fill_template`) |
| PowerPoint `.pptx` | yes, slide by slide: titles, text, tables, chart numbers, speaker notes; pictures through the model | yes (`produce_pptx`, `produce_fill_template`) |
| Text, Markdown, CSV | yes, in parts; UTF-8 (with or without BOM), UTF-16, Windows Arabic (cp1256) and Western (cp1252) | — |
| Legacy `.doc`/`.ppt`, spreadsheets, HEIC photos | no: named as unsupported, with what to save them as | — |

## Functions

| Function | Level | What it does |
|---|---|---|
| `read_inspect` | 0 | Kind, pages (parts, slides), unreadable and scanned pages, characters, headings, encoding, pictures; a deck's titles, chart slides, notes, layouts |
| `read_text` | 0 | Text by page from `from_page` (and `from_char` inside a long page), up to `max_chars`; `next_page`/`next_char` reach the end |
| `read_pictures` | 0, uses the model | A scanned page rendered to a picture, a photo, a slide's or a Word file's pictures, read by the model (`transcribe` or `describe`), up to 8 a call; refusals and cut readings said |
| `read_extract` | 1, uses the model | Named fields over the whole document in windows; each verified (quote on the page, value in the quote), unverified, `not_stated` or `not_read`; recorded |
| `compare_versions` | 0 | Added, removed and changed passages with their pages; `focus` words first |
| `produce_docx` | 1 | A Word file from sections, paragraphs and tables; Arabic right to left; read back; recorded |
| `produce_pdf` | 1 | The same as a PDF in bundled Noto fonts, Arabic shaped; read back; recorded |
| `produce_pptx` | 1 | A 16:9 deck (or a template's size); tables and bullets continue across slides; checked for overlaps; recorded |
| `produce_fill_template` | 1 | `{{placeholders}}` filled in a Word or PowerPoint template; unfilled ones listed |
| `pdf_merge`, `pdf_split`, `pdf_rotate` | 1 | Pages copied as they are, into new files; recorded |
| `pdf_fields` | 0 | A PDF form's fields, types, values and options |
| `pdf_fill` | 1 | The form filled from values, each checked first; read back; empty fields listed |

None is schedulable: each works on a file someone hands it. Records: `extract` (what was read from which file,
by window) and `output` (every file produced, by operation and source). Files: `source`.

## Evidence, not opinion

`read_extract` sends the document to the model in windows of `max_chars` (24,000 by default, up to `max_windows`),
asking only for the fields not yet found. Code then checks each answer twice: the quote must be in the page's text
(spacing, the script of the digits and Arabic diacritics aside), and the value must be written in the quote. A field
is `not_stated` only when every page was read. When a page was not read (a scanned page without `read_pictures`, a
window the model refused or cut short, a window past `max_windows`), a missing field is `not_read`, and the pages
are named.

What the model reads off a picture is not the file's text: it is marked `from_picture` wherever it appears, and a
field verified against it says so.

## Arabic

- PDFs use Noto Sans and Noto Naskh Arabic, bundled in `fonts/` under the SIL Open Font License (`fonts/OFL.txt`),
  shaped by HarfBuzz. A paragraph whose first strong letter is Arabic is set right to left; an Arabic table's
  columns start from the right. A character neither font has (Chinese, emoji) is refused by name, never printed as
  "?".
- Word files set `w:bidi`/`w:rtl` on Arabic paragraphs and `w:bidiVisual` on Arabic tables; decks set `rtl` on
  Arabic paragraphs and tables.
- Arabic-Indic digits and the Arabic decimal and thousands marks (٫ ٬) match their ASCII forms when quotes are
  checked.

## What it will not do

- Convert a Word file to PDF with its layout: that needs a word processor (LibreOffice or Word), which this agent
  does not carry. It can produce a new PDF from the Word file's text; the layout will differ.
- Open a password-protected PDF, or fill an XFA (LiveCycle) form.
- Read footnotes, comments or text boxes in Word files, or SmartArt in decks.

## Limits, and what is not verified

- Files are at most 8 MB less 64 KB (the chat's limit), refused with their size before they are read.
- OCR quality is the chat's model's. A scanned page is rendered at about 200 dpi, at most 2,000 pixels a side.
- A PDF's own text layer for Arabic is as the PDF stores it: some PDFs store Arabic in visual order or in
  presentation forms, and a quote may then not match word for word.
- Arabic in a filled PDF form relies on the viewer redrawing the field (`NeedAppearances`); the stored value is
  exact.
- Deck text sizes are estimated (half an em a character), not rendered. The read-back checks that no two shapes
  overlap and none runs off the slide; it does not inspect glyphs.

## Sample data

Sidra Office Supplies: the Riverside office lease and invoice INV-1043 as PDFs, the invoice again as a scan
(a PNG), an Arabic summary of the lease, the Harbourline proposal in two versions, and the Q3 review deck, in
`samples/` (`samples.yaml`). `tests/make_sample_pdfs.py` writes the PDFs and the scan, `tests/make_sample_decks.py`
the deck.
