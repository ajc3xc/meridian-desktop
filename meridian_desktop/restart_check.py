"""Tell the owner when this window's code, or the local Meridian server it started, has changed on disk. Pure Python: no Qt.

RestartWatch takes a baseline when it is built: the file, size and modification time of every meridian_desktop module
loaded so far, plus the executable when the app is frozen. Each check stats those same files again. A file that has a
different size or modification time, or that is gone, counts as changed. A file that is new on disk is ignored, because
the running code never read it. Modules imported later join the baseline at the first check after they load.

Only stat calls are made. No file contents are read, so a check is cheap enough for a UI timer.

The server case needs a process to watch. note_server_started is called with the PID that a successful local start
reported, and the server's creation time is read from the operating system. A ``.py`` file under the core checkout's
meridian folder that is newer than that creation time means the running server predates the file. The watch drops
the server when the PID is gone or its creation time changes, which means the PID was reused. A server that was already
running before the owner pressed Start is not watched. Its creation time is not known to this window.

Restart never happens here. The window only says that a restart is needed.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE = "meridian_desktop"
# The core package that the runner imports. The server watch looks at its .py files only.
SERVER_PACKAGE = "meridian"
# The most names the banner lists. The rest are counted.
MAX_NAMES = 3
# Windows FILETIME ticks (100 ns) from 1601-01-01 to the Unix epoch.
_EPOCH_TICKS = 116_444_736_000_000_000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SEPARATORS = re.compile(r"[\\/]+")


@dataclass(frozen=True)
class FileStamp:
    """The two numbers a check compares. A file is changed when either one differs."""

    mtime_ns: int
    size: int


@dataclass(frozen=True)
class Restart:
    """What changed. ``code`` holds the display names of changed or removed code files.

    ``server`` is True when the server this window started is stale.
    """

    code: tuple[str, ...] = ()
    server: bool = False

    @property
    def needed(self) -> bool:
        return bool(self.code) or self.server


@dataclass(frozen=True)
class _Server:
    pid: int
    started_ns: int
    source: Path


def process_started_ns(pid: int) -> int | None:
    """The creation time of a process in Unix nanoseconds, or None when it cannot be read.

    None covers a process that has exited, a PID the owner cannot query, and every platform except Windows. The
    Windows call is GetProcessTimes through ctypes, so the app needs no new package.
    """
    if sys.platform != "win32" or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)
    return (ticks - _EPOCH_TICKS) * 100


def stamp(path: str) -> FileStamp | None:
    """The stamp of one file, or None when it does not exist now. Only stat is called."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return FileStamp(info.st_mtime_ns, info.st_size)


def package_files(modules: Mapping[str, Any]) -> set[str]:
    """The ``__file__`` of every meridian_desktop module in ``modules``, package __init__ files included."""
    files: set[str] = set()
    for name, module in list(modules.items()):
        if name != PACKAGE and not name.startswith(PACKAGE + "."):
            continue
        file = getattr(module, "__file__", None)
        if isinstance(file, str) and file:
            files.add(file)
    return files


def newest_source_mtime_ns(root: Path) -> int | None:
    """The newest modification time of any .py file under ``root``, or None when there is none. Only stat is called."""
    newest: int | None = None
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            try:
                mtime = os.stat(os.path.join(dirpath, name)).st_mtime_ns
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    return newest


def display_name(path: str) -> str:
    """The name the banner shows: from the package folder down, for example meridian_desktop/ui/main_window.py."""
    segments = [part for part in _SEPARATORS.split(path) if part]
    if PACKAGE in segments:
        index = len(segments) - 1 - segments[::-1].index(PACKAGE)
        return "/".join(segments[index:])
    return segments[-1] if segments else path


def banner_text(restart: Restart) -> str:
    """The sentence the banner shows, or "" when no restart is needed."""
    if not restart.needed:
        return ""
    sentences: list[str] = []
    if restart.code:
        names = ", ".join(restart.code[:MAX_NAMES])
        extra = len(restart.code) - MAX_NAMES
        if extra > 0:
            names += f" and {extra} more"
        sentences.append(f"Restart needed: {names} changed on disk since this window opened.")
    if restart.server:
        if sentences:
            sentences.append("The local Meridian server changed on disk since it started.")
        else:
            sentences.append("Restart needed: the local Meridian server changed on disk since it started.")
    return " ".join(sentences)


class RestartWatch:
    """The baseline for this window's code, and the local server it started, if any. Read on the GUI thread."""

    def __init__(
        self,
        *,
        modules: Mapping[str, Any] | None = None,
        frozen: bool | None = None,
        executable: str | None = None,
        started_ns_of: Callable[[int], int | None] = process_started_ns,
    ) -> None:
        self._modules: Mapping[str, Any] = sys.modules if modules is None else modules
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._executable = sys.executable if executable is None else executable
        self._started_ns_of = started_ns_of
        self._stamps: dict[str, FileStamp] = {}
        # Replaced as a whole, never edited, so a start on a worker thread and a check on the GUI thread cannot tear it.
        self._server: _Server | None = None
        self._add_loaded()

    def _code_paths(self) -> set[str]:
        paths = package_files(self._modules)
        if self._frozen and self._executable:
            paths.add(self._executable)
        return paths

    def _add_loaded(self) -> None:
        for path in self._code_paths():
            if path in self._stamps:
                continue
            found = stamp(path)
            if found is not None:
                self._stamps[path] = found

    def note_server_started(self, pid: int, checkout: str) -> None:
        """Watch the server that a successful local start reported. Called on the thread that ran the start."""
        if not checkout:
            return
        started = self._started_ns_of(pid)
        if started is None:
            return
        self._server = _Server(pid, started, Path(checkout) / SERVER_PACKAGE)

    def _server_stale(self) -> bool:
        server = self._server
        if server is None:
            return False
        if self._started_ns_of(server.pid) != server.started_ns:
            # The process has exited, or its PID now belongs to another process. Nothing is watched any more.
            self._server = None
            return False
        newest = newest_source_mtime_ns(server.source)
        return newest is not None and newest > server.started_ns

    def check(self) -> Restart:
        """Compare the baseline with the disk now. Modules loaded since the last check join the baseline first."""
        self._add_loaded()
        changed = [path for path, before in self._stamps.items() if stamp(path) != before]
        names = tuple(sorted(display_name(path) for path in changed))
        return Restart(code=names, server=self._server_stale())
