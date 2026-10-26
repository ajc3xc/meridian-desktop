"""The LaTeX engine's port and its start, for the Overleaf card on Home. Pure Python: stdlib only, no Qt import.

    engine_listening(base_url)  is something listening on the engine's TCP port? A plain TCP connect, no HTTP request.
    has_starter(folder)         does this folder hold the engine's hidden starter? Reads the folder only.
    engine_folder(settings)     the folder the owner chose and saved, when it still holds the starter, or None.
    start_engine(folder)        Windows only. Starts the engine's hidden starter in the background and returns at once.

start_engine never kills, stops or restarts a process. The card asks the owner first, or the owner's launch switch says yes.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.settings_store import Settings

STARTER_NAME = "start_server_hidden.vbs"
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
    """The outcome of one start. ``reason`` is fixed text, set only when the start did not happen."""

    started: bool
    reason: str = ""


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
    if not (folder / STARTER_NAME).is_file():
        return EngineStart(False, NO_STARTER_TEXT)
    launch = subprocess.Popen if launcher is None else launcher
    command = [_wscript(), "//B", "//nologo", STARTER_NAME]
    try:
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
        return EngineStart(False, START_FAILED_TEXT.format(exc.__class__.__name__))
    return EngineStart(True)


__all__ = [
    "PROBE_TIMEOUT",
    "STARTER_NAME",
    "EngineStart",
    "engine_folder",
    "engine_listening",
    "has_starter",
    "start_engine",
]
