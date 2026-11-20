"""The Code agent's program, started and spoken to as the runtime does: programs really written to disk and really
run, the chat's model scripted, the person a scripted answer to the question that shows a program, to network.open
and to a token's form.
"""

import zipfile
from pathlib import Path

import pytest
import yaml

from tests.harness import Chat, ChatSinks, Records, probe, requirements, run


ROOT = Path(__file__).resolve().parents[1] / "agents"
ORDERS = b"month,total\nJanuary,1200.50\nFebruary,980.00\nMarch,1430.25\n"

TOTALS = '''import csv

with open("orders.csv", newline="") as handle:
    rows = list(csv.DictReader(handle))
total = sum(float(row["total"]) for row in rows)
best = max(rows, key=lambda row: float(row["total"]))
with open("totals.txt", "w") as out:
    out.write(f"{total:.2f}")
print(f"Total {total:.2f}; best month {best['month']}")
'''


def answer(code, purpose="Totals the orders and says which month was best.",
           packages="none", hosts="none", credentials="none"):
    return (f"PURPOSE: {purpose}\nPACKAGES: {packages}\nHOSTS: {hosts}\n"
            f"CREDENTIALS: {credentials}\n```python\n{code}```")


class Model:
    """The chat's model, scripted: one answer a request, in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    async def __call__(self, messages, max_tokens=None, pictures=None):
        self.asked.append(messages[-1]["text"])
        assert self.answers, "the model was asked more than the script holds"
        return self.answers.pop(0)


class Person:
    """The scripted person: answers each program shown as the list says (None: no answer), and gives a token
    when its form is put to them."""

    def __init__(self, answers=(True,), token="s3cret-token"):
        self.answers = list(answers)
        self.token = token
        #: Programs shown as a question in the chat; network.open as it was asked; the token forms.
        self.questions, self.opened, self.forms = [], [], []

    def allows(self):
        return self.answers.pop(0) if self.answers else True

    async def ask(self, text, choices, expects):
        self.questions.append(text)
        allowed = self.allows()
        return None if allowed is None else choices[0] if allowed else choices[1]

    async def open(self, hosts, purpose, code):
        self.opened.append({"hosts": hosts, "purpose": purpose, "code": code})
        return self.allows()

    async def form(self, resource, asked):
        self.forms.append((resource, asked["fields"], asked["given"]))
        return {"token": self.token} if self.token else None


def made(model, person=None, provider=None):
    provider = provider or Records()
    person = person or Person()
    executor = Chat(provider, ChatSinks(llm=model, ask=person.ask, open=person.open, form=person.form))
    return executor, provider, person


def program(agents, executor, inputs):
    return run(executor.invoke(agents["code_runner"], "program_run", inputs, chat_level=2))


class TestTheManifest:
    def test_it_declares_no_host_its_tokens_and_what_it_calls(self, agents):
        doc = yaml.safe_load((ROOT / "code_runner" / "manifest.yaml").read_text(encoding="utf-8"))
        # No host at all: the package index is opened per call, with the program that needs it (network.open
        # gives the process its way out, agents.md §8).
        assert doc["network"] == {"hosts": []}
        assert doc["agent"]["version"] == "1.1.0"
        # The packages it carries, each pinned, the common ones among them.
        pinned = dict(line.split("==") for line in requirements(ROOT / "code_runner"))
        assert {"pandas", "numpy", "matplotlib", "openpyxl", "python-docx", "xlsxwriter"} <= set(pinned)
        from agents.code_runner.tools.workspace import CARRIED, canonical
        assert {canonical(name): version for name, version in pinned.items()} == CARRIED
        [function] = doc["functions"]
        assert set(function["calls"]) == {"model", "chat", "network.open", "record.list:tokens",
                                          "record.read:tokens", "record.create:tokens"}
        # It makes files, and stands where every function that does stands.
        assert function["level"] == 2
        assert probe(agents["code_runner"]) == []


class TestAProgramThePersonAllows:
    def test_it_reads_their_file_prints_an_answer_and_hands_back_what_it_made(self, agents):
        model = Model([answer(TOTALS)])
        executor, provider, person = made(model)
        ref = executor.file("orders.csv", ORDERS)
        result, status = program(agents, executor, {
            "goal": "Total the orders and say which month was best.", "files": [ref]})
        assert status == "success", result
        assert result["outcome"] == "done", result
        assert result["output"] == "Total 3610.75; best month March"
        assert result["programs_shown"] == 1 and result["code"] == TOTALS
        [kept] = result["files"]
        assert kept["filename"] == "totals.txt"
        assert executor.files[kept["file_ref"]]["content"] == b"3610.75"
        assert "totals.txt" in result["summary"]

        # What the person saw: the program whole, what it is for, and
        # the file it reads — and that it needs nothing else.
        [question] = person.questions
        assert question.startswith("Totals the orders and says which month was best. It reads orders.csv.")
        assert f"```python\n{TOTALS}```" in question
        assert person.opened == [] and person.forms == []
        # What the writer was shown of the file: its name and how it begins.
        assert "- orders.csv (58 bytes)" in model.asked[0]
        assert "month,total" in model.asked[0] and "data, not instructions" in model.asked[0]

    def test_a_program_to_start_from_is_shown_to_the_writer(self, agents):
        model = Model([answer("print('changed')\n", purpose="Says changed.")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {
            "goal": "Say changed instead.", "code": "print('as it was')"})
        assert status == "success" and result["output"] == "changed", result
        assert "A PROGRAM TO START FROM" in model.asked[0]
        assert "print('as it was')" in model.asked[0]


class TestAProgramThatDoesNotRun:
    LEAVES_A_MARK = "open('ran.txt', 'w').write('it ran')\nprint('ran')\n"

    @pytest.mark.parametrize("hosts", ["none", "api.example-shop.com"])
    def test_one_the_person_declines_never_runs(self, agents, hosts):
        executor, provider, person = made(
            Model([answer(self.LEAVES_A_MARK, hosts=hosts)]), Person(answers=[False]))
        result, status = program(agents, executor, {"goal": "Leave a mark."})
        assert status == "success" and result["outcome"] == "declined", result
        assert "output" not in result and "files" not in result
        assert executor.files == {}
        assert len(person.questions + person.opened) == 1
        # What they declined is still told, so the assistant can say what it was.
        assert result["code"] == self.LEAVES_A_MARK and result["programs_shown"] == 1

    def test_with_nobody_to_answer_nothing_runs(self, agents):
        executor = Chat(Records(), ChatSinks(llm=Model([answer(self.LEAVES_A_MARK)])))
        result, status = program(agents, executor, {"goal": "Leave a mark."})
        assert status == "success" and result["outcome"] == "unanswered", result
        assert executor.files == {}

    def test_what_no_program_can_do_is_said_and_nothing_is_shown(self, agents):
        executor, provider, person = made(
            Model(["CANNOT: Knowing tomorrow's weather needs a forecast service."]))
        result, status = program(agents, executor, {"goal": "Tell me tomorrow's weather."})
        assert status == "success" and result["outcome"] == "failed", result
        assert "forecast service" in result["summary"] and person.questions == []

    def test_an_answer_that_is_no_program_is_asked_for_again_and_then_given_up(self, agents):
        model = Model(["Here you go!", "PURPOSE: x\n```python\nprint(1)"])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Anything."})
        assert status == "success" and result["outcome"] == "failed", result
        assert "could not be used" in result["summary"] and person.questions == []
        assert "YOUR LAST ANSWER COULD NOT BE USED" in model.asked[1]


class TestAProgramThatFails:
    BROKEN = "import csv\nrows = list(csv.DictReader(open('orders.csv')))\nprint(rows[0]['amount'])\n"

    def test_it_is_corrected_and_the_person_sees_the_new_one(self, agents):
        model = Model([answer(self.BROKEN, purpose="Prints the first amount."),
                       answer(TOTALS)])
        executor, provider, person = made(model)
        ref = executor.file("orders.csv", ORDERS)
        result, status = program(agents, executor, {"goal": "Total the orders.", "files": [ref]})
        assert status == "success" and result["outcome"] == "done", result
        assert result["programs_shown"] == 2 and result["code"] == TOTALS
        # A changed program is a new program: both went before the person,
        # and the second says why there is a second.
        first, second = person.questions
        assert first.startswith("Prints the first amount.") and self.BROKEN in first
        assert second.startswith("A correction: the program before this one failed. Totals the orders")
        assert TOTALS in second
        assert "earlier_failure" not in result
        # The correction was written from what happened.
        assert "KeyError: 'amount'" in model.asked[1]
        assert "THE PROGRAM THAT WAS TRIED" in model.asked[1]

    def test_a_correction_the_person_declines_says_what_failed_before_it(self, agents):
        model = Model([answer(self.BROKEN, purpose="Prints the first amount."),
                       answer(TOTALS)])
        executor, provider, person = made(model, Person(answers=[True, False]))
        ref = executor.file("orders.csv", ORDERS)
        result, status = program(agents, executor, {"goal": "Total the orders.", "files": [ref]})
        assert status == "success" and result["outcome"] == "declined", result
        assert result["programs_shown"] == 2
        assert "KeyError: 'amount'" in result["earlier_failure"]

    def test_three_programs_are_the_most_one_call_shows(self, agents):
        model = Model([answer("raise SystemExit('no such thing')\n")] * 3)
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Do the impossible."})
        assert status == "success" and result["outcome"] == "failed", result
        assert result["programs_shown"] == 3 and len(person.questions) == 3
        assert "no such thing" in result["summary"] and "no such thing" in result["output"]

    def test_one_that_does_not_end_is_stopped(self, agents, monkeypatch):
        monkeypatch.setenv("DECENTAI_CODE_RUN_SECONDS", "2")
        model = Model([answer("import time\nprint('started', flush=True)\ntime.sleep(60)\n"),
                       answer("print('quick')\n")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Be quick."})
        assert status == "success" and result["outcome"] == "done", result
        assert result["output"] == "quick"
        assert "stopped after 2 seconds" in model.asked[1] and "started" in model.asked[1]


class TestWhatAProgramNeeds:
    TOKEN = "import os\nprint('the token is', os.environ['SHOP_TOKEN'])\n"

    def test_a_token_is_asked_once_kept_for_its_host_and_never_printed_back(self, agents):
        shows = answer(self.TOKEN, purpose="Shows the token.", hosts="api.example-shop.com",
                       credentials="SHOP_TOKEN=api.example-shop.com")
        executor, provider, person = made(Model([shows, shows]))
        result, status = program(agents, executor, {"goal": "Show the token."})
        assert status == "success" and result["outcome"] == "done", result
        assert person.forms == [("tokens", ["token"], {"host": "api.example-shop.com"})]
        assert "s3cret-token" not in result["output"]
        assert "the token is [hidden]" in result["output"]
        # The hosts opened for it, with the program and what it uses.
        [opened] = person.opened
        assert opened["hosts"] == ["api.example-shop.com"]
        assert opened["purpose"] == "Shows the token. It uses SHOP_TOKEN for api.example-shop.com."
        assert opened["code"] == {"language": "python", "text": self.TOKEN}
        assert list(provider.secrets["tokens"].values()) == [
            {"keys": {"host": "api.example-shop.com"}, "values": {"token": "s3cret-token"}}]

        # The next program for that host uses the token kept for it.
        result, status = program(agents, executor, {"goal": "Show the token again."})
        assert status == "success" and result["outcome"] == "done", result
        assert len(person.forms) == 1 and len(person.opened) == 2

    def test_a_credential_the_person_does_not_give_ends_it(self, agents):
        model = Model([answer("print(1)\n", hosts="api.example-shop.com",
                              credentials="SHOP_TOKEN=api.example-shop.com")])
        executor, provider, person = made(model, Person(token=""))
        result, status = program(agents, executor, {"goal": "Use the shop."})
        assert status == "success" and result["outcome"] == "declined", result
        assert "credential" in result["summary"] and "output" not in result

    def test_a_credential_for_a_host_the_program_did_not_name_is_not_a_plan(self, agents):
        model = Model([
            answer("print(1)\n", hosts="api.example-shop.com",
                   credentials="SHOP_TOKEN=elsewhere.example.net"),
            answer("print('ok')\n", purpose="Says ok."),
        ])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Use the shop."})
        assert status == "success" and result["outcome"] == "done", result
        assert "which HOSTS does not name" in model.asked[1]
        assert len(person.questions) == 1 and person.opened == [] and person.forms == []

    def test_an_address_is_sent_back_to_the_writer_before_anyone_sees_it(self, agents):
        """HOSTS names hosts by name: an address goes back to the writer, in
        words it can act on, and the person sees only the program after it."""
        model = Model([answer("print(1)\n", hosts="10.0.0.7"),
                       answer("print('ok')\n", purpose="Says ok.")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Reach the router."})
        assert status == "success" and result["outcome"] == "done", result
        assert "'10.0.0.7' is not a host name" in model.asked[1]
        assert person.opened == [] and len(person.questions) == 1
        assert "print('ok')" in person.questions[0] and result["programs_shown"] == 1

    def test_its_packages_are_installed_and_found_by_the_program(self, agents, tmp_path, monkeypatch):
        """The real pip, from a folder of wheels where the package index
        would be: the person allows the index with the program, the agent
        installs what it named into a folder of its own, and the program
        imports it from there."""
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        shout = wheels / "shout-1.0-py3-none-any.whl"
        with zipfile.ZipFile(shout, "w") as wheel:
            wheel.writestr("shout.py", "def loud(words):\n    return words.upper() + '!'\n")
            wheel.writestr("shout-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: shout\nVersion: 1.0\n")
            wheel.writestr("shout-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: tests\n"
                                                        "Root-Is-Purelib: true\nTag: py3-none-any\n")
            wheel.writestr("shout-1.0.dist-info/RECORD", "")
        monkeypatch.setenv("PIP_NO_INDEX", "1")
        monkeypatch.setenv("PIP_FIND_LINKS", str(wheels))

        model = Model([answer("import shout\nprint(shout.loud('done'))\n",
                              purpose="Says done, loudly.", packages="shout==1.0")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Say done loudly."})
        assert status == "success" and result["outcome"] == "done", result
        assert result["output"] == "DONE!" and result["packages"] == ["shout==1.0"]
        [opened] = person.opened
        assert opened["hosts"] == ["pypi.org", "files.pythonhosted.org"]
        assert opened["purpose"] == "Says done, loudly. It installs shout==1.0 from the package index."
        assert result["installed"] == ["shout==1.0"]

    def test_what_pip_cannot_install_goes_back_to_the_writer(self, agents, monkeypatch):
        """pip's own words reach the correction, and nothing ran."""
        monkeypatch.setenv("PIP_NO_INDEX", "1")
        model = Model([answer("import shout\n", purpose="Says done.", packages="shout==1.0"),
                       answer("print('done')\n", purpose="Says done.")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Say done."})
        assert status == "success" and result["outcome"] == "done", result
        assert "could not be installed" in model.asked[1] and "shout" in model.asked[1]


class TestThePlan:
    """What the writer's answer is read as (tools/plan.py)."""

    def test_the_labelled_lines_and_the_fence_are_the_plan(self):
        from agents.code_runner.tools.plan import Plan

        plan = Plan.read(
            "PURPOSE:  Gets   the issues.\nPACKAGES: requests, `pandas`\n"
            "HOSTS: API.github.com\nCREDENTIALS: GITHUB_TOKEN=api.github.com\n"
            "```python\nimport requests\n```\nThat should do it.")
        assert plan.unreadable == "" and plan.cannot == ""
        assert plan.purpose == "Gets the issues."
        assert plan.packages == ["requests", "pandas"] and plan.hosts == ["api.github.com"]
        [credential] = plan.credentials
        assert (credential.name, credential.host, credential.label) == (
            "GITHUB_TOKEN", "api.github.com", "GITHUB_TOKEN for api.github.com")
        assert plan.code == "import requests\n"

    def test_none_is_nothing_and_a_missing_line_is_nothing(self):
        from agents.code_runner.tools.plan import Plan

        plan = Plan.read("PURPOSE: Adds.\nPACKAGES: none\n```python\nprint(2)\n```")
        assert (plan.packages, plan.hosts, plan.credentials, plan.unreadable) == ([], [], [], "")

    def test_what_is_not_a_plan_says_why(self):
        from agents.code_runner.tools.plan import Plan

        assert "no program" in Plan.read("I would use pandas for this.").unreadable
        assert "never closed" in Plan.read("PURPOSE: x\n```python\nprint(1)").unreadable
        assert "PURPOSE is missing" in Plan.read("```python\nprint(1)\n```").unreadable
        assert "in capitals" in Plan.read(
            "PURPOSE: x\nHOSTS: a.example.com\nCREDENTIALS: token=a.example.com\n"
            "```python\nprint(1)\n```").unreadable
        assert "in capitals" in Plan.read(
            "PURPOSE: x\nHOSTS: a.example.com\nCREDENTIALS: PATH=a.example.com\n"
            "```python\nprint(1)\n```").unreadable
        assert "not a host name" in Plan.read("PURPOSE: x\nHOSTS: *.example.com\n```python\nprint(1)\n```").unreadable
        assert Plan.read("CANNOT: It needs a person.").cannot == "It needs a person."


class TestTheWorkingFolder:
    def test_a_files_name_is_made_safe_and_its_own(self):
        from agents.code_runner.tools.workspace import Workspace

        assert Workspace.name("../../etc/passwd", []) == "passwd"
        assert Workspace.name("my report (final).xlsx", []) == "my report (final).xlsx"
        assert Workspace.name("a;rm -rf.csv", []) == "a_rm -rf.csv"
        assert Workspace.name("program.py", []) == "program-2.py"
        assert Workspace.name("Orders.csv", ["orders.csv"]) == "Orders-2.csv"
        assert Workspace.name("", []) == "file"


class TestTheWayOutIsNotPrinted:
    def test_the_proxy_address_and_its_pass_are_hidden(self, monkeypatch):
        """A program may print its environment: the run's pass to the way out is in it."""
        from agents.code_runner.tools.workspace import Workspace

        monkeypatch.setenv("HTTPS_PROXY", "http://run:p-2c8d1xQ@sandbox:3128")
        hidden = Workspace._hidden({"API_KEY": "k-1"})
        assert {"http://run:p-2c8d1xQ@sandbox:3128", "p-2c8d1xQ", "k-1"} <= set(hidden)
        assert hidden[0] == "http://run:p-2c8d1xQ@sandbox:3128"


class TestThePackagesItCarries:
    USES = ("import numpy, pandas, openpyxl, docx, matplotlib\n"
            "matplotlib.use('Agg')\nimport matplotlib.pyplot as plot\n"
            "frame = pandas.DataFrame({'a': numpy.arange(3)})\n"
            "plot.bar(frame.index, frame['a']); plot.savefig('chart.png')\n"
            "print('sum', int(frame['a'].sum()))\n")

    def test_a_program_that_names_only_carried_packages_opens_nothing(self, agents):
        """pandas and its kind come with the agent: the program is a question in the chat, and no network.open."""
        model = Model([answer(self.USES, purpose="Draws a chart.", packages="pandas, numpy, matplotlib")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Draw a chart."})
        assert status == "success" and result["outcome"] == "done", result
        assert result["output"] == "sum 3" and "installed" not in result
        assert [f["filename"] for f in result["files"]] == ["chart.png"]
        assert person.opened == [] and len(person.questions) == 1
        assert "package index" not in person.questions[0]

    def test_another_version_opens_the_package_index_and_says_so_on_the_card(self, agents):
        model = Model([answer("import pandas\n", purpose="Uses an old pandas.", packages="pandas==1.5.3")])
        executor, provider, person = made(model, Person(answers=[False]))
        result, status = program(agents, executor, {"goal": "Use pandas 1.5.3."})
        assert status == "success" and result["outcome"] == "declined", result
        [opened] = person.opened
        assert opened["hosts"] == ["pypi.org", "files.pythonhosted.org"]
        assert opened["purpose"] == "Uses an old pandas. It installs pandas==1.5.3 from the package index."
        assert person.questions == []

    def test_what_is_carried_is_read_by_version(self):
        from agents.code_runner.tools.workspace import Packages

        assert Packages.needed(["pandas", "NumPy>=2", "python_docx", "pandas==3.1.1"]) == []
        assert Packages.needed(["pandas==1.5.3", "pandas[excel]", "shout"]) == ["pandas==1.5.3", "pandas[excel]",
                                                                               "shout"]


class TestTheProgramIsKeptApart:
    LOOKS = ("import os, pathlib, resource\n"
             "here = pathlib.Path.cwd()\n"
             "print('names', ','.join(sorted(os.environ)))\n"
             "print('agent', os.environ.get('CODE_RUNNER_AGENT_ONLY', 'unseen'))\n"
             "print('home', os.environ['HOME'] == str(here.parent / 'home'))\n"
             "print('tmp', os.environ['TMPDIR'] == os.environ['HOME'])\n"
             "print('runs', oct(os.stat(here.parent.parent).st_mode & 0o777))\n"
             "print('fsize', resource.getrlimit(resource.RLIMIT_FSIZE)[0] // 2**20)\n"
             "print('data', resource.getrlimit(resource.RLIMIT_DATA)[0] // 2**20)\n"
             "print('folder', here.parent)\n"
             "print('files', ','.join(sorted(p.name for p in here.iterdir())))\n")
    ALLOWED = {"PATH", "HOME", "TMPDIR", "MPLCONFIGDIR", "MPLBACKEND", "LANG", "PYTHONUTF8", "PYTHONIOENCODING",
               "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONNOUSERSITE", "PYTHONPATH", "HTTP_PROXY",
               "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
               "LC_CTYPE"}

    @staticmethod
    def said(output):
        return dict(line.split(" ", 1) for line in output.splitlines())

    def test_it_has_a_clean_environment_a_folder_of_its_own_and_limits(self, agents, monkeypatch):
        """Nothing of the agent's environment reaches the program; its home is its run's own; its limits are set;
        and the next call's program finds nothing of this one's."""
        monkeypatch.setenv("CODE_RUNNER_AGENT_ONLY", "kept by the agent")
        model = Model([answer(self.LOOKS, purpose="Looks around."), answer(self.LOOKS, purpose="Looks around.")])
        executor, provider, person = made(model)
        ref = executor.file("orders.csv", ORDERS)
        result, status = program(agents, executor, {"goal": "Look around.", "files": [ref]})
        assert status == "success" and result["outcome"] == "done", result
        first = self.said(result["output"])
        assert set(first["names"].split(",")) <= self.ALLOWED, first["names"]
        assert first["agent"] == "unseen"
        assert (first["home"], first["tmp"], first["runs"]) == ("True", "True", "0o300")
        assert (first["fsize"], first["data"]) == ("256", "640")
        assert first["files"] == "orders.csv,program.py"

        result, status = program(agents, executor, {"goal": "Look around again."})
        assert status == "success" and result["outcome"] == "done", result
        second = self.said(result["output"])
        assert second["folder"] != first["folder"] and second["files"] == "program.py"
        assert not Path(first["folder"]).exists()

    def test_a_program_past_its_memory_fails_alone(self, agents):
        greedy = "try:\n    block = bytearray(2 * 1024 ** 3)\nexcept MemoryError:\n    print('held')\n"
        executor, provider, person = made(Model([answer(greedy, purpose="Takes too much.")]))
        result, status = program(agents, executor, {"goal": "Take 2 GB."})
        assert status == "success" and result["output"] == "held", result


class TestWhatItHandsBack:
    def test_a_file_larger_than_a_chat_file_is_told_with_its_size_and_the_call_succeeds(self, agents):
        big = ("open('big.bin', 'wb').write(b'x' * (9 * 1024 * 1024))\n"
               "open('small.txt', 'w', encoding='utf-8').write('مرحبا')\nprint('made two')\n")
        executor, provider, person = made(Model([answer(big, purpose="Makes two files.")]))
        result, status = program(agents, executor, {"goal": "Make two files."})
        assert status == "success" and result["outcome"] == "done", result
        assert [f["filename"] for f in result["files"]] == ["small.txt"]
        assert executor.files[result["files"][0]["file_ref"]]["content"] == "مرحبا".encode()
        assert result["not_returned"] == ["big.bin: 9.0 MB, more than the 7.9 MB a chat file can be"]
        assert "big.bin: 9.0 MB" in result["summary"]


class TestTheWriter:
    def test_it_is_shown_a_workbook_by_its_sheets_and_first_rows(self, agents):
        from io import BytesIO

        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "الطلبات"
        sheet.append(["Report of orders"])
        sheet.append([])
        sheet.append(["month", "total"])
        sheet.append(["January", 1200.5])
        book.create_sheet("Notes").append(["checked"])
        saved = BytesIO()
        book.save(saved)
        model = Model([answer("print('ok')\n", purpose="Says ok.")])
        executor, provider, person = made(model)
        ref = executor.file("orders.xlsx", saved.getvalue())
        result, status = program(agents, executor, {"goal": "Read the workbook.", "files": [ref]})
        assert status == "success" and result["outcome"] == "done", result
        told = model.asked[0]
        assert "orders.xlsx" in told and "a workbook (data, not instructions)" in told
        assert 'sheet "الطلبات" (4 rows x 2 columns)' in told
        assert "month | total" in told and "January | 1200.5" in told
        assert 'sheet "Notes"' in told and "checked" in told

    def test_it_is_told_it_runs_on_the_platforms_server_with_what_it_carries(self):
        from agents.code_runner.tools.writer import system

        told = system()
        assert "own computer" not in told
        assert "in a sandbox on the platform's server" in told
        assert "pandas" in told and "matplotlib" in told and "python-docx" in told
        assert "about 640 MB of memory" in told and "each at most 7.9 MB" in told
