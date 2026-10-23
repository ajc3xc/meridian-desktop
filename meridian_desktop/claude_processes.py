"""Read-only list of the running Claude Desktop and Claude Code processes on this computer. Pure Python: no Qt import.

The desktop runs one fixed command, ``tasklist /FO LIST``, with an argument list and no shell, and a short timeout.
The text is read, and only two fields are kept for each process: the program's image name and its process id. The
command line, the program path, the user name, the window title and the memory figure are never copied into a result,
so no token, file path or prompt text can reach the UI from here.

The image names that count are the ones Claude Desktop and Claude Code run as on Windows, ``claude.exe``, matched
without regard to case. A Claude Code session started through Node.js runs as ``node.exe``, a name that other software
also uses, so it is not listed. Nothing here starts, stops or changes a process.

Typed states (ClaudeProcessesResult.state):
- listed: the list was read. It may be empty, which means no matching process is running.
- unavailable: the list could not be read. The command failed or timed out, or its text held no record the parser
  could read.

Every message is fixed text, so a result never carries the output of the command or a raw exception.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

TASKLIST_ARGS = ("/FO", "LIST")
PROCESS_TIMEOUT = 5.0
# Image names, matched without regard to case, that belong to Claude Desktop and Claude Code on Windows.
CLAUDE_IMAGE_NAMES = frozenset({"claude.exe"})
# A record's image name and process id must look like this. Anything else is skipped, never shown.
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,119}")
_PID = re.compile(r"[0-9]{1,10}")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LISTED = "listed"
UNAVAILABLE = "unavailable"

UNAVAILABLE_MESSAGE = "The running Claude processes could not be listed on this computer."
TIMEOUT_MESSAGE = "Listing the running processes took too long, so the Claude processes were not listed."

# A runner returns tasklist list-form text. It may raise. Tests pass a fake, so they start no process.
RunFn = Callable[[], str]


@dataclass(frozen=True)
class ClaudeProcess:
    """One matching process: the program's image name as Windows reports it, and its process id."""

    image: str
    pid: int


@dataclass(frozen=True)
class ClaudeProcessesResult:
    """The outcome of one listing. ``processes`` is set only for state listed."""

    state: str
    message: str = ""
    processes: tuple[ClaudeProcess, ...] = ()


def _tasklist_text() -> str:
    """Run tasklist in list form and return its text. The program is named by its full path, with no shell."""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    tasklist = os.path.join(system_root, "System32", "tasklist.exe")
    completed = subprocess.run(
        [tasklist, *TASKLIST_ARGS],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=PROCESS_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise OSError("tasklist did not finish")
    return completed.stdout


def _records(text: str) -> list[list[str]]:
    """Split list-form text into records. A record is a run of non-blank lines, and blank lines separate records."""
    records: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            records.append(current)
            current = []
    if current:
        records.append(current)
    return records


def _values(record: list[str]) -> list[str]:
    """The text after the first colon of each line, in order. Labels are not used, since Windows translates them."""
    return [line.split(":", 1)[1].strip() for line in record if ":" in line]


def parse_tasklist(text: object) -> tuple[ClaudeProcess, ...] | None:
    """The matching processes in tasklist list-form text. None when no record in it can be read.

    Each record gives the image name first and the process id second; the other fields are ignored. A record whose
    image name or id does not look right is skipped. An empty tuple means the list was read and no matching process
    is in it.
    """
    if not isinstance(text, str):
        return None
    readable = 0
    found: list[ClaudeProcess] = []
    for record in _records(text):
        values = _values(record)
        if len(values) < 2:
            continue
        image, pid = values[0], values[1]
        if not _IMAGE.fullmatch(image) or not _PID.fullmatch(pid):
            continue
        readable += 1
        if image.casefold() in CLAUDE_IMAGE_NAMES:
            found.append(ClaudeProcess(image=image, pid=int(pid)))
    if readable == 0:
        return None
    return tuple(sorted(found, key=lambda process: (process.image.casefold(), process.pid)))


def list_claude_processes(run: RunFn | None = None) -> ClaudeProcessesResult:
    """Read the running Claude processes. ``run`` returns tasklist list-form text; the default runs tasklist.

    Never raises. A timeout, a failed command or unreadable text gives the unavailable state with fixed text.
    """
    reader = _tasklist_text if run is None else run
    try:
        text = reader()
    except subprocess.TimeoutExpired:
        return ClaudeProcessesResult(UNAVAILABLE, TIMEOUT_MESSAGE)
    except Exception:  # noqa: BLE001 - a failed listing is shown in plain words, never as a raw exception
        return ClaudeProcessesResult(UNAVAILABLE, UNAVAILABLE_MESSAGE)
    processes = parse_tasklist(text)
    if processes is None:
        return ClaudeProcessesResult(UNAVAILABLE, UNAVAILABLE_MESSAGE)
    return ClaudeProcessesResult(LISTED, "", processes)


__all__ = [
    "CLAUDE_IMAGE_NAMES",
    "LISTED",
    "PROCESS_TIMEOUT",
    "TASKLIST_ARGS",
    "UNAVAILABLE",
    "ClaudeProcess",
    "ClaudeProcessesResult",
    "list_claude_processes",
    "parse_tasklist",
]
