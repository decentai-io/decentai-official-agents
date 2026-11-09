# Web Reader (`web_reader`)

Someone pastes a link to a supplier's terms page and asks whether it
says anything about refunds. The answer comes back with the section it
was read from and the sentence itself — or a plain "the page does not
mention refunds".

Reads a public web page, an online PDF, or a text or JSON URL with the
same rules as Documents: the text in numbered parts with totals, the
passages that contain a phrase with their section or page, and a copy
handed to the chat as a file that Documents (or any agent) can read by
`file_ref`.

No credential, no records. Every call fetches afresh.

## Functions

| Function | Level | What it does |
|---|---|---|
| `pages_read` | 0 | Final URL after redirects, title, content type and the readable text in numbered parts — sections of a web page (split at headings, a long section split further), pages of a PDF, parts of plain text — from `from`, up to `max_chars`, with `total_parts`, `total_chars` and `next`. Links found, as text and absolute URL (first 25, with the total). PDF pages with no text layer are listed |
| `pages_find` | 0 | One phrase or up to ten, matched regardless of case and line breaks. Each match with its part number, heading and a short quote; `absent` lists the phrases the page does not contain |
| `pages_save` | 2 | A PDF as its original bytes; a web page as Markdown of its readable text, other text as `.txt`, each headed with `Source:` and `Fetched:`. The fetch reads at most 5 MB, as every fetch does: a larger PDF is refused as `too_large`, and a larger web page is saved from its first 5 MB with a note saying so |

The copy is the chat's file (PDF, Markdown or plain text; at most 25 MB,
what the platform carries); the agent keeps no resource.

## How a page is read

| Kind | Read as |
|---|---|
| HTML | Standard-library parser. Dropped: `script`, `style`, `noscript`, `template`, `svg`, `nav`, `footer`, `aside`, `iframe`, `button`, `select`, anything `hidden` or `aria-hidden`, and elements with role navigation, banner, contentinfo, complementary or search. Headings become `# …` lines and start a section; list items keep a dash; table cells are separated by `|` |
| PDF | pypdf, page by page; the document title from its metadata |
| Text, JSON, XML | Passed through, split into parts of at most 4,000 characters at line boundaries |
| Anything else (images, archives) | Refused as unsupported, naming the content type |

The charset is the one the server names, else the one the page
declares, else UTF-8.

A fetch that does not produce a page fails with a `kind`:
`invalid_url`, `forbidden` (an address that is not public),
`too_many_redirects`, `timeout`, `network`, `http`, `too_large` or
`unsupported`.

## Safety: only public addresses

An assistant that fetches whatever URL it is given can be aimed at the
network it runs in. Every fetch (`tools/fetch.py`):

- accepts only `http` and `https`, and no user name or password in the URL;
- resolves the host and refuses it if **any** address is loopback,
  private, link-local (including `169.254.169.254`), multicast,
  reserved, unspecified or otherwise not globally routable — IPv4, IPv6
  and IPv4-mapped IPv6 — and says which;
- checks the address the socket actually connected to as well, so a
  name that resolves differently the second time (DNS rebinding) is
  still refused;
- follows at most 5 redirects itself, checking every hop;
- reads at most 5 MB (the rest is not read, and `body_truncated` says
  so), with a 10 s connect timeout, a 20 s read timeout and 45 s for
  the whole body;
- ignores proxy settings in the environment (a proxy would be the
  connected address, not the target), except the sandbox's way out;
- sends `User-Agent: DecentAI-WebReader/0.1 (…)`.

**Behind the platform's way out.** In the sandbox the agent's only way
out is a proxy named in `HTTPS_PROXY`. The way out resolves the name
and checks the addresses itself, so the agent leaves the name to it and
asks it for a tunnel (HTTP `CONNECT`) for `http://` as for `https://`:
a refusal is then always the way out's, never a site's. The agent still
checks the scheme, the credentials, an address written as an address,
and every redirect hop; a host the way out refuses (403) is reported as
`forbidden`, one it could not reach as `network`, with its words.

**Test-only:** `DECENTAI_WEB_ALLOW_LOOPBACK=1` permits loopback
(127.0.0.0/8, `::1`) and nothing else — never a private range — so the
tests can serve pages from a loopback stub. A deployment must never set
it; the tests prove a private address is refused even with it set, and
loopback without it.

## Limits, and what is not verified

- No JavaScript runs. A page that builds its text in the browser reads
  as nearly empty; `note` says so when a web page yields under 200
  characters.
- Boilerplate removal is by element and role, not by layout: a site
  whose menus are plain `div`s keeps them.
- A scanned PDF page has no text layer and is listed in
  `unreadable_pages`. A PDF over 5 MB is not read at all.
- Pages behind a login, a cookie wall or bot protection read as what
  that wall says.
- The address rules rely on the resolver the program uses, or on the
  platform's way out; a site reachable only through some other proxy
  cannot be fetched.
- Tested against a loopback server with hand-written HTML, PDFs and
  redirects. Real sites on the internet, TLS and IPv6 connectivity have
  not been exercised by the tests.
