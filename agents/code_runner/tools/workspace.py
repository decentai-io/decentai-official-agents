"""Where one program runs: a folder made for it, holding the person's files and the program, and gone when the call
ends.

The program is a process of its own, and it is given as little as it needs:

- **A clean environment.** Only ``PATH``, ``HOME`` (a folder of its run's own), the way out (the proxy variables),
  the tokens the person granted it, and the few variables Python reads to find its packages and print UTF-8. Nothing
  of the agent's own environment reaches it.
- **A folder of its own.** It starts in an empty working folder made for this run; its home and the packages
  installed for it are beside that, in the same private folder, and all of it is removed when the call ends. The
  runs' folders sit in one folder that can be passed through but not listed, so a program finds no other run's
  folder by looking.
- **Limits** (``resource.setrlimit``, set in the child before it starts): its memory, the size of a file it writes,
  its open files and its processes. A program past its memory gets a ``MemoryError`` instead of taking the agent's
  whole process down with it (the sandbox ends every process of a group together when one runs out).
- **The agent itself is not dumpable** (``PR_SET_DUMPABLE``): a program can't read the agent's environment or
  memory through ``/proc``.

It cannot run as another user: the sandbox maps one user into each process's jail, so ``os.setuid`` to anyone else
is refused there (EPERM: the jail's one user has no capabilities in its namespace), and a nested user namespace
(``unshare``, ``clone`` with namespace flags) and ``ptrace`` are refused by the jail's seccomp policy. So a program is
the agent's own user: what another call of the same chat has in its own folder is kept from it by an unlistable
folder and a random name, not by the system's permissions; a program that set out to could change the folder's
mode back. It is the same person's files, in one chat's process (agents.md §1), and nothing of another chat's.
"""

import asyncio
import hashlib
import os
import re
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from standard.http import FILE_MOST, human_size

PROGRAM = "program.py"
SAFE_RE = re.compile(r"[^\w.()\[\] -]+")
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
               "no_proxy")
#: Where the packages the program carries are: ``site/`` at the program's root, beside ``agents/`` (publish/build.py).
#: In the tests, where there is none, the interpreter's own.
SITE = Path(__file__).resolve().parents[3] / "site"

#: The packages carried in the agent's program (requirements.txt pins every one of them, and what they need), so
#: that most programs install nothing and reach nothing. A program that names one of these at a version it holds
#: needs no package index.
CARRIED = {
    "certifi": "2026.10.23", "charset-normalizer": "3.5.2", "contourpy": "1.4.0", "cycler": "0.12.1",
    "et-xmlfile": "2.0.0", "fonttools": "4.68.0", "idna": "3.20", "kiwisolver": "1.5.1", "lxml": "6.1.3",
    "matplotlib": "3.11.2", "numpy": "2.5.5", "openpyxl": "3.1.5", "packaging": "26.3", "pandas": "3.1.1",
    "pillow": "13.0.0", "pyparsing": "3.3.3", "pypdf": "6.21.4", "python-dateutil": "4.0.0", "python-docx": "1.2.0",
    "python-pptx": "1.0.2", "requests": "2.34.2", "tabulate": "0.10.0", "typing-extensions": "4.16.0",
    "tzdata": "2026.6", "urllib3": "2.8.0", "xlsxwriter": "3.3.1",
}


def canonical(name: str) -> str:
    """A distribution's name as the package index compares names (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def carried(spec: str) -> bool:
    """Whether a package as a program names it (``pandas``, ``pandas==3.1.1``, ``pandas>=2``) is one the program
    carries at a version that fits."""
    from packaging.requirements import InvalidRequirement, Requirement

    try:
        wanted = Requirement(spec)
    except InvalidRequirement:
        return False
    held = CARRIED.get(canonical(wanted.name))
    return bool(held) and not wanted.extras and not wanted.url and wanted.specifier.contains(held, prereleases=True)


def unguarded() -> None:
    """The agent's process made not dumpable, once: its environment and memory are closed to the programs it runs,
    which are the same user. Linux only; elsewhere nothing."""
    if unguarded.done or not sys.platform.startswith("linux"):
        return
    unguarded.done = True
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):
        pass


unguarded.done = False


class Limits:
    """What a run may use, set in the child before the program starts. Each can be changed by the agent's
    environment: DECENTAI_CODE_MEMORY_MIB, DECENTAI_CODE_FILE_MIB."""

    #: The sandbox gives a process 1 GiB by default, and the files written in its /tmp count in it: the program's
    #: share leaves the agent and the files room, so that a program past its memory fails alone.
    MEMORY_MIB = 640
    FILE_MIB = 256
    FILES_OPEN = 512
    PROCESSES = 256

    def __init__(self):
        self.memory = self._mib("DECENTAI_CODE_MEMORY_MIB", self.MEMORY_MIB)
        self.file = self._mib("DECENTAI_CODE_FILE_MIB", self.FILE_MIB)

    @staticmethod
    def _mib(name: str, default: int) -> int:
        try:
            return max(16, int(os.environ.get(name) or default)) * 1024 * 1024
        except ValueError:
            return default * 1024 * 1024

    def apply(self) -> None:
        """In the child, between fork and exec: only system calls, nothing that takes a lock."""
        import resource

        for limit, value in ((resource.RLIMIT_DATA, self.memory), (resource.RLIMIT_FSIZE, self.file),
                             (resource.RLIMIT_NOFILE, self.FILES_OPEN), (resource.RLIMIT_NPROC, self.PROCESSES),
                             (resource.RLIMIT_CORE, 0)):
            try:
                soft, hard = resource.getrlimit(limit)
                value = value if hard == resource.RLIM_INFINITY else min(value, hard)
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass


class Ran:
    """How a run ended: its exit code, and what it printed."""

    def __init__(self, code: int, output: str, timed_out: bool = False):
        self.code = code
        self.output = output
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


class NotInstalled(Exception):
    """The packages a program named could not be installed, in pip's words."""


