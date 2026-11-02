"""The agent programs running on this computer, and how their processes nest. Pure Python: no Qt import.

The registry holds the image names this desktop counts, each with the label it is shown under: claude.exe (Claude),
codex.exe (Codex) and cursor.exe (Cursor). Owner-listed custom names come from the settings key agent_process_names.
A custom name must be a bare file name that ends in .exe, is 5 to 64 characters long, and holds only letters, digits,
spaces and . _ ( ) + -. So no slash, no backslash and no colon (which rules out a drive letter) can be kept. Any other
entry is dropped and counted, never raised as an error. Names match without regard to case.

The process table is read with the Windows toolhelp snapshot (CreateToolhelp32Snapshot, Process32FirstW and
Process32NextW) through ctypes. Each entry keeps three values only: the process id, the parent process id and the
executable's file name. No process is opened, so no command line, path, argument or user name is read. When the
snapshot cannot be taken, snapshot_processes gives None, and the caller falls back to tasklist (see claude_processes).

Grouping, for each label: a root is a matching process whose parent is not a matching process of the same label, so a
program started by another agent program is a root of its own label. The total is every matching process of that label
under one of its roots, found by walking up through parents of the same label.

Nothing here starts, stops or changes a process.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

# The image names this desktop counts, each with the label it is shown under. The built-in programs come first.
BUILT_IN_PROGRAMS: tuple[tuple[str, str], ...] = (
    ("claude.exe", "Claude"),
    ("codex.exe", "Codex"),
    ("cursor.exe", "Cursor"),
)
EXE_SUFFIX = ".exe"
CUSTOM_MIN_LENGTH = 5
CUSTOM_MAX_LENGTH = 64
# The part of a custom name before .exe. It starts with a letter or digit, and it does not end in a dot or a space.
_CUSTOM_STEM = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._ ()+-]*[A-Za-z0-9_()+-])?")

TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
MAX_PATH_CHARS = 260


@dataclass(frozen=True)
class AgentProcess:
    """One process: its image name as Windows reports it, its process id and its parent's id. tasklist gives no parent,
    so the entries it reads keep 0 there. Nothing else about a process is kept."""

    image: str
    pid: int
    ppid: int = 0


@dataclass(frozen=True)
class AgentGroup:
    """One program's counts: its label, how many of its processes are roots, and how many matching processes it has in
    all, roots included."""

    label: str
    roots: int
    total: int


SnapshotFn = Callable[[], list[AgentProcess] | None]


def custom_label(name: str) -> str:
    """The label a custom name is shown under: its file name without .exe. ``name`` must be a valid custom name."""
    return name[: -len(EXE_SUFFIX)]


def _valid_custom(value: object) -> str | None:
    """The custom name with its surrounding spaces removed, or None when it is not a valid bare file name."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not CUSTOM_MIN_LENGTH <= len(name) <= CUSTOM_MAX_LENGTH:
        return None
    if name[-len(EXE_SUFFIX):].casefold() != EXE_SUFFIX:
        return None
    if _CUSTOM_STEM.fullmatch(name[: -len(EXE_SUFFIX)]) is None:
        return None
    return name


def clean_custom_names(values: Iterable[object]) -> tuple[tuple[str, ...], int]:
    """The valid custom names in their order, and how many entries were dropped as invalid.

    A name already listed, or a built-in program, is skipped without being counted: it is not invalid, only repeated.
    """
    kept: list[str] = []
    seen = {image.casefold() for image, _label in BUILT_IN_PROGRAMS}
    dropped = 0
    for value in values:
        name = _valid_custom(value)
        if name is None:
            dropped += 1
        elif name.casefold() not in seen:
            seen.add(name.casefold())
            kept.append(name)
    return tuple(kept), dropped


