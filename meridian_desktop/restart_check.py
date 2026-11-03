"""Tell the owner when this window's code, or the local server it started, has changed on disk. Pure Python: no Qt.

RestartWatch takes a baseline of the meridian_desktop modules loaded so far: the package files, the package __init__
files, and __main__ when it sits in the package folder. The executable is added when the app is frozen. Each check
stats those same files again. A file with a different size or modification time counts as changed, and so does a file
that is gone. Any other OSError keeps the previous stamp, because a locked or half-written file is not a removed one.
A new file on disk is ignored, because the running code never read it.

Only stat calls are made on the GUI thread. No file contents are read, so a check is cheap enough for a UI timer.

Limit: modules are recorded at the first check after the watch is built, not at import. A module that changes between
its own import and that first check is not caught. A module loaded later is recorded at the first check that sees it,
and the same limit applies to it.

The server case needs a process to watch. note_server_started is called on the thread that ran the start, with the PID
that a successful local start reported. It reads the process creation time, then takes a snapshot of every .py file
under the core checkout's meridian folder: size and modification time. Each check stats only those files. A file that
changed or is gone means the running server predates it. The snapshot is taken when the start is confirmed, so an edit
made between the process starting and that moment is not seen. A .py file added after the snapshot is not seen either.

The watch drops the server when the process has exited or its PID now belongs to another process, which shows as a
different creation time. A creation time that cannot be read right now keeps the watch, and the next check reads it
again. Creation times are read through Windows, so the server case is off on other platforms. A server that was already
running before the owner pressed Start is not watched. This window does not know when that process started.

Restart never happens here. The window only says that a restart is needed.
"""

from __future__ import annotations

import os
import re
import sys
import threading
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
_ERROR_INVALID_PARAMETER = 87
_SEPARATORS = re.compile(r"[\\/]+")


class ProcessGone:
    """What a process reader returns when the process is not running, or the PID cannot name a process."""

    def __repr__(self) -> str:
        return "GONE"


GONE = ProcessGone()


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
    files: Mapping[str, FileStamp]


def process_started_ns(pid: int) -> int | None | ProcessGone:
    """The creation time of a process in Unix nanoseconds, GONE, or None when it cannot be read right now.

    GONE covers a PID that cannot name a process, and a process whose exit time is already set, even if this program
    still holds a handle to it. None covers a query that failed for another reason, and every platform except Windows.
    The Windows calls are made through ctypes, so the app needs no new package.
    """
    if pid <= 0:
        return GONE
    if sys.platform != "win32":
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
        if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
            return GONE
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return None
        if exited.dwLowDateTime or exited.dwHighDateTime:
            return GONE
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)
    return (ticks - _EPOCH_TICKS) * 100


def stamp(path: str) -> FileStamp | None:
    """The stamp of one file, or None when the file is gone. Only stat is called.

    Any other OSError is raised. The caller then keeps the stamp it already has.
    """
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return FileStamp(info.st_mtime_ns, info.st_size)


def package_files(modules: Mapping[str, Any]) -> set[str]:
    """The ``__file__`` of every meridian_desktop module in ``modules``, __init__ files included.

    The app's __main__ is included when it sits in the package folder, which is the case under ``python -m``.
    """
    files: set[str] = set()
    for name, module in list(modules.items()):
        file = getattr(module, "__file__", None)
        if not isinstance(file, str) or not file:
            continue
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            files.add(file)
        elif name == "__main__" and _is_package_main(file):
            files.add(file)
    return files


def _is_package_main(file: str) -> bool:
    segments = [part for part in _SEPARATORS.split(file) if part]
    return len(segments) >= 2 and segments[-1] == "__main__.py" and segments[-2] == PACKAGE


def server_source_stamps(root: Path) -> dict[str, FileStamp]:
    """The stamp of every .py file under ``root``, read once. Unreadable files are left out. Only stat is called."""
    stamps: dict[str, FileStamp] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            stamps[path] = FileStamp(info.st_mtime_ns, info.st_size)
    return stamps


def _any_changed(snapshot: Mapping[str, FileStamp]) -> bool:
    """True when a snapshot file is gone or differs from its stamp. A file that cannot be read now is not a change."""
    for path, before in snapshot.items():
        try:
            now = stamp(path)
        except OSError:
            continue
        if now != before:
            return True
    return False


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
    """The baseline for this window's code, and the local server it started, if any.

    ``check`` and ``note_server_started`` may run on different threads. The server record is replaced under a lock as a
    whole, so a check never reads a half-written record.
    """

    def __init__(
        self,
        *,
        modules: Mapping[str, Any] | None = None,
        frozen: bool | None = None,
        executable: str | None = None,
        started_ns_of: Callable[[int], int | None | ProcessGone] = process_started_ns,
    ) -> None:
        self._modules: Mapping[str, Any] = sys.modules if modules is None else modules
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._executable = sys.executable if executable is None else executable
        self._started_ns_of = started_ns_of
        self._stamps: dict[str, FileStamp] = {}
        self._lock = threading.Lock()
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
            try:
                found = stamp(path)
            except OSError:
                # Unreadable right now. It is tried again on the next check, and it is not dropped.
                continue
            if found is not None:
                self._stamps[path] = found

    def note_server_started(self, pid: int, checkout: str) -> None:
        """Watch the server that a successful local start reported. Called on the thread that ran the start."""
        if not checkout:
            return
        started = self._started_ns_of(pid)
        if not isinstance(started, int):
            # GONE or unreadable right now. Nothing is watched, and the owner can start the server again.
            return
        server = _Server(pid, started, server_source_stamps(Path(checkout) / SERVER_PACKAGE))
        with self._lock:
            self._server = server

    def _server_stale(self) -> bool:
        with self._lock:
            server = self._server
        if server is None:
            return False
        state = self._started_ns_of(server.pid)
        if state is GONE or (isinstance(state, int) and state != server.started_ns):
            # The process has exited, or its PID now belongs to another process. Clear this record only if it is
            # still the one that was read. A newer start may have replaced it while the reader ran.
            with self._lock:
                if self._server is server:
                    self._server = None
            return False
        # A creation time that cannot be read right now keeps the watch. The files are still compared.
        return _any_changed(server.files)

    def check(self) -> Restart:
        """Compare the baseline with the disk now. Modules loaded since the last check join the baseline first."""
        self._add_loaded()
        changed: list[str] = []
        for path, before in list(self._stamps.items()):
            try:
                now = stamp(path)
            except OSError:
                # Unreadable right now. The previous stamp stays, so this is not a change.
                continue
            if now != before:
                changed.append(path)
        names = tuple(sorted(display_name(path) for path in changed))
        return Restart(code=names, server=self._server_stale())
