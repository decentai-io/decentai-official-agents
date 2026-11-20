# DecentAI Agents

General-purpose agents for DecentAI: the accounts you already work in,
the files you hand over, and the things you keep. Each one is attached to
something a person already has, so it answers a question on the first
afternoon, and each is written the way the platform wants agents written.

DecentAI ships with no agents. This repository builds a catalog: a
`catalog.json`, and for each agent its manifest, a program for each machine,
its sample data and its readme. A deployment adds the catalog's address as an
agent source, a person reviews one agent and installs it, and the deployment
keeps that release by its digests. Reading a source runs nothing.

## The catalog

**Accounts you connect** — sign in once; the platform keeps the tokens, refreshes them, and hands the agent a fresh
access token when it reads the account. Every Google agent asks for the same permissions, and so does every Microsoft
agent, so one connected account serves them all. Several accounts may be connected; every function takes the one it
works on by its address.

*Mail, calendars, tasks and chat*

| | |
|---|---|
| [`outlook/`](agents/outlook/) | Mail, calendar and To Do of a Microsoft 365 or Outlook.com account: whole conversations, attachments in and out, replies that keep the history, sent on your word; meetings found, booked and answered; tasks kept in your zone; watches for a reply, new mail and finished tasks |
| [`gmail/`](agents/gmail/) | The same for Gmail: search, whole threads in any charset, drafts with your files attached, reply-all, forward, labels, archive; sent on your word; a reply reminder and an inbox watch that never skips mail |
| [`google_calendar/`](agents/google_calendar/) | Google Calendar and Google Tasks: what is on, who is free, bookings with a Meet link that wait for your go-ahead, invitations answered, and what was done |
| [`teams/`](agents/teams/) | Microsoft Teams, work and school accounts: catch up on chats, what you missed, send or post on your go-ahead, with @mentions; a watch for new messages |
| [`slack/`](agents/slack/) | The same for Slack, as you: conversations, search, a direct message by name or address, reactions, files; a watch for mentions and direct messages |
| [`mail/`](agents/mail/) | Any mail account, connected with its address and an app password and nothing registered with a provider: Yahoo, iCloud, Fastmail or your own server. Folders, whole conversations, attachments in and out, reply-all, forward, sent on approval, and the same watches. Over IMAP and SMTP, through the sandbox's way out |

*Files, documents, spreadsheets and forms*

| | |
|---|---|
| [`onedrive/`](agents/onedrive/) | OneDrive, work or personal, with the Word, Excel and Forms files in it: find, bring in, upload, file, share, restore; Word edited as a proposal you apply; Excel read and written; Forms' responses; watches for saves and new rows |
| [`google_drive/`](agents/google_drive/) | Drive and shared drives with the Docs, Sheets and Forms in them: find, bring in, file and share; edit docs, write sheets, count form answers; watches |
| [`dropbox/`](agents/dropbox/) | Dropbox, personal or team space: find, bring in, upload, file, share, restore; a folder watched so a schedule hears only what changed |
| [`notion/`](agents/notion/) | Pages and databases the person shared: read, a row written against the database's own schema, and changes watched |
| [`todoist/`](agents/todoist/) | Your Todoist: what is due, a task filed where you said or moved, and what you ticked off carried into your other lists |

**Services on the organization's key** — an administrator pastes the key once.

| | |
|---|---|
| [`places/`](agents/places/) | Find a place, read its hours and phone, and see how far and how long a trip is, transit included — from Google Maps, asking only for what each answer needs |

**Files you hand over** — read with page references and quoted evidence, in Arabic as in English.

| | |
|---|---|
| [`documents/`](agents/documents/) | PDFs, Word files, decks and scans: read to the end with page references, scans and photos read off the picture, fields extracted with the sentence behind each; Word, PDF (Arabic too) and PowerPoint produced; PDFs merged, split, rotated and their forms filled |
| [`sheets/`](agents/sheets/) | CSV, Excel and JSON: inspect, reconcile, clean, convert, pivot and compare, the decimal comma and Arabic encodings understood, with a log of every change |
| [`expenses/`](agents/expenses/) | Receipts, photos included, into an itemized total you can defend: duplicates caught in code, your policy checked if you have one |

