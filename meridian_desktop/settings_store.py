"""Settings store for the desktop shell. Pure Python: no Qt import.

File: %LOCALAPPDATA%\\meridian-desktop\\settings.json, or
~/.meridian-desktop/settings.json when LOCALAPPDATA is unset.

Reads never raise. A missing file gives defaults with no warning. A corrupt or
unreadable file gives defaults plus a warning string. Writes are atomic: a temp
file in the same directory, then os.replace.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping

# local: the Meridian server or database file on this computer. hosted: the hosted Meridian account.
# tunnel: the hosted account, which reaches this computer through the tunnel. Its projects are read
# the same way as hosted. A saved file with local or hosted reads exactly as it did before.
VALID_MODES = ("local", "hosted", "tunnel")
DEFAULT_MODE = "local"
DEFAULT_HOSTED_URL = "https://usemeridian.us"
DEFAULT_LOCAL_URL = "http://127.0.0.1:7878"
DEFAULT_ENGINE_URL = "http://127.0.0.1:8471"

_URL_KEYS = ("hosted_url", "local_url", "engine_url")

# The Meridian core checkout (the folder that holds the meridian package) and a Python program that
# can import it. The client setup runner uses both. Only paths are stored, never a secret.
CORE_CHECKOUT_KEY = "core_checkout"
CORE_PYTHON_KEY = "core_python"
CORE_KEYS = (CORE_CHECKOUT_KEY, CORE_PYTHON_KEY)

# The three owner-confirmed control switches. Each is a real true or false, and an absent key means off.
CONTROL_FLAG_KEYS = ("tunnel_control_enabled", "zotero_autostart", "local_server_autostart")


@dataclass(frozen=True)
class Settings:
    mode: str = DEFAULT_MODE
    hosted_url: str = DEFAULT_HOSTED_URL
    local_url: str = DEFAULT_LOCAL_URL
    engine_url: str = DEFAULT_ENGINE_URL
    # Path to the Meridian database file that the read-only reader opens. None means the
    # user has not picked one yet. Only a path is stored here, never a token or a secret.
    db_path: str | None = None
    # Start Meridian Desktop when the user signs in to Windows. Off by default. install.py
    # writes and removes the per-user Run entry. save() writes this key only when it is on,
    # so a file that never opted in keeps the five keys it has always had.
    autostart: bool = False
    # The core checkout and its Python, both "" when not set. save() writes each one only when it
    # is set, so a file that never set them keeps the keys it had before.
    core_checkout: str = ""
    core_python: str = ""
    # Check GitHub once at a real start for a newer release. The check makes one request for the release
    # JSON and downloads nothing. On by default. save() writes this key only when it is off, so a file that
    # never turned the check off is written exactly as before.
    check_updates: bool = True
    # The owner-confirmed runner controls (docs/local-trust-contract.md). Each is off by default. save() writes a key
    # only when it is on, so a file that never turned one on keeps the keys it had before. tunnel_control_enabled
    # switches the tunnel start, stop and restart buttons. zotero_autostart and local_server_autostart start Zotero
    # and the local server once at launch, and each is turned on only after the owner confirms the dialog.
    tunnel_control_enabled: bool = False
    zotero_autostart: bool = False
    local_server_autostart: bool = False


@dataclass(frozen=True)
class LoadResult:
    settings: Settings
    warning: str | None = None


def default_settings_path(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "meridian-desktop" / "settings.json"
    base = home if home is not None else Path.home()
    return base / ".meridian-desktop" / "settings.json"


def core_path_problem(key: str, value: str) -> str | None:
    """Plain words for why a core setting cannot be saved, or None when it can.

    An empty value means "not set" and is always allowed, so a setting can be cleared. A checkout
    must be an existing folder that holds the meridian package. A Python must be an existing file.
    """
    if key not in CORE_KEYS:
        raise ValueError(f"unknown core setting: {key}")
    text = value.strip()
    if not text:
        return None
    path = Path(text)
    if key == CORE_CHECKOUT_KEY:
        if not path.is_dir():
            return (
                f"Not saved. The Meridian core checkout folder does not exist: {text}. "
                "Choose the folder that holds the meridian package."
            )
        if not (path / "meridian" / "__init__.py").is_file():
            return (
                f"Not saved. {text} is not a Meridian core checkout: it has no meridian package. "
                "Choose the folder that holds meridian/."
            )
        return None
    if not path.is_file():
        return (
            f"Not saved. The Python program does not exist: {text}. "
            "Choose the python.exe that can import meridian."
        )
    return None


def _from_mapping(data: Mapping) -> tuple[Settings, list[str]]:
    problems: list[str] = []
    values: dict[str, object] = {}

    mode = data.get("mode", DEFAULT_MODE)
    if mode in VALID_MODES:
        values["mode"] = mode
    else:
        problems.append(f"mode must be one of {VALID_MODES}; using {DEFAULT_MODE!r}")

    for key in _URL_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str) and value.strip():
            values[key] = value
        else:
            problems.append(f"{key} must be a non-empty string; using its default")

    # db_path: an absent key or null means "not chosen" (None) with no warning, so a file
    # saved with db_path unset reads back clean. A blank or non-string value is refused.
    if "db_path" in data:
        value = data["db_path"]
        if value is None:
            values["db_path"] = None
        elif isinstance(value, str) and value.strip():
            values["db_path"] = value
        else:
            problems.append("db_path must be a non-empty string or null; using None")

    # autostart: an absent key means off, with no warning. Only a real true or false is accepted.
    if "autostart" in data:
        value = data["autostart"]
        if isinstance(value, bool):
            values["autostart"] = value
        else:
            problems.append("autostart must be true or false; using false")

    # core_checkout and core_python: an absent key means "not set", with no warning. Only text is
    # accepted. Existence is not checked here, so a drive that is offline does not break loading.
    for key in CORE_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            values[key] = value.strip()
        else:
            problems.append(f"{key} must be a path as text or empty; using empty")

    # check_updates: an absent key means on, with no warning. Only a real true or false is accepted.
    if "check_updates" in data:
        value = data["check_updates"]
        if isinstance(value, bool):
            values["check_updates"] = value
        else:
            problems.append("check_updates must be true or false; using true")

    # The runner control switches: an absent key means off, with no warning. Only a real true or false is accepted.
    for key in CONTROL_FLAG_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            values[key] = value
        else:
            problems.append(f"{key} must be true or false; using false")

    return replace(Settings(), **values), problems


class SettingsStore:
    """Load and save the desktop settings at one path. The path is injectable."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_settings_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> LoadResult:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return LoadResult(Settings())
        except (OSError, ValueError) as exc:
            return LoadResult(
                Settings(),
                f"settings file unreadable ({exc.__class__.__name__}); using defaults",
            )

        try:
            data = json.loads(raw)
        except ValueError:
            return LoadResult(Settings(), "settings file is corrupt (not valid JSON); using defaults")

        if not isinstance(data, dict):
            return LoadResult(Settings(), "settings file does not hold a JSON object; using defaults")

        settings, problems = _from_mapping(data)
        return LoadResult(settings, "; ".join(problems) or None)

    def save(self, settings: Settings) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        data = asdict(settings)
        if not settings.autostart:
            # Off is the default, so a file that never opted in is written exactly as before.
            del data["autostart"]
        for key in CORE_KEYS:
            if not data[key]:
                # "Not set" is the default, so a file that never set a core path has no such key.
                del data[key]
        if settings.check_updates:
            # On is the default, so a file that never turned the update check off has no such key.
            del data["check_updates"]
        for key in CONTROL_FLAG_KEYS:
            if not data[key]:
                # Off is the default, so a file that never turned a runner control on has no such key.
                del data[key]
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def set_mode(self, mode: str) -> Settings:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        updated = replace(self.load().settings, mode=mode)
        self.save(updated)
        return updated

    def set_core_path(self, key: str, value: str) -> Settings:
        """Validate and save one core setting. Raises ValueError with plain words when it is refused.

        A refused value writes nothing. Every other field in the saved file is kept.
        """
        text = value.strip()
        problem = core_path_problem(key, text)
        if problem is not None:
            raise ValueError(problem)
        updated = replace(self.load().settings, **{key: text})
        self.save(updated)
        return updated
