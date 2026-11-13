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

**Accounts you connect** — sign in once; the platform keeps the tokens,
refreshes them, and hands the agent a fresh access token when it reads the
account. Every Google agent asks for the same permissions, and so does every
Microsoft agent.

*Mail, calendars and chat*

| | |
|---|---|
| [`gmail/`](agents/gmail/) | Find, read, draft and, on your word, send. Reminders that fire only while a reply is missing, and an inbox watched so a schedule hears only new mail |
| [`outlook/`](agents/outlook/) | The same for a Microsoft 365 mailbox, through Microsoft Graph |
| [`google_calendar/`](agents/google_calendar/) | What is on, who is free, and a booking that waits for your go-ahead |
| [`microsoft_calendar/`](agents/microsoft_calendar/) | The same for a Microsoft 365 calendar |
| [`teams/`](agents/teams/) | Catch up on a chat, see whether someone answered; send or post on your go-ahead |
| [`slack/`](agents/slack/) | The same for Slack, as you, with a watch for a mention or a direct message |
| [`mail/`](agents/mail/) | Any mail account, connected with its address and an app password and nothing registered with a provider: find, read a conversation, save an attachment, draft, send on approval, and the same two watches. Over IMAP and SMTP, through the sandbox's way out |

*Files and storage*

| | |
|---|---|
| [`google_drive/`](agents/google_drive/) | Find a file, see who can open it, bring a copy in; file and share on your go-ahead |
| [`onedrive/`](agents/onedrive/) | The same for a Microsoft 365 OneDrive |
| [`dropbox/`](agents/dropbox/) | The same for a Dropbox, and a folder watched so a schedule hears only what changed |
| [`box/`](agents/box/) | The same for a Box account, with its change stream watched the same way |

*Live documents, spreadsheets and forms*

| | |
|---|---|
| [`google_sheets/`](agents/google_sheets/) | A live Google spreadsheet read, written on your go-ahead, and watched for new rows |
| [`excel_online/`](agents/excel_online/) | The same for an Excel workbook in OneDrive, each write one workbook session |
| [`google_docs/`](agents/google_docs/) | A Google Doc read by paragraph, commented on, and edited only as a proposal you apply — refused if the doc changed since |
| [`word_online/`](agents/word_online/) | A Word document in OneDrive, read from the file itself, its comments listed, and edited only as a proposal you apply; with a watch for saves and new comments. It does not add comments |
| [`google_forms/`](agents/google_forms/) | What a Google Form asks, its responses, and a watch for new answers |
| [`microsoft_forms/`](agents/microsoft_forms/) | A Microsoft Form's responses, read from the Excel workbook they sync to — Microsoft offers no Forms API |
| [`notion/`](agents/notion/) | Pages and databases the person shared: read, a row written against the database's own schema, and changes watched |

*Tasks elsewhere*

| | |
|---|---|
| [`microsoft_todo/`](agents/microsoft_todo/) | Your To Do lists: what is open and due, tasks added and ticked off, and what you completed there brought back to Tasks |
| [`google_tasks/`](agents/google_tasks/) | The same for Google Tasks |
| [`todoist/`](agents/todoist/) | Your Todoist: what is due, a task filed where you said, and what you ticked off carried into your other lists |

**Services on the organization's key** — an administrator pastes the key once.

| | |
|---|---|
| [`places/`](agents/places/) | Find a place, read its hours and phone, and see how far and how long a trip is — from Google Maps |

**Files you hand over** — read with page references and quoted evidence.

| | |
|---|---|
| [`documents/`](agents/documents/) | PDFs, Word files and text read with page references; fields extracted with the sentence behind each; Word and PDF produced |
| [`sheets/`](agents/sheets/) | Inspect, reconcile and clean CSV and Excel files, with a log of every change |
| [`slides/`](agents/slides/) | PowerPoint decks read slide by slide with their speaker notes; new decks produced from an outline, in your own template |
| [`json_data/`](agents/json_data/) | JSON and JSON Lines: what is actually in a file, values by path, a schema checked, two versions compared, a CSV out |
| [`expenses/`](agents/expenses/) | Receipts into an itemized total you can defend: duplicates caught in code, your policy checked if you have one |

**The public web** — only public addresses are ever fetched; nothing inside the network the platform runs in.

