"""The two launch-time starts in docs/local-trust-contract.md (amendments A and B). Pure Python: no Qt import.

Each start runs at most once, when the desktop launches, and only when its own setting is on:

- Zotero (amendment A): only when nothing answers on 127.0.0.1:23119, and only when Zotero is found in its
  usual install folder. It starts once, through zotero_client.start_zotero, which logs the attempt.
- The local server (amendment B): only when the core checkout and its Python are set, nothing answers at the
  local URL, and the runner reports the local scope not running. It runs the fixed local start once, through
  runner_controls, which logs the attempt. The local URL check is an extra guard: the core's server stops any
  program already using its port before it starts, so a server the owner runs by hand must not be replaced.

Neither start restarts anything, and an unknown state is never treated as not running. The caller supplies
every outside call through LaunchDeps, so the tests use fakes. Each step catches its own failure and reports
it in plain words, with a class name only when there is no fixed message.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meridian_desktop.local_client import HEALTH_NOT_RUNNING
from meridian_desktop.runner_controls import CONFIRMED, ControlResult
from meridian_desktop.tunnel_status import NOT_RUNNING
from meridian_desktop.zotero_client import API_NOT_RUNNING, ZoteroError

PROGRAM_ZOTERO = "Zotero"
PROGRAM_LOCAL = "Local server"

STARTED = "started"
SKIPPED = "skipped"
FAILED = "failed"

ZOTERO_STARTED_TEXT = "Zotero was started once, at launch."
ZOTERO_ANSWERING_TEXT = "Zotero's local API already answers, or could not be checked, on 127.0.0.1:23119. Nothing was started."
ZOTERO_NOT_INSTALLED_TEXT = "Zotero is not installed in a usual folder, so it was not started at launch."
ZOTERO_FAILED_TEXT = "Zotero could not be started at launch ({})."
LOCAL_CORE_NOT_SET_TEXT = "The core checkout and its Python are not set, so the local server was not started at launch."
LOCAL_ANSWERING_TEXT = "Something already answers at the local URL, so the local server was not started at launch."
LOCAL_STATUS_TEXT = "The runner does not report the local server as not running, so it was not started at launch."
LOCAL_FAILED_TEXT = "The local server was not started at launch. {}"
LOCAL_STARTED_TEXT = "The local server was started once, at launch."


@dataclass(frozen=True)
class LaunchNote:
    """One step's result: the program it is about, STARTED, SKIPPED or FAILED, and one plain sentence."""

    program: str
    outcome: str
    message: str


@dataclass(frozen=True)
class LaunchDeps:
    """Every outside call the launch-time starts make. Each is a fake in the tests.

    zotero_probe() returns an object with a ``state`` (zotero_client.probe_api). zotero_exe() returns the zotero.exe
    path or None. zotero_start(exe) starts it and raises ZoteroError on failure. local_health() returns a
    local_client HEALTH_* word for the saved local URL. local_status() and local_start() return a ControlResult
    for the local scope. core_ready says whether the core checkout and its Python are set.
    """

    zotero_probe: Callable[[], Any]
    zotero_exe: Callable[[], str | None]
    zotero_start: Callable[[str], str]
    local_health: Callable[[], str]
    local_status: Callable[[], ControlResult]
    local_start: Callable[[], ControlResult]
    core_ready: bool


def _zotero_step(deps: LaunchDeps) -> LaunchNote:
    try:
        check = deps.zotero_probe()
        if check.state != API_NOT_RUNNING:
            return LaunchNote(PROGRAM_ZOTERO, SKIPPED, ZOTERO_ANSWERING_TEXT)
        exe = deps.zotero_exe()
        if exe is None:
            return LaunchNote(PROGRAM_ZOTERO, SKIPPED, ZOTERO_NOT_INSTALLED_TEXT)
        deps.zotero_start(exe)
    except ZoteroError as exc:
        return LaunchNote(PROGRAM_ZOTERO, FAILED, ZOTERO_FAILED_TEXT.format(exc.message))
    except Exception as exc:  # noqa: BLE001 - a launch step never raises into the window
        return LaunchNote(PROGRAM_ZOTERO, FAILED, ZOTERO_FAILED_TEXT.format(exc.__class__.__name__))
    return LaunchNote(PROGRAM_ZOTERO, STARTED, ZOTERO_STARTED_TEXT)


def _local_step(deps: LaunchDeps) -> LaunchNote:
    if not deps.core_ready:
        return LaunchNote(PROGRAM_LOCAL, SKIPPED, LOCAL_CORE_NOT_SET_TEXT)
    try:
        if deps.local_health() != HEALTH_NOT_RUNNING:
            return LaunchNote(PROGRAM_LOCAL, SKIPPED, LOCAL_ANSWERING_TEXT)
        if deps.local_status().state != NOT_RUNNING:
            return LaunchNote(PROGRAM_LOCAL, SKIPPED, LOCAL_STATUS_TEXT)
        result = deps.local_start()
    except Exception as exc:  # noqa: BLE001 - a launch step never raises into the window
        return LaunchNote(PROGRAM_LOCAL, FAILED, LOCAL_FAILED_TEXT.format(exc.__class__.__name__))
    if result.outcome == CONFIRMED:
        return LaunchNote(PROGRAM_LOCAL, STARTED, LOCAL_STARTED_TEXT)
    return LaunchNote(PROGRAM_LOCAL, FAILED, LOCAL_FAILED_TEXT.format(result.message))


def run_launch_starts(zotero_on: bool, local_on: bool, deps: LaunchDeps) -> tuple[LaunchNote, ...]:
    """Run each start whose setting is on, once, in order: Zotero, then the local server. Off means no call at all."""
    notes: list[LaunchNote] = []
    if zotero_on:
        notes.append(_zotero_step(deps))
    if local_on:
        notes.append(_local_step(deps))
    return tuple(notes)


__all__ = [
    "FAILED",
    "PROGRAM_LOCAL",
    "PROGRAM_ZOTERO",
    "SKIPPED",
    "STARTED",
    "LaunchDeps",
    "LaunchNote",
    "run_launch_starts",
]
