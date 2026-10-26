"""The LaTeX engine's port and its start, for the Overleaf card on Home. Pure Python: stdlib only, no Qt import.

    engine_listening(base_url)  is something listening on the engine's TCP port? A plain TCP connect, no HTTP request.
    has_starter(folder)         does this folder hold the engine's hidden starter? Reads the folder only.
    engine_folder(settings)     the folder the owner chose and saved, when it still holds the starter, or None.
    start_engine(folder)        Windows only. Starts the engine's hidden starter in the background and returns at once.
    log_engine_start(...)       appends one line to the control log for one start from the card. Never raises.

start_engine never kills, stops or restarts a process. The card asks the owner first, or the owner's launch switch says yes,
and at launch only for the folder the owner confirmed (docs/local-trust-contract.md, Amendment G).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.runner_controls import append_control_line, default_log_path
from meridian_desktop.settings_store import Settings

STARTER_NAME = "start_server_hidden.vbs"
# The fixed arguments that go after wscript.exe to run the starter. start_engine builds its argument list from them, and every
# dialog that names the command shows COMMAND_TEXT, so the words the owner reads are the words that run.
WSCRIPT_ARGS = ("//B", "//nologo")
COMMAND_TEXT = " ".join(("wscript.exe", *WSCRIPT_ARGS, STARTER_NAME))
PROBE_TIMEOUT = 0.5
WINDOWS_ONLY_TEXT = "Starting the LaTeX engine is only supported on Windows. Nothing was started."
NO_STARTER_TEXT = "The LaTeX engine folder has no starter file. Nothing was started."
START_FAILED_TEXT = "The LaTeX engine could not be started ({})."
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# connector(host, port, timeout) opens one TCP connection and closes it. It raises OSError when nothing listens.
Connector = Callable[[str, int, float], None]


def _connect(host: str, port: int, timeout: float) -> None:
    socket.create_connection((host, port), timeout=timeout).close()


def engine_listening(base_url: str, *, connector: Connector | None = None, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when something accepts a TCP connection on the engine's loopback port. Sends no HTTP request.

    A non-loopback or port-less address returns False without a connection. Any connection failure returns False.
    """
    try:
        local_client.assert_loopback(base_url)
        parts = urllib.parse.urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except (LocalClientError, ValueError):
        return False
    if host is None or port is None:
        return False
    connect = _connect if connector is None else connector
    try:
        connect(host, port, timeout)
    except OSError:
        return False
    return True


def has_starter(folder: Path) -> bool:
    """True when ``folder`` holds the engine's hidden starter. It only looks at the folder; nothing is started."""
    try:
        return (folder / STARTER_NAME).is_file()
    except (OSError, ValueError):
        return False


def engine_folder(settings: Settings) -> Path | None:
    """The folder the owner chose for the LaTeX engine, or None when none is chosen or the chosen one has no starter.

    The folder comes only from the saved settings (Settings.engine_folder), and the owner picks it in the Overleaf card.
    No folder is named in code, so nothing here guesses where the engine is installed. A folder that no longer holds the
    starter reads as None, so the card asks the owner to choose it again.
    """
    chosen = settings.engine_folder.strip()
    if not chosen:
        return None
    folder = Path(chosen)
    return folder if has_starter(folder) else None


@dataclass(frozen=True)
class EngineStart:
    """The outcome of one start. ``reason`` is fixed text, set only when the start did not happen.

    ``failure`` is the class name of the error that stopped the start, or "" when none did. It is for the control log only,
    so it is left out of equality: two results with the same ``started`` and ``reason`` compare equal, as before.
    """

    started: bool
    reason: str = ""
    failure: str = field(default="", compare=False)


def _wscript() -> str:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(system_root, "System32", "wscript.exe")


def start_engine(
    folder: Path,
    *,
    platform: str | None = None,
    launcher: Callable[..., Any] | None = None,
) -> EngineStart:
    """Start the engine's hidden starter from ``folder``, detached and with no window, and return at once.

    Windows only. Elsewhere it returns a result that says so, and starts nothing. Call this only after the owner says yes
    to Start engine, or when the owner's launch switch is on and the folder is the one the owner chose.
    ``launcher`` replaces subprocess.Popen in tests, so no test starts a process.
    """
    if (sys.platform if platform is None else platform) != "win32":
        return EngineStart(False, WINDOWS_ONLY_TEXT)
    launch = subprocess.Popen if launcher is None else launcher
    command = [_wscript(), *WSCRIPT_ARGS, STARTER_NAME]
    try:
        # The starter check is inside the try. A check that raises (PermissionError, for example) is a start that failed, so it
        # is reported and logged by class name like a failed launch, and never escapes to the caller.
        if not (folder / STARTER_NAME).is_file():
            return EngineStart(False, NO_STARTER_TEXT)
        launch(
            command,
            cwd=str(folder),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_DETACHED,
        )
    except Exception as exc:  # noqa: BLE001 - any start failure is reported by class name only
        name = exc.__class__.__name__
        return EngineStart(False, START_FAILED_TEXT.format(name), failure=name)
    return EngineStart(True)


# The control log line for one engine start. The program is fixed text, and no pid is written: start_engine does not report
# one. Only the class name of a failure is written, never the error's message, the engine's address or any output.
ENGINE_LOG_PROGRAM = f"wscript.exe {STARTER_NAME}"


def _local_now() -> datetime:
    return datetime.now().astimezone()


def engine_log_line(stamp: str, running: bool, failure: str = "") -> str:
    """One control log line: the local time in ISO form, the program, the outcome, and the failure class name when there is one.

    ``running`` is True when the engine's port answered after the start, so the outcome reads "started" exactly when the
    card's own line says the engine started.
    """
    outcome = "started" if running else "not started"
    line = f'{stamp} program="{ENGINE_LOG_PROGRAM}" outcome="{outcome}"'
    return f"{line} failure={failure}" if failure else line


def log_engine_start(
    settings_path: Path,
    result: EngineStart,
    running: bool,
    *,
    clock: Callable[[], datetime] = _local_now,
) -> None:
    """Append one line to the control log beside the settings file, for one start from the Overleaf card.

    It is the same control log the runner calls write (runner_controls.append_control_line). It never raises: a log that
    cannot be written must not reach the card or change its text.
    """
    try:
        line = engine_log_line(clock().isoformat(timespec="seconds"), running, result.failure)
        append_control_line(default_log_path(settings_path), line)
    except Exception:  # noqa: BLE001 - a logging failure is not the owner's concern and must not change the card
        return


__all__ = [
    "COMMAND_TEXT",
    "ENGINE_LOG_PROGRAM",
    "PROBE_TIMEOUT",
    "STARTER_NAME",
    "WSCRIPT_ARGS",
    "EngineStart",
    "engine_folder",
    "engine_listening",
    "engine_log_line",
    "has_starter",
    "log_engine_start",
    "start_engine",
]
