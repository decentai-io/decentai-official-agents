# Expenses (`expenses`)

A week of travel leaves eleven receipts as PDFs. Upload them and ask for
a claim: each line shows the receipt text it came from, the two taxi
receipts that are the same ride are flagged, and the hotel line that
breaks the policy says which rule. Submit it, and the approver's
decision is recorded under their name.

Receipts into an expense claim, with every extracted fact checked
against the receipt's own text, assumptions kept apart from facts,
duplicates matched in code, the company policy as rules with quoted
sources, and a claim status that becomes approved or paid only on a
named reviewer's word.

No external service and no credential.

## Functions

| Function | Level | What it does |
|---|---|---|
| `claims_list` | 0 | The person's claims with their refs — how an existing claim is found |
| `claims_create` | 1 | A draft claim for a claimant, USD unless said otherwise |
| `claims_get` | 0 | The claim, its receipts, totals by currency, how many need attention |
| `claims_check` | 1 | Duplicates, receipts needing review or entry, other currencies, policy exceptions |
| `claims_workbook` | 2 | Receipts, Summary, Exceptions, Rules applied — read back, and handed back as the call's file |
| `claims_set_status` | 1 | draft → submitted on the claimant's word; approved, rejected, paid only with the reviewer's name |
| `receipts_read` | 1, uses the chat's model | One receipt file (PDF, text or photo) into a draft claim: merchant, date, currency, amount, category — verified or assumed |
| `receipts_confirm` | 1 | A person confirms or corrects a receipt; required for photos and anything needing review |
| `policies_load` | 1, uses the chat's model | The known rules out of the policy document, each with its sentence, checked verbatim |
| `policies_set_rule` | 1 | One rule set or corrected by a person |
| `policies_get` | 0 | The rules in force |

Records: `claim`, `receipt`, `rule` (rules are editable on the page).
Files: `receipt_file`, `policy_file` (or a file handed to the call); the
workbook goes back as the call's file.

## Facts and assumptions

`receipts_read` asks the chat's model for the fields and then checks:

- the **amount** must appear on the receipt as a money figure, or it
  is assumed; if a larger figure is printed, the note says so;
- the **date** must parse — ISO, written with a month name, or
  day/month/year when the order is unambiguous — or it is assumed;
- the **currency** must be shown on the receipt by code or symbol, or
  it is assumed; a receipt showing exactly one currency is taken as
  that currency;
- the **merchant** must appear in the text, or it is assumed;
- the **category** is always the model's judgement and is listed as
  assumed.

A receipt with any assumed money fact is `needs_review` and cannot be
in a submitted claim until a person confirms it. A photo (jpeg, png)
or a scanned PDF has no text this agent can read: it is recorded as
`manual` with the file kept, for `receipts_confirm` to fill.

Another currency is claimed as it is and flagged; nothing is
converted.

## Duplicates and policy

A duplicate is the same merchant, date and amount already recorded —
in this claim or any other. It is flagged on read and again by
`claims_check`, which marks it `duplicate`.

`policies_load` reads six rule names out of the policy document —
meal allowance per day, hotel per night, taxi per trip, receipt
required above, claim window in days, alcohol allowed — each with the
sentence that states it, verified against the text. A rule the policy
does not state is reported as not found and never assumed; a person
can set it with `policies_set_rule`. `claims_check` applies the meal
allowance (per day, in the claim's currency), hotel per night (to
accommodation lines), taxi per trip (to transport lines) and the claim
window (against `as_of`, today by default), and names each exception
with its figures. `receipt_required_above` and `alcohol_allowed` are
listed under the rules applied but raise no exception.

## Status

Preparing a claim proves nothing about reimbursement. `set_status`
lets the claimant submit a claim that has receipts and none still
needing review or entry; approved, rejected and paid require the
reviewer's name and are stamped with the date. The agent never reports a claim as
approved or paid on its own account.

## Sample data

Sidra Office Supplies: Sami's Dubai trip, a claim in draft with four
text receipts (a hotel night, a dinner over the meal allowance, and the
same taxi ride twice), and the travel policy in `samples/` with its six
rules already loaded (`samples.yaml`).

## Limits

- No OCR and no image reading; photos are manual entry.
- Dates with ambiguous day/month order (03/04/2026) are not read.
- Meal allowance is checked per calendar day in the claim's currency;
  other currencies are flagged, not summed against the allowance.
- Rules are one value each; tiered or per-city allowances are not
  modelled.
