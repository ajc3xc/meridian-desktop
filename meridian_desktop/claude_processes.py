"""Read-only list of the agent programs running on this computer. Pure Python: no Qt import.

The process table comes first. agent_processes reads it through the toolhelp snapshot and keeps three values for each
process: its id, its parent's id and its executable's file name. No process is opened, so no command line, path,
argument or user name is read. If the snapshot cannot be taken, the desktop runs one fixed command, ``tasklist /FO LIST``,
with an argument list, no shell and a short timeout, and keeps only the image name and the process id from its text.
tasklist gives no parent id, so each process it lists is counted as a root of its program.

The programs that count are set in agent_processes: claude.exe (Claude), codex.exe (Codex), cursor.exe (Cursor) and the
owner's saved custom names (the settings key agent_process_names). Matching is without regard to case. A Claude Code
session started through Node.js runs as node.exe, a name other software also uses, so it is not listed. Nothing here
starts, stops or changes a process.

Typed states (ClaudeProcessesResult.state):
- listed: the list was read. It may be empty, which means no matching process is running. ``source`` says which read
  gave the list, and ``groups`` holds one count for each program that has a process.
- unavailable: neither the snapshot nor tasklist could be read, or the tasklist text held no record the parser could read.

Every message is fixed text, so a result never carries the output of the command or a raw exception.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from meridian_desktop import agent_processes
from meridian_desktop.agent_processes import AgentGroup, AgentProcess
from meridian_desktop.settings_store import SettingsStore, default_settings_path

TASKLIST_ARGS = ("/FO", "LIST")
PROCESS_TIMEOUT = 5.0
SOURCE_SNAPSHOT = "snapshot"
SOURCE_TASKLIST = "tasklist"
# A record's image name and process id must look like this. Anything else is skipped, never shown.
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,119}")
_PID = re.compile(r"[0-9]{1,10}")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LISTED = "listed"
UNAVAILABLE = "unavailable"

UNAVAILABLE_MESSAGE = "The running agent programs could not be listed on this computer."
TIMEOUT_MESSAGE = "Listing the running processes took too long, so the agent programs were not listed."

# A runner returns tasklist list-form text. It may raise. Tests pass a fake, so they start no process.
RunFn = Callable[[], str]

# One matching process. The name is kept from the first version of this module, so existing callers still work.
ClaudeProcess = AgentProcess


@dataclass(frozen=True)
class ClaudeProcessesResult:
    """The outcome of one listing. ``processes`` and ``groups`` are set only for state listed.

    ``processes`` holds every matching process, and ``groups`` one count for each program that has one. ``source`` is
    SOURCE_SNAPSHOT or SOURCE_TASKLIST for a listed result. ``dropped`` counts the saved custom names that were not valid.
    """

    state: str
    message: str = ""
    processes: tuple[AgentProcess, ...] = ()
    groups: tuple[AgentGroup, ...] = ()
    source: str = ""
    dropped: int = 0


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


def parse_tasklist(text: object, labels: Mapping[str, str] | None = None) -> tuple[AgentProcess, ...] | None:
    """The matching processes in tasklist list-form text. None when no record in it can be read.

    ``labels`` is the registry of image names to match (agent_processes.registry). The default is the built-in programs.
    Each record gives the image name first and the process id second; the other fields are ignored. A record whose
    image name or id does not look right is skipped. An empty tuple means the list was read and no matching process is
    in it.
    """
    if not isinstance(text, str):
        return None
    names = agent_processes.registry() if labels is None else labels
    readable = 0
    found: list[AgentProcess] = []
    for record in _records(text):
        values = _values(record)
        if len(values) < 2:
            continue
        image, pid = values[0], values[1]
        if not _IMAGE.fullmatch(image) or not _PID.fullmatch(pid):
            continue
        readable += 1
        found.append(AgentProcess(image=image, pid=int(pid)))
    if readable == 0:
        return None
    return agent_processes.matching(found, names)


def _saved_names() -> Sequence[object]:
    """The owner's saved custom program names. An unreadable settings file gives none, and this never raises."""
    try:
        return SettingsStore(default_settings_path()).load().settings.agent_process_names
    except Exception:  # noqa: BLE001 - a settings problem only means that no custom name is counted
        return ()


def _safe_snapshot(read: Callable[[], list[AgentProcess] | None]) -> list[AgentProcess] | None:
    """The snapshot, or None when reading it raises. A raised error is the same as a snapshot that cannot be taken."""
    try:
        return read()
    except Exception:  # noqa: BLE001 - the tasklist fallback follows, so the error is not shown
        return None


def _listed(
    processes: tuple[AgentProcess, ...],
    labels: Mapping[str, str],
    source: str,
    dropped: int,
) -> ClaudeProcessesResult:
    return ClaudeProcessesResult(
        LISTED,
        "",
        processes,
        agent_processes.group_processes(processes, labels),
        source,
        dropped,
    )


def list_claude_processes(
    run: RunFn | None = None,
    *,
    snapshot: Callable[[], list[AgentProcess] | None] | None = None,
    custom_names: Sequence[object] | None = None,
) -> ClaudeProcessesResult:
    """Read the running agent programs. Never raises.

    The process table is read first, through ``snapshot`` (default agent_processes.snapshot_processes). When it cannot be
    read, tasklist is run instead, through ``run`` (default: tasklist itself). A caller that passes ``run`` supplies the
    tasklist text, so only the tasklist path is used and the snapshot is not read. ``custom_names`` are the owner's saved
    custom names; None reads them from settings. A timeout, a failed command or unreadable text gives the unavailable
    state, with fixed text.
    """
    try:
        names, dropped = agent_processes.clean_custom_names(_saved_names() if custom_names is None else custom_names)
    except Exception:  # noqa: BLE001 - a bad list of saved names means no custom name is counted
        names, dropped = (), 0
    labels = agent_processes.registry(names)
    if run is None:
        read_snapshot = agent_processes.snapshot_processes if snapshot is None else snapshot
        table = _safe_snapshot(read_snapshot)
        if table is not None:
            return _listed(agent_processes.matching(table, labels), labels, SOURCE_SNAPSHOT, dropped)
    reader = _tasklist_text if run is None else run
    try:
        text = reader()
    except subprocess.TimeoutExpired:
        return ClaudeProcessesResult(UNAVAILABLE, TIMEOUT_MESSAGE)
    except Exception:  # noqa: BLE001 - a failed listing is shown in plain words, never as a raw exception
        return ClaudeProcessesResult(UNAVAILABLE, UNAVAILABLE_MESSAGE)
    processes = parse_tasklist(text, labels)
    if processes is None:
        return ClaudeProcessesResult(UNAVAILABLE, UNAVAILABLE_MESSAGE)
    return _listed(processes, labels, SOURCE_TASKLIST, dropped)


__all__ = [
    "LISTED",
    "PROCESS_TIMEOUT",
    "SOURCE_SNAPSHOT",
    "SOURCE_TASKLIST",
    "TASKLIST_ARGS",
    "UNAVAILABLE",
    "ClaudeProcess",
    "ClaudeProcessesResult",
    "list_claude_processes",
    "parse_tasklist",
]
