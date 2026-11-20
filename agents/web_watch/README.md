# Web Watch (`web_watch`)

A desk lamp is on the wish list at USD 129. Ask to be told when it
drops below 100, and the assistant hears about it the morning it does —
once, with the old price, the new one and the difference worked out —
and not on the mornings it did not. The same agent follows a blog's
feed and says what was published since yesterday.

Version 2.0.0 absorbs the former Feeds agent. Watches and feed
subscriptions are one kind of record, checked by one schedulable
function. Feed subscriptions kept by Feeds 1.x do not move by
themselves: follow those feeds again (or import them from OPML).

No credential.

## Kinds of watch

| Kind | Reports when | Value |
|---|---|---|
| `page` | the readable text changes (whitespace aside) | an excerpt; a line diff |
| `selector` | the text of the part a CSS selector picks changes, or the part disappears | an excerpt; a line diff |
| `contains` | a phrase appears or disappears (case and line breaks ignored) | `present` / `absent`, with a quote |
| `pattern` | the first match of a regular expression changes | its first group, or the whole match |
| `number` | a number crosses `above` or `below`, moves by `change_percent` since last reported, or (with none of those) changes | the number in plain digits, as written, the difference and the percentage |
| `feed` | an RSS 2.0, RSS 1.0 (RDF), Atom or JSON Feed has items never seen before, filtered by `keywords` and `exclude` | the items, oldest first |

`contains`, `pattern` and `number` can be narrowed to one part of the
page with `selector`. A number is found after a phrase (`phrase`, such
as `Price:`), by a pattern, or in a selector's part. It is read in
code: `1,299.00`, `1.299,00`, `1 299,50`, `1'299.00` and Arabic-Indic
`١٬٢٩٩٫٥٠` are all 1299 and 1299.5; `decimal_comma` settles `1.299`.
Comparisons and sums are exact decimals.

Pages with rotating content (dates, counters, adverts) change on every
check as `page` watches: use `selector`, `contains`, `pattern` or
`number` for them.

## Functions

| Function | Level | What it does |
|---|---|---|
| `watches_add` | 1 | Checks the phrase, pattern or selector (an invalid one is refused with why), takes the first reading now and keeps it. Refuses a page that needs JavaScript, a bot check or a sign-in, pointing to the Browser agent. Adding the same watch twice returns the first |
| `watches_list` | 0 | The active watches (closed ones with `include_closed`): what each watches, last read, last change, failures in a row |
| `watches_close` | 1 | Stops a watch for good; its record stays, closed |
| `watches_remove` | 2 | Deletes a watch |
| `watches_check_now` | 0 | What a scheduled check would report now, **storing nothing** |
| `watches_changed` | 1, schedulable | Every active watch (or one): `changed`, `items`, `problems`, `closed`, `more` |
| `feeds_read` | 0 | A feed's latest items without watching it, optionally only those with keywords |
| `feeds_import` | 1 | An OPML file (or its text) becomes feed watches, up to 200; ones already followed and addresses that could never be fetched are skipped and named |

Records: `watch`. What is filtered or listed (status, kind, URL, label,
dates, failures) is in `keys`; what the page says, the pattern, the
snapshot and the feed's seen ids are in `values`.

## On a schedule: each change once, none lost

`watches_changed` runs through the shared `standard.watch`:

- every watch is read on its own, six at a time; one that fails (an
  error status, a timeout, a refused address, a bot check, a pattern too
  slow) is listed in `problems` with its failures in a row, and keeps
  its last reading, so what it would have said comes when it can be
  read again;
- readings are written only after every watch was read, with what they
  hand on;
- a page answering 410, or 404 five checks in a row, closes its watch,
  listed in `closed`;
- runs in one process take turns, so two overlapping runs never hand the
  same change on twice;
- a run stops starting new reads after 170 seconds; `more` says the next
  run goes on, least recently tried first.

**Feeds** are read by item id, not by date: an item that was never seen
is news even when it is dated before the newest one (news sites and
GitHub do this). Up to 1,500 seen ids are kept per feed (12 characters
each). At most `max_items` items a feed a run (20 by default), oldest
first; the rest are the next run's. A feed followed from OPML takes its
first look on the first run and hands on only what comes after.

Feeds are found from a site's address: the feed the page advertises
(`<link rel="alternate">`), else `/feed` beside the page, `/feed`,
`/rss.xml`, `/atom.xml`, `/feed.xml`, `/index.xml`, `/rss` and
`/feed.json` at the root. The XML is read leniently (a BOM,
`text/xml`, any charset the document declares, HTML entities such as
`&nbsp;`, a bare `&`) but a document that declares entities is refused
unread, so entity expansion never reaches the parser.

**Regular expressions** run on the `regex` module with a two-second
limit, in a thread: one that backtracks badly is refused at `watches_add`
and is a `too_slow` problem at a check, never a hung agent.

**Diffs** are made from a compressed snapshot of the text (up to 76,000
characters of compressed text over four fields, typically a few hundred
thousand characters of page). They list 60 lines or 6,000 characters at
most, with `diff_truncated` and the whole `lines_added` and
`lines_removed`; a page too long for its snapshot says so in `note`.

## Safety: only public addresses

The same `tools/fetch.py` as Web Reader (a test fails if the copies
differ): public addresses only, every redirect hop checked, the
connected address checked, at most 8 MiB less 64 KiB, fetched in a
thread, the sandbox's way out used by name. See
[the Web Reader's README](../web_reader/README.md#safety-only-public-addresses).

## What it will not do

- Watch a page that needs JavaScript, a sign-in or a bot check: the
  Browser agent can read such a page; a schedulable browser watch is
  not here yet.
- Tell a model anything to decide: the schedulable function calls none.
- Tested against a loopback server whose pages and feeds the tests
  change; real sites and feeds are for the first live run.
