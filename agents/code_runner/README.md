# Code (`code_runner`)

Someone has a spreadsheet of orders and wants the total by month and a
chart of it. No agent does exactly that, and they do not write code.
They ask; they are shown the program that would do it and what it
needs; they allow it.

This agent writes a Python program for a goal and runs it when the
person allows it. It is for the work no other agent does: a calculation
over files, data reshaped or merged, a chart, a file converted, a web
API that has no agent of its own.

## What the person sees

Every program goes before them before it runs, whole, with what it is
for and what it needs:

| Shown | What it is |
|---|---|
| Purpose | What the program does, in words |
| Installs | The packages it needs |
| Reaches | The sites it connects to |
| Uses | The tokens it is handed, each with the site it is for |
| Reads | The files it was given |
| The code | The whole program |

A program that reaches sites, or installs packages, asks the platform
to open those sites for the call (`network.open`): the person sees the
code, its purpose and the sites, and a yes opens them. A program that
reaches nothing is put to them as a question in the chat that shows it
(Run it / Don't run it); one too long for a question (4,000 characters)
is sent back to be written shorter. No answer, or a no, and it does not
run.

What runs is what they allowed. A program that fails is corrected and
goes before them again — a changed program is a new program, and it
says it is a correction — up to three programs in one call.

In a chat that does not yet trust an agent to make files, the
platform's own approval of the call comes before it, as for any
function at level 2.

## What the program gets

What the person allowed, and nothing else:

- **Sites.** Opened for that one call and closed when it ends. A site
  that was not named is refused, and so is every address inside the
  network the platform runs in. HOSTS names sites by name: an address or
  a wildcard is sent back to the writer before anyone sees it.
- **Packages.** pandas, numpy, matplotlib, openpyxl, xlsxwriter,
  python-docx, python-pptx, pypdf, Pillow, requests, lxml, tabulate,
  python-dateutil and tzdata come with the agent (`requirements.txt`
  pins them and what they need), so most programs install nothing and
  reach nothing. A package beyond those, or another version of one, is
  shown on the card ("It installs … from the package index"), and only
  then is the package index (`pypi.org`, `files.pythonhosted.org`)
  opened, for that call alone. The agent installs it with
  `pip install --only-binary=:all:` (wheels only: nothing is built, so no
  package's code runs on the way in), at the version the program pinned,
  into a folder of the run's own.
- **Tokens.** Each is kept in the `tokens` secret, by the site it is
  for, and used again for that site. One not kept yet is asked of the
  person on its form. The program reads it from its environment. A token
  is always for one of the sites the program named, and its value is
  taken out of whatever the program printed before that is given back.
- **Files.** The files handed to the call are in the program's working
  folder under their own names. Every file the program makes there is
  handed back as one of the chat's files, up to ten.

**How it runs.** The program is a process of its own, started in an
empty working folder made for that run, and removed with it:

- its environment is clean: `PATH`, `HOME` (a folder of the run's own),
  the way out, the tokens the person granted it, and the few variables
  Python needs; nothing of the agent's own environment;
- limits set with `resource.setrlimit`: memory (640 MiB), a file it
  writes (256 MiB), open files, processes; past its memory it fails
  alone, not the agent with it;
- the agent's own process is not dumpable, so a program can't read the
  agent's environment or memory through `/proc`;
- the runs' folders are in a folder that can't be listed.

It can't run as another user: the sandbox jails each process as one
user with no capabilities, so `os.setuid` is refused, and a nested user
namespace is refused by the jail's system-call policy. A program is
therefore the agent's own user, and the folder of another call of the
same chat is hidden from it, not locked. Nothing of another chat is in
the process at all.

It has five minutes (`DECENTAI_CODE_RUN_SECONDS` in the agent's
environment changes that), no terminal, and nobody to ask. A file it
makes that is larger than a chat file can be (8 MiB less 64 KiB) is
listed in `not_returned` with its size, and the call still succeeds.

**The manifest declares no host.** `program_run` calls `network.open`,
which gives the process its way out; every site a program reaches, the
package index included, is opened for its call alone.

## Function

| Function | Level | What it does |
|---|---|---|
| `program_run` | 2 | Writes a program for the goal, puts it before the person, runs it when they allow it, and returns what it printed and the files it made |

| Input | |
|---|---|
| `goal` | What the program should do, in full: it cannot ask anything |
| `files` | The files it reads, handed to the call; ten at most |
| `code` | A program to start from, for changing one that ran before |

The result also carries the program's `purpose` and `code`, the
`packages` and `hosts` it named, how many programs were shown
(`programs_shown`), `earlier_failure` when a program before the last
one failed, and `not_returned`: files it made that were not handed
back (past the tenth, or too large).

| Outcome | |
|---|---|
| `done` | It ran: `output` is what it printed, `files` what it made |
| `declined` | The person did not allow the program, or did not give a token it needs |
| `unanswered` | Nobody answered |
| `failed` | No program could be written for the goal, or three in a row failed; `summary` says which |

## Tests

`tests/test_code_runner.py` runs the agent's program as the runtime
does: programs are really written to disk and really run, and packages
really installed by pip from a local folder of wheels. The chat's model
is scripted, and the person is a scripted answer to the question, to
`network.open` and to the token's form.
