# Web Reader (`web_reader`)

Someone pastes a link to a supplier's terms page and asks whether it
says anything about refunds. The answer comes back with the section it
was read from and the sentence itself — or a plain "the page does not
mention refunds".

Reads a public web page, an online PDF, or a text or JSON URL with the
same rules as Documents: the text in numbered parts with totals, the
passages that contain a phrase with their section or page, and a copy
handed to the chat as a file that Documents (or any agent) can read by
`file_ref`. It is the Browser agent's cheap, quotable fast path: when a
page needs a real browser, it says so (`needs_browser`) and its
instructions hand the page to the Browser agent's `browse_run`.

No credential, no records. Every call fetches afresh.

## Functions

| Function | Level | What it does |
|---|---|---|
| `pages_read` | 0 | Final URL after redirects, title, content type, charset, and the readable text in numbered parts — sections of a web page (split at headings, a long section split further), pages of a PDF, parts of plain text — from `from`, up to `max_chars`, with `total_parts`, `total_chars` and `next`. Links (first 25, with the total). PDF pages with no text layer are listed. `needs_browser` and `browser_reason` when a plain fetch cannot see the page |
| `pages_find` | 0 | One phrase or up to ten, matched regardless of case and line breaks. Each match with its part number, heading and a short quote; `absent` lists the phrases not found. A long PDF is searched 400 pages a call from `from`; `searched` and `next` say which pages were looked at |
| `pages_save` | 0 | A PDF as its original bytes; a web page as Markdown of its readable text, other text as `.txt`, each headed with `Source:` and `Fetched:`. At most 8 MiB less 64 KiB, a chat file's most |

## How a page is read

| Kind | Read as |
|---|---|
| HTML | Standard-library parser. Dropped: `script`, `style`, `noscript`, `template`, `svg`, `nav`, `footer`, `aside`, `iframe`, `button`, `select`, anything `hidden` or `aria-hidden`, and elements with role navigation, banner, contentinfo, complementary or search. Headings become `# …` lines and start a section; list items keep a dash; table cells are separated by `|` |
| PDF | pypdf, page by page, at most 400 pages and 40 seconds a call; the page count is always the PDF's own, and the pages not read in this call are named with `next`. A page longer than 100,000 characters is clipped and flagged (`clipped`, `chars`) |
| Text, JSON, XML | Passed through, split into parts of at most 4,000 characters at line boundaries |
| Anything else | Refused as unsupported, naming the content type |

**Charsets**, as a browser chooses them: a byte-order mark, then the
server's charset, then the page's own (`<meta charset>`, `http-equiv`,
an XML declaration), then UTF-8, then a guess (windows-1252 on a near
tie). Arabic pages in windows-1256 and UTF-8 are read either way.

**What only a browser sees** (`needs_browser`, with `browser_reason`):

- `javascript`: a page that runs scripts and has under 200 characters
  of text, or a large page of scripts with few words, or a `<noscript>`
  saying the page needs JavaScript;
- `blocked`: a bot check (Cloudflare, DataDome, PerimeterX, Imperva,
  Akamai, AWS WAF, DDoS-Guard, Sucuri, a captcha), whether it answers
  200, 403, 429 or 503;
- `sign_in`: a sign-in form (a password field on a thin or login page),
  or HTTP 401.

A fetch that does not produce a page fails with a `kind`:
`invalid_url`, `forbidden` (an address that is not public),
`too_many_redirects`, `timeout`, `network`, `http`, `too_large`,
`blocked`, `sign_in` or `unsupported`.

## Safety: only public addresses

Every fetch (`tools/fetch.py`, the same file in Web Watch, checked by a
test):

- accepts only `http` and `https`, and no user name or password in the URL;
- resolves the host and refuses it if **any** address is loopback,
  private, link-local (including `169.254.169.254`), multicast,
  reserved, unspecified or otherwise not globally routable — IPv4, IPv6
  and IPv4-mapped IPv6 — and says which;
- checks the address the socket actually connected to as well (DNS
  rebinding);
- follows at most 5 redirects itself, checking every hop;
- reads at most 8 MiB less 64 KiB; a larger PDF is refused before its
  bytes are read when the server gives its size, with the size and the
  link; a larger web page is read in its first part (`body_truncated`);
- times out after 10 s to connect, 20 s between reads and 45 s in all;
- runs in a thread, off the agent's loop, so one slow site never holds
  up the chat's other calls;
- ignores proxy settings in the environment, except the sandbox's way
  out (`HTTPS_PROXY`), which it asks for a tunnel by name;
- sends `User-Agent: DecentAI-WebReader/1.1 (…)`.

**Test-only:** `DECENTAI_WEB_ALLOW_LOOPBACK=1` permits loopback and
nothing else. A deployment must never set it.

## What it will not do

- Run JavaScript, sign in, or pass a bot check: it says so and leaves
  the page to the Browser agent.
- Read a scanned page (no OCR): it is listed in `unreadable_pages`.
- Read a PDF over 8 MiB less 64 KiB, or an encrypted one.
- Remove boilerplate by layout: a site whose menus are plain `div`s
  keeps them.
- Tested against a loopback server with hand-written pages, PDFs and
  redirects; real sites, TLS and IPv6 are for the first live run.