| | |
|---|---|
| [`web_reader/`](agents/web_reader/) | A web page or online PDF read with the Documents rules: quoted, with its section or page, never invented; saved for another agent to read |
| [`web_watch/`](agents/web_watch/) | A page watched for the change that matters — a price, a phrase, any edit — reported once, on a schedule |
| [`feeds/`](agents/feeds/) | RSS and Atom subscriptions, found from a site's address; what was published since the last look, exactly once |

**Work nothing else covers** — each step is yours to see, and to stop.

| | |
|---|---|
| [`code_runner/`](agents/code_runner/) | A Python program written for what you ask and run once you allow it: you see the program first, with the packages it installs, the sites it reaches, the credentials it uses and the files it reads, and it gets those and nothing else |

**Checking the platform**

| | |
|---|---|
| [`connection_check/`](agents/connection_check/) | What an agent can reach from where it runs: the one host it declared, one it did not, an address inside the network and a way around the platform, each tried and told apart as reached, refused or unreachable |

**What you keep**

| | |
|---|---|
| [`tasks/`](agents/tasks/) | What you have to do, and everything that renews or expires — the notice deadline, not the renewal date |
| [`timesheets/`](agents/timesheets/) | Where the week went, by project — meetings placed by your own rules, locked when handed in |
| [`note/`](agents/note/) | Notes and decisions, kept and findable — and every feature the platform enforces, in one agent: the example the others are written from. Its sync is simulated, so it connects to nothing |
| [`notebook/`](agents/notebook/) | Note in an earlier form, which keeps a note's content encrypted in `values` |
| [`applications/`](agents/applications/) | A guided application on a form the agent carries: one question at a time, every answer and document checked as it arrives, submitted on your word |

`agents/` holds the code. `catalog/` holds what a deployment installs, built from it and never edited by hand.
Beside them: [`standard/`](standard/), this repository's own small library for the agents standard, carried inside
every program; [`publish/`](publish/), which builds the catalog; and [`tests/`](tests/), where every agent's program
runs as a process, spoken to in the standard's messages.

## The standard

Every agent here is written to the platform's agents standard, schema 3
([`documentation/agents.md`](https://github.com/decentai-io/decentai-engine/blob/main/documentation/agents.md)): a manifest,
and a program that reads and writes JSON messages on its standard input and output. The platform has no SDK.
`standard/` is this publisher's own code on top of those messages, so the agents keep one shape:

| | |
| --- | --- |
| `agent.py` | `AgentBase`, `ToolBase`, `ResourceDenied`: an agent is tools; a function's id is its tool's id and its method's name (`notes_save` is `NotesTool.save`) |
| `call.py` | `Call`: a function's inputs, and within it the runtime's functions — `call.records` (`record.*`), `call.model`, `call.say` and `call.ask` (`chat`), `call.open` (`network.open`), `call.screen` (`note`); files in as paths in the home (`call.file`), out with `call.give` |
| `program.py` | `Program`: `ready`, every call run on the agent's own loop together, a cancel answered at once |
| `net.py` | `Tunnel`, `TunnelRefused`, `BaseAddress`: connections through the sandbox's way out |

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
`documents/samples/` are made by `tests/make_sample_pdfs.py`, the deck under `slides/samples/` by
`tests/make_sample_decks.py`.

## Run the tests

Each agent's program runs as a process of its own, started at its first call in a home of its own, as a sandbox
starts it, and spoken to in the standard's messages ([`tests/harness.py`](tests/harness.py)): its inputs and outputs
held to their schemas, each call it makes held to what its function declares in `calls`, `record.*` answered from
records kept in memory, `model` and `chat` by the test. Its connections go through a way out of the test's own that
opens only the hosts it may reach; every provider is a stub under `tests/`. Every manifest is checked with the
platform's own checks, from a checkout of [the platform](https://github.com/decentai-io/decentai-engine) beside this one (as `decentai-engine`, or `decentai`).

```bash
docker run -d --name agents-test -v "$PWD:/w" python:3.12-slim sleep infinity
docker exec agents-test sh -c '
  cat /w/decentai-official-agents/agents/*/requirements.txt | sort -u > /tmp/pins.txt
  pip install -r /w/decentai-official-agents/tests/requirements.txt -r /tmp/pins.txt
  cd /w/decentai-official-agents && python -m pytest tests -q -n 6 --timeout=300'
```