**The public web** — only public addresses are ever fetched; nothing inside the network the platform runs in. A page
built by JavaScript or behind a sign-in is the [Browser](https://github.com/decentai-io/decentai-browser-agent)'s.

| | |
|---|---|
| [`web_reader/`](agents/web_reader/) | A web page or online PDF read with the Documents rules: quoted, with its section or page, never invented; saved for another agent to read |
| [`web_watch/`](agents/web_watch/) | Pages and feeds watched for the change that matters — a price below a threshold, a phrase, a part of a page, new feed items — reported once, on a schedule |

**Work nothing else covers** — each step is yours to see, and to stop.

| | |
|---|---|
| [`code_runner/`](agents/code_runner/) | A Python program written for what you ask and run once you allow it: you see the program first, with the packages it installs, the sites it reaches, the credentials it uses and the files it reads, and it gets those and nothing else. The common data packages are carried, so most programs reach nothing |

**What you keep**

| | |
|---|---|
| [`tasks/`](agents/tasks/) | What you have to do, repeating or not, and everything that renews or expires — the notice deadline, not the renewal date |
| [`timesheets/`](agents/timesheets/) | Where the week went, by project — a timer, meetings placed by your own rules, gaps found, locked when handed in |

### Examples: how an agent is made

A second catalog, `catalog/examples.json`, holds agents written to show how one is made, for developers and
administrators to add as a source of its own. They are not in the catalog people browse.

| | |
|---|---|
| [`note/`](agents/note/) | Notes and decisions, kept and findable — and every feature the platform enforces, in one agent: the example the others are written from. Its sync is simulated, so it connects to nothing |
| [`applications/`](agents/applications/) | A guided application on a form the agent carries: one question at a time, every answer and document checked as it arrives. Submitting sends it nowhere |
| [`connection_check/`](agents/connection_check/) | What an agent can reach from where it runs: the one host it declared, one it did not, an address inside the network and a way around the platform, each tried and told apart as reached, refused or unreachable |

`agents/` holds the code. `catalog/` holds what a deployment installs, built from it and never edited by hand.
Beside them: [`standard/`](standard/), this repository's own small library for the agents standard, carried inside
every program; [`publish/`](publish/), which builds the catalog; and [`tests/`](tests/), where every agent's program
runs as a process, spoken to in the standard's messages.

## The standard

Every agent here is written to the platform's agents standard, schema 3
([`STANDARD.md`](STANDARD.md)): a manifest,
and a program that reads and writes JSON messages on its standard input and output. The platform has no SDK.
`standard/` is this publisher's own code on top of those messages, so the agents keep one shape:

| | |
| --- | --- |
| `agent.py` | `AgentBase`, `ToolBase`, `ResourceDenied`: an agent is tools; a function's id is its tool's id and its method's name (`notes_save` is `NotesTool.save`) |
| `call.py` | `Call`: a function's inputs, and within it the runtime's functions — `call.records` (`record.*`), `call.model`, `call.say` and `call.ask` (`chat`), `call.open` (`network.open`), `call.screen` (`note`); files in as paths in the home (`call.file`), out with `call.give` |
| `program.py` | `Program`: `ready`, every call run on the agent's own loop together, a cancel answered at once |
| `net.py` | `Tunnel`, `TunnelRefused`, `BaseAddress`: connections through the sandbox's way out |
| `http.py`, `errors.py` | `Http`, `HttpError`: a service's API spoken off the agent's loop; a busy service waited out (`Retry-After`), a write never sent twice, every refusal with a kind |
| `graph.py`, `google.py` | `Graph`, `Google`: Microsoft's and Google's own ways — ids, the account's every address, rate limits, page tokens, shared drives |
| `accounts.py` | `account`: which of several connected accounts a call works on |
| `watch.py` | `check`, `Seen`: watches that hand on what is new once — one failing keeps its place while the others go on, a gone target closes its watch, and every watching agent can list and close its watches |

## What is in an agent's folder

| | |
| --- | --- |
| `manifest.yaml` | what the agent declares: the contract a deployment reviews and holds it to |
| `requirements.txt` | the dependencies it carries, each pinned, the ones its code imports and the ones they bring |
| `agent.py`, `tools/` | its code: `agents.<id>`, importing `standard` |
| `README.md` | what it does and what it won't, for the person reviewing it: the catalog's `readme` |
| `samples.yaml`, `samples/` | sample data, where it has any: zipped into the catalog's `samples` |

## Build it

```bash
publish/build.sh            # every agent; or name some: publish/build.sh note gmail
```

It exports the working tree cleanly (what git tracks or would track) and builds in Docker, on the sandbox's own base
image, pinned by digest. Each agent gets a program for each machine, a zip with `start` at its root that the
sandbox runs on its base `python-3.12`: the manifest as JSON, `standard/`, the agent's code under `agents/<id>/`, and
its pinned wheels unpacked under `site/`, a native wheel of that machine's. The same files make the same bytes; a
build that would change a published version's bytes refuses.

```text
catalog/
  catalog.json
  note/2.0.0/x86_64.zip
  note/2.0.0/aarch64.zip
  note/2.0.0/samples.zip
  ...
```

Commit `catalog/` with the change. A folder per version means nothing is overwritten: keep the folders of earlier
versions, since a deployment that installed one may read it again.

**Every change is a new version.** A version never changes: the platform refuses a different manifest or program
under one it has seen. Raise `agent.version` with any change, to the code, the manifest or `requirements.txt`.

## Install it

1. In DecentAI, an administrator adds this catalog's address as an agent source, with no credential:
   `https://raw.githubusercontent.com/decentai-io/decentai-official-agents/main/catalog/catalog.json`. The browser agent
   has a catalog of its own: [DecentAI Browser](https://github.com/decentai-io/decentai-browser-agent).
2. A person reviews one agent: its functions, levels, calls, scopes, secrets, data, files, and where it connects.
3. Install. The deployment downloads its programs, checks each against its digest, and a runtime starts it once
   with no network and waits for a `ready` that matches the manifest.

The examples are a source of their own, for those who want them:
`https://raw.githubusercontent.com/decentai-io/decentai-official-agents/main/catalog/examples.json`.

**Coming from the first catalog (November 2026).** Thirty-seven agents became twenty, and their versions say so:
Outlook took in Microsoft Calendar and To Do; OneDrive took in Word, Excel and Microsoft Forms; Google Calendar took
in Google Tasks; Google Drive took in Google Docs, Sheets and Forms; Documents took in Slides; Sheets took in JSON
Data; Web Watch took in Feeds. Box and Notebook are gone. An agent taken in stays installed until an administrator
removes it, and the watches it kept stay with it: make them again with the agent that took it in. A Microsoft
account connected before needs one reconnect (the calendar's time zone needs `MailboxSettings.Read`), and so does a
Slack account (direct messages, reactions and files).

## Sample data

An agent that keeps records may ship `samples.yaml` beside its manifest: records and files that let a person try it
before they have their own, loaded as the person's own and removed in one step. Every sheet but Note's tells the
same story, **Sidra Office Supplies**, so the lease Tasks records is the lease Documents ships to read.

```yaml
story: One line saying whose data this is.
records:
  - ref: lease
    resource: agreement            # a resource the manifest declares
    fields: {name: Office lease, counterparty: Riverside Estates, kind: lease, ...}
  - resource: clause
    fields: {agreement_ref: "@lease", topic: notice, ...}
files:
  - {ref: handover, resource: document, path: samples/handover-notes.csv}
```

A field whose whole value is `@ref` becomes the id the row of that ref was given. Rows are checked against the
manifest here (`tests/test_samples.py`) and again by the platform when they load. The PDFs under
`documents/samples/` are made by `tests/make_sample_pdfs.py`, the deck under `documents/samples/` by
`tests/make_sample_decks.py`.

## Run the tests

Each agent's program runs as a process of its own, started at its first call in a home of its own, as a sandbox
starts it, and spoken to in the standard's messages ([`tests/harness.py`](tests/harness.py)): its inputs and outputs
held to their schemas, each call it makes held to what its function declares in `calls`, `record.*` answered from
records kept in memory, `model` and `chat` by the test. Its connections go through a way out of the test's own that
opens only the hosts it may reach; every provider is a stub under `tests/`. Every manifest is also checked with the
platform's own checks where a checkout of the platform sits beside this one (as `decentai-engine`); the platform is
closed source, so elsewhere those checks are skipped and the rest run.

```bash
docker run -d --name agents-test -v "$PWD:/w" python:3.12-slim sleep infinity
docker exec agents-test sh -c '
  cat /w/decentai-official-agents/agents/*/requirements.txt | sort -u > /tmp/pins.txt
  pip install -r /w/decentai-official-agents/tests/requirements.txt -r /tmp/pins.txt
  cd /w/decentai-official-agents && python -m pytest tests -q -n 6 --timeout=300'
```
