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

VALID_MODES = ("local", "hosted")
DEFAULT_MODE = "local"
DEFAULT_HOSTED_URL = "https://usemeridian.us"
DEFAULT_LOCAL_URL = "http://127.0.0.1:7878"
DEFAULT_ENGINE_URL = "http://127.0.0.1:8471"

_URL_KEYS = ("hosted_url", "local_url", "engine_url")


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
