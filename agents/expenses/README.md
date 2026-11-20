# Expenses (`expenses`)

A week of travel leaves eleven receipts: PDFs, photos taken at the
table, a till slip in Arabic. Upload them and ask for a claim: each line
shows the receipt text it came from, the two taxi receipts that are the
same ride are flagged, and the hotel line that breaks the policy says
which rule. Submit it, and the approver's decision is recorded under
their name.

Receipts into an expense claim, with every extracted fact checked
against the receipt's own text (for a photo, the text the model read off
it), assumptions kept apart from facts, duplicates matched in code, the
company policy as rules with quoted sources, and a claim status that
becomes approved or paid only on a named reviewer's word.

No external service and no credential.

## Functions

| Function | Level | What it does |
|---|---|---|
| `claims_list` | 0 | The person's claims with their refs: how an existing claim is found |
| `claims_create` | 1 | A draft claim for a claimant, in the currency the person names (never assumed) |
| `claims_get` | 0 | The claim, its receipts, totals by currency, how many need attention; with `rates`, other currencies converted **for information only** |
| `claims_check` | 1 | Duplicates, receipts needing review or entry, other currencies, policy exceptions |
| `claims_workbook` | 1 | Receipts, Summary, Exceptions, Rules applied (and For information with rates): read back, handed back as the call's file |
| `claims_set_status` | 1 | draft → submitted after the person confirms; approved, rejected, paid only with the reviewer's name |
| `claims_delete` | 2 | A draft or rejected claim and its receipt records, after the person confirms; files kept |
| `receipts_read` | 1, uses the chat's model | One receipt (text, PDF, photo, scanned PDF) into a draft claim: merchant, date, currency, amount, category, each verified or assumed |
| `receipts_confirm` | 1 | A person confirms or corrects a receipt; required for manual receipts and anything needing review |
| `receipts_not_duplicate` | 1 | A person's word that a flagged duplicate is a separate expense; never matched to that receipt again |
| `receipts_delete` | 2 | A receipt taken out of a draft claim, after the person confirms; its duplicates released; file kept |
| `policies_load` | 1, uses the chat's model | The known rules out of the policy, read whole (in windows when long, page by page when scanned), each with its sentence and place |
| `policies_set_rule` | 1 | One rule set or corrected by a person |
| `policies_get` | 0 | The rules in force, with their sources |

None is schedulable: reading receipts and policies needs the model, and
the rest is done while a person is there.

Records: `claim`, `receipt`, `rule` (rules are editable on the page).
Files: `receipt_file`, `policy_file` (or a file handed to the call), at
most 8 MiB less 64 KiB; a larger one is refused before it is read. The
workbook goes back as the call's file.

## Photos and scans

A photo (JPEG, PNG, WebP) is turned upright, brought to at most 2,000
pixels a side and under 5 MB, and shown to the chat's model, which
copies the receipt's lines as printed (Arabic stays Arabic). A PDF with
no text layer has its first four pages rendered (pypdfium2) and read
the same way. The fields are then extracted from that text and checked
against it exactly as for a text receipt; the receipt says
`read_from: picture` and keeps the text that was read. A refusal by the
model's provider, or no text on the picture, records the receipt as
`manual`; a reading cut short is said, and the receipt needs review.
HEIC photos are refused with how to send them as JPEG.

## Facts and assumptions

`receipts_read` asks the chat's model for the fields and then checks:

- the **amount** must appear on the receipt as a money figure, or it is
  assumed; if a larger figure is printed, the note says so. Both decimal
  conventions are read (`12,50` and `12.50`, `1.234,56` and
  `1,234.56`), and Arabic-Indic digits with the Arabic decimal mark
  (`١٢٫٥٠`). A figure two conventions could read (`1,234`) is settled by
  the receipt's own other figures or its currency, or refused;
- the **date** must parse (ISO, with an English or Arabic month name,
  or day/month/year when the order is unambiguous) and be on the
  receipt. When the model's date can't be read, the receipt's first date
  is used, always as assumed, and the receipt needs review;
- the **currency** must be shown on the receipt by code or mark
  (`AED`, `د.إ`, `ر.س`, `€`…). A bare `$` is any of several dollars: it
  is never taken as USD; the person states it (`currency` on
  `receipts_read`, or `receipts_confirm`);
- the **merchant** must appear in the text, or it is assumed;
- the **category** is always the model's judgement and is listed as
  assumed.

A receipt with any assumed money fact is `needs_review` and cannot be in
a submitted claim until a person confirms it.

Another currency is claimed as it is and flagged. Given `rates` (one
unit of that currency in the claim's currency, such as
`{"AED": "0.2723"}`), `claims_get` and `claims_workbook` show the
converted totals beside the claim, labelled as information; the claim's
own totals never change.

## Duplicates and policy

A duplicate is the same merchant, date, amount and currency already
recorded, in this claim or any other. The original is the receipt
**added first** (records are listed in the order they were written), not
the one whose ref sorts first. It is flagged on read and again by
`claims_check`. A person who knows better says so with
`receipts_not_duplicate`: the receipt goes back to what it was read as
and the pair is never matched again. Deleting an original releases its
duplicates.

`policies_load` reads six rule names out of the policy: meal allowance
per day, hotel per night, taxi per trip, receipt required above, claim
window in days, alcohol allowed. Each comes with the sentence that
states it, checked against the text, and where it was found (page and
window). A long policy is read whole in windows of 24,000 characters,
cut between lines, up to 20 windows; a longer one is refused with its
size. A scanned policy is read page by page off its pictures, up to 40
pages. Two parts that give different values are reported as a conflict
and set nothing; parts the model refused or cut are named. A rule the
policy does not state is not found, never assumed; a person can set it
with `policies_set_rule`.

`claims_check` applies the meal allowance (per day, in the claim's
currency), hotel per night, taxi per trip, the claim window (against
`as_of`, today in `timezone` by default) and the receipt line (an amount
above it must be shown on the receipt or confirmed by a person), and
names each exception with its figures.

## Status

Preparing a claim proves nothing about reimbursement. Submitting asks
the person first (no answer, nothing changes), and needs receipts with
none still needing review or entry; approved, rejected and paid require
the reviewer's name and are stamped with the date. A submitted, approved
or paid claim and its receipts can't be changed or deleted. The agent
never reports a claim as approved or paid on its own account.

## Sample data

Sidra Office Supplies: Sami's Dubai trip, a claim in draft with five
text receipts (a hotel night, a dinner over the meal allowance, the
same taxi ride twice, and a karak café receipt in Arabic, in dirhams),
and the travel policy in `samples/` with its six rules already loaded
(`samples.yaml`).

## Limits

- Dates with ambiguous day/month order (03/04/2026) are not read.
- Meal allowance is checked per calendar day in the claim's currency;
  other currencies are flagged, not summed against the allowance.
- Rules are one value each; tiered or per-city allowances are not
  modelled.
- A receipt's text beyond 12,000 characters is not read, and says so.