class Packages:
    """The packages the person allowed with a program, installed for this call alone once the package index is open
    for it (network.open): wheels only, never a package built (and its code run) on the way in. Python and pip are
    the sandbox base's."""

    SECONDS = 600
    SAID_CHARS = 600

    @staticmethod
    def needed(packages: List[str]) -> List[str]:
        """Those of a program's packages the program doesn't carry."""
        return [spec for spec in packages if not carried(spec)]

    @classmethod
    async def install(cls, packages: List[str], workspace: "Workspace") -> str:
        """The folder they are in, for the program's ``PYTHONPATH``."""
        named = hashlib.sha256("\n".join(sorted(packages)).encode("utf-8")).hexdigest()[:16]
        folder = workspace.base / "packages" / named
        finished = folder / ".installed"
        if finished.exists():
            return str(folder)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        # pip's settings (PIP_*) are the agent's to give; nothing else of its environment.
        environment = {**workspace.environment(None, {}),
                       **{k: v for k, v in os.environ.items() if k.startswith("PIP_")}}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--quiet", "--no-input", "--disable-pip-version-check",
            "--no-cache-dir", "--only-binary=:all:", "--target", str(folder), "--", *packages,
            cwd=str(workspace.base), env=environment,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            said, _ = await asyncio.wait_for(process.communicate(), cls.SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise NotInstalled(f"pip did not finish in {cls.SECONDS} seconds.")
        except asyncio.CancelledError:
            process.kill()
            raise
        if process.returncode != 0:
            raise NotInstalled(cls._words(said))
        finished.touch()
        return str(folder)

    @classmethod
    def _words(cls, said: bytes) -> str:
        """What pip said. The proxy's address may carry this worker's pass, which is nobody's to read."""
        text = said.decode("utf-8", "replace").strip()
        for value in Workspace._hidden({}):
            text = text.replace(value, "the platform's proxy")
        return text[-cls.SAID_CHARS:] or "pip failed and said nothing."


class Workspace:
    #: What is kept of what a program printed: its beginning and its end, which is where the answer and the error
    #: are.
    HEAD_CHARS = 6000
    TAIL_CHARS = 3000
    #: What is handed back of what it made: ten files, each at most what a chat file can be.
    FILES_MAX = 10
    FILE_MAX_BYTES = FILE_MOST

    def __init__(self):
        unguarded()
        self.base = Path(tempfile.mkdtemp(prefix="code-", dir=self.runs()))  # 0700: the agent's alone
        self.folder = self.base / "work"
        self.home = self.base / "home"
        self.said = self.base / "output.txt"
        self.limits = Limits()
        self._before: Dict[str, tuple] = {}

    @staticmethod
    def runs() -> str:
        """The folder every run's folder is made in: its owner may pass through it and make folders in it, but not
        list it (0300), so a run's folder is found only by its random name."""
        folder = Path(tempfile.gettempdir()) / f"code-runs-{os.getpid()}"
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            if os.name != "nt":
                os.chmod(folder, 0o300)
        except OSError:
            return tempfile.gettempdir()
        return str(folder)

    @staticmethod
    def name(filename: str, taken: List[str]) -> str:
        """A file's own name, made safe to lie in the working folder: no folder before it, nothing a shell reads,
        and not the program's or another file's. Letters of any script are kept."""
        name = SAFE_RE.sub("_", Path(str(filename or "").replace("\\", "/")).name).strip(" .") or "file"
        stem, dot, ending = name.rpartition(".")
        if not dot:
            stem, ending = name, ""
        candidate, count = name, 1
        while candidate.lower() in (PROGRAM, *(t.lower() for t in taken)):
            count += 1
            candidate = f"{stem}-{count}" + (f".{ending}" if ending else "")
        return candidate

    def fill(self, files: Dict[str, bytes], code: str) -> None:
        """The folders as a run finds them: the person's files and the program, an empty home, and nothing an
        earlier run left."""
        for folder in (self.folder, self.home):
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
        for name, content in files.items():
            (self.folder / name).write_bytes(content)
        (self.folder / PROGRAM).write_text(code, encoding="utf-8")
        self._before = self._listing()

    def _listing(self) -> Dict[str, tuple]:
        found = {}
        for path in self.folder.rglob("*"):
            relative = path.relative_to(self.folder)
            if path.is_file() and not path.is_symlink() and "__pycache__" not in relative.parts \
                    and not any(part.startswith(".") for part in relative.parts):
                stat = path.stat()
                found[relative.as_posix()] = (stat.st_size, stat.st_mtime_ns)
        return found

    def environment(self, packages: Optional[str], secrets: Dict[str, str]) -> Dict[str, str]:
        """All a program is given: where commands are, its own home, the way out, its tokens, and where its
        packages are. Nothing else of the agent's environment."""
        paths = [path for path in (packages, str(SITE) if SITE.is_dir() else "") if path]
        return {
            "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.home),
            "MPLCONFIGDIR": str(self.home / ".matplotlib"),
            "MPLBACKEND": "Agg",
            "LANG": "C.UTF-8",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            **({"PYTHONPATH": os.pathsep.join(paths)} if paths else {}),
            **{name: os.environ[name] for name in PROXY_NAMES if os.environ.get(name)},
            **secrets,
        }

    async def run(self, packages: Optional[str], secrets: Dict[str, str], seconds: float) -> Ran:
        """The program, run to its end or to ``seconds``. ``packages`` is the folder the packages installed for it
        are in (``Packages``); ``secrets`` reach the program as its environment and are taken out of what it
        printed."""
        timed_out = False
        posix = os.name != "nt"
        with open(self.said, "wb") as said:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-s", PROGRAM, cwd=str(self.folder),
                env=self.environment(packages, secrets), stdin=asyncio.subprocess.DEVNULL,
                stdout=said, stderr=asyncio.subprocess.STDOUT,
                # A group of its own, so that what the program started ends with it; and its limits.
                **({"start_new_session": True, "preexec_fn": self.limits.apply} if posix else {}))
            try:
                await asyncio.wait_for(process.wait(), seconds)
            except asyncio.TimeoutError:
                timed_out = True
                self._end(process)
                await process.wait()
            except asyncio.CancelledError:
                self._end(process)
                raise
        return Ran(process.returncode if process.returncode is not None else 1,
                   self._printed(secrets), timed_out)

    @staticmethod
    def _end(process) -> None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    @staticmethod
    def _hidden(secrets: Dict[str, str]) -> List[str]:
        """What never leaves in what the program printed: the program's tokens, and the run's way out, whose
        address may carry its pass. Longest first, so a part never leaves the rest of a whole."""
        hidden = [value for value in secrets.values() if value]
        for name in PROXY_NAMES:
            address = os.environ.get(name) or ""
            if address and "://" in address:
                hidden += [address, urlsplit(address).password or ""]
        return sorted({value for value in hidden if value}, key=len, reverse=True)

    def _printed(self, secrets: Dict[str, str]) -> str:
        try:
            text = self.said.read_bytes().decode("utf-8", "replace")
        except OSError:
            return ""
        for value in self._hidden(secrets):
            text = text.replace(value, "[hidden]")
        if len(text) > self.HEAD_CHARS + self.TAIL_CHARS:
            left_out = len(text) - self.HEAD_CHARS - self.TAIL_CHARS
            text = (text[: self.HEAD_CHARS]
                    + f"\n… ({left_out} characters left out) …\n"
                    + text[-self.TAIL_CHARS:])
        return text.strip()

    def made(self) -> List[Path]:
        """The files the run made or changed, by name."""
        after = self._listing()
        return [self.folder / name for name in sorted(after)
                if name != PROGRAM and after[name] != self._before.get(name)]

    def kept(self) -> Tuple[List[Path], List[str]]:
        """What of what it made is handed back, and what is not, each with why: past the tenth, or larger than a
        chat file can be (its size said)."""
        handed, left = [], []
        for path in self.made():
            size = path.stat().st_size
            named = path.relative_to(self.folder).as_posix()
            if size > self.FILE_MAX_BYTES:
                left.append(f"{named}: {human_size(size)}, more than the {human_size(self.FILE_MAX_BYTES)} a chat "
                            f"file can be")
            elif len(handed) >= self.FILES_MAX:
                left.append(f"{named}: past the {self.FILES_MAX} files one run hands back")
            else:
                handed.append(path)
        return handed, left

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)