def registry(custom_names: Iterable[object] = ()) -> dict[str, str]:
    """The label of each image name, keyed by the name in lower case: the built-in programs first, then the valid
    custom names. An invalid custom name is ignored here; clean_custom_names is the place that counts it."""
    labels = {image.casefold(): label for image, label in BUILT_IN_PROGRAMS}
    for value in custom_names:
        name = _valid_custom(value)
        if name is not None:
            labels.setdefault(name.casefold(), custom_label(name))
    return labels


def matching(processes: Iterable[AgentProcess], labels: Mapping[str, str]) -> tuple[AgentProcess, ...]:
    """The processes whose image name is in the registry, sorted by image name without regard to case, then by id."""
    found = [process for process in processes if process.image.casefold() in labels]
    return tuple(sorted(found, key=lambda process: (process.image.casefold(), process.pid)))


def _root_of(pid: int, matched: Mapping[int, tuple[str, int]]) -> int | None:
    """The root of the same-label chain that holds ``pid``: the top process whose parent is not a matching process of
    its label. None when the chain loops, which a real process table does not do."""
    label = matched[pid][0]
    seen = {pid}
    current = pid
    while True:
        parent_id = matched[current][1]
        parent = matched.get(parent_id)
        if parent is None or parent[0] != label:
            return current
        if parent_id in seen:
            return None
        seen.add(parent_id)
        current = parent_id


def group_processes(processes: Iterable[AgentProcess], labels: Mapping[str, str]) -> tuple[AgentGroup, ...]:
    """One group per label that has a matching process, in registry order. A label with no process has no group."""
    matched: dict[int, tuple[str, int]] = {}
    for process in processes:
        label = labels.get(process.image.casefold())
        if label is not None:
            matched[process.pid] = (label, process.ppid)
    roots: dict[str, int] = {}
    totals: dict[str, int] = {}
    for pid, (label, _ppid) in matched.items():
        top = _root_of(pid, matched)
        if top is None:
            continue
        totals[label] = totals.get(label, 0) + 1
        if top == pid:
            roots[label] = roots.get(label, 0) + 1
    order = dict.fromkeys(labels.values())
    return tuple(
        AgentGroup(label=label, roots=roots.get(label, 0), total=totals[label]) for label in order if label in totals
    )


def snapshot_processes() -> list[AgentProcess] | None:
    """Every process in the toolhelp snapshot, as image name, process id and parent id. None when the snapshot cannot be
    taken: this is not Windows, or a call failed. Never raises, and opens no process."""
    if sys.platform != "win32":
        return None
    try:
        return _read_snapshot()
    except Exception:  # noqa: BLE001 - a failed snapshot is the tasklist fallback, never a raised error
        return None


def _read_snapshot() -> list[AgentProcess] | None:
    """The toolhelp snapshot through ctypes. Windows only; the caller checks the platform first."""
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):  # PROCESSENTRY32W: only the three fields kept below are read.
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH_CHARS),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateToolhelp32Snapshot
    create.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create.restype = wintypes.HANDLE
    first = kernel32.Process32FirstW
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    first.restype = wintypes.BOOL
    following = kernel32.Process32NextW
    following.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    following.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    handle = create(TH32CS_SNAPPROCESS, 0)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        if not first(handle, ctypes.byref(entry)):
            return None
        found: list[AgentProcess] = []
        while True:
            found.append(
                AgentProcess(
                    image=entry.szExeFile,
                    pid=int(entry.th32ProcessID),
                    ppid=int(entry.th32ParentProcessID),
                )
            )
            if not following(handle, ctypes.byref(entry)):
                break
        # The walk ends normally only when the table has no more entries. Any other error means the table is incomplete.
        if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
            return None
        return found
    finally:
        close(handle)


__all__ = [
    "BUILT_IN_PROGRAMS",
    "CUSTOM_MAX_LENGTH",
    "CUSTOM_MIN_LENGTH",
    "EXE_SUFFIX",
    "AgentGroup",
    "AgentProcess",
    "SnapshotFn",
    "clean_custom_names",
    "custom_label",
    "group_processes",
    "matching",
    "registry",
    "snapshot_processes",
]
