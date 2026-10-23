"""Settings store for the desktop shell. Pure Python: no Qt import.

File: %LOCALAPPDATA%\\meridian-desktop\\settings.json, or
~/.meridian-desktop/settings.json when LOCALAPPDATA is unset.

Reads never raise. A missing file gives defaults with no warning. A corrupt or
unreadable file gives defaults plus a warning string, with the tunnel switch off
(fail closed, see load). Writes are atomic: a temp file in the same directory,
then os.replace.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
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
# True when the locator (meridian_desktop/core_locator.py) found that core path, and the owner did not choose it. The
# locator may replace an automatically found path, and never a path the owner saved by hand. Absent means false.
CORE_CHECKOUT_AUTO_KEY = "core_checkout_auto"
CORE_PYTHON_AUTO_KEY = "core_python_auto"
CORE_AUTO_FLAGS = {CORE_CHECKOUT_KEY: CORE_CHECKOUT_AUTO_KEY, CORE_PYTHON_KEY: CORE_PYTHON_AUTO_KEY}
CORE_AUTO_KEYS = tuple(CORE_AUTO_FLAGS.values())

# The mode a run starts in when the file names none and a hosted token is saved (owner decision, 2026-10-23).
HOSTED_MODE = "hosted"

# The three owner-confirmed control switches. Each is a real true or false. An absent key means the default in
# CONTROL_FLAG_DEFAULTS, which is set once below the Settings class.
CONTROL_FLAG_KEYS = ("tunnel_control_enabled", "zotero_autostart", "local_server_autostart")

# The exact values the owner saw in the confirmation dialog when a launch-time switch was turned on (amendments A and B
# of docs/local-trust-contract.md). The launch-time starts run only with these, and never with the current core setup.
# An empty value means the owner has not confirmed one yet.
CONFIRMED_KEYS = ("confirmed_core_python", "confirmed_core_checkout", "confirmed_zotero_exe")


@dataclass(frozen=True)
class Settings:
    mode: str = DEFAULT_MODE
    # True when the file names a valid mode, which the owner chose. A missing file, a corrupt file, or a file with no
    # mode key has no explicit mode: the run then starts in the launch mode (see resolve_launch_mode), and save() does
    # not write a mode key, so the file keeps none until the owner picks one.
    mode_explicit: bool = True
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
    # Install new releases in the background, and check for them once at launch and every six hours after. On by
    # default. The Settings tab shows it as a checkbox. save() writes this key only when it is off. A check runs only
    # when both this and check_updates are on.
    auto_update: bool = True
    # The owner-confirmed runner controls (docs/local-trust-contract.md). save() writes a key only when it differs from
    # its default, so a file that keeps the defaults keeps the keys it had before. tunnel_control_enabled switches the
    # tunnel start, stop and restart buttons, and defaults to true by owner decision on 2026-10-23 (amendment F). Each
    # button still asks first. A malformed value, and a file that is corrupt or unreadable, read it as false, so the
    # buttons fail closed. zotero_autostart and local_server_autostart start Zotero and the local server once at
    # launch, default to off, and are turned on only after the owner confirms the dialog.
    tunnel_control_enabled: bool = True
    zotero_autostart: bool = False
    local_server_autostart: bool = False
    confirmed_core_python: str = ""
    confirmed_core_checkout: str = ""
    confirmed_zotero_exe: str = ""
    # Whether the core checkout and Python were found by the locator rather than chosen by the owner. False by default,
    # and save() writes each one only when it is True.
    core_checkout_auto: bool = False
    core_python_auto: bool = False
    # The names of the saved API keys that are not built in (RunPod is always listed). Only names: a value lives in the
    # keychain and never here. save() writes the key only when there is a name, so a file with no keys is unchanged.
    key_names: tuple[str, ...] = ()


# The default of each switch, read from Settings so the two can never drift apart.
CONTROL_FLAG_DEFAULTS = {key: getattr(Settings(), key) for key in CONTROL_FLAG_KEYS}


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
    values: dict[str, object] = {"mode_explicit": False}

    # mode: a valid mode in the file is explicit and always wins. An absent key is not an explicit mode, with no
    # warning, and neither is an invalid one, which is reported. The launch mode decides for those (see load).
    if "mode" in data:
        mode = data["mode"]
        if mode in VALID_MODES:
            values["mode"] = mode
            values["mode_explicit"] = True
        else:
            problems.append(f"mode must be one of {VALID_MODES}; the saved value is ignored")

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

    # The confirmed values: an absent key means not confirmed, with no warning. The text is kept exactly as saved,
    # because it is what the owner was shown. Only text is accepted.
    for key in CONFIRMED_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            values[key] = value
        else:
            problems.append(f"{key} must be a path as text or empty; using empty")

    # core_checkout_auto and core_python_auto: an absent key means the owner chose the path (false), with no warning.
    for key in CORE_AUTO_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            values[key] = value
        else:
            problems.append(f"{key} must be true or false; using false")
    # key_names: an absent key means no saved names, with no warning. Only a list of non-empty text is accepted.
    if "key_names" in data:
        value = data["key_names"]
        if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
            values["key_names"] = tuple(item.strip() for item in value)
        else:
            problems.append("key_names must be a list of names; using none")

    # check_updates: an absent key means on, with no warning. Only a real true or false is accepted.
    if "check_updates" in data:
        value = data["check_updates"]
        if isinstance(value, bool):
            values["check_updates"] = value
        else:
            problems.append("check_updates must be true or false; using true")

    # auto_update: an absent key means on, with no warning. Only a real true or false is accepted.
    if "auto_update" in data:
        value = data["auto_update"]
        if isinstance(value, bool):
            values["auto_update"] = value
        else:
            problems.append("auto_update must be true or false; using true")

    # The runner control switches: an absent key means its default in CONTROL_FLAG_DEFAULTS (tunnel on, the two launch
    # switches off), with no warning. Only a real true or false is accepted. Any other value is read as false, never as
    # the default, so a malformed tunnel switch fails closed instead of turning the tunnel buttons on.
    for key in CONTROL_FLAG_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            values[key] = value
        else:
            values[key] = False
            problems.append(f"{key} must be true or false; using false")

    return replace(Settings(), **values), problems


REPLACE_ATTEMPTS = 20
REPLACE_WAIT_SECONDS = 0.05


def _replace_with_retry(source: str, target: Path) -> None:
    """Replace ``target`` with ``source``, waiting out a brief Windows file lock.

    On Windows a file another thread has open can refuse the replace for a moment. The write is retried for about a
    second. Any other error, or a lock that outlasts the retries, is raised to the caller unchanged.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_WAIT_SECONDS)


class SettingsStore:
    """Load and save the desktop settings at one path. The path is injectable.

    The launch mode is held for this run only. resolve_launch_mode sets it once at launch, and load() then gives it to
    any settings that name no explicit mode. save() never writes it, so the file stays as the owner left it.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_settings_path()
        self._launch_mode: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def resolve_launch_mode(self, hosted_token_saved: Callable[[], bool]) -> str:
        """Pick the mode this run starts in, hold it for the run, and return it.

        Call once, at launch, before the window.

        A mode the file names always wins. With no explicit mode, the run starts in hosted mode when a hosted token is
        saved, and in local mode otherwise. ``hosted_token_saved`` answers only whether a token exists. It is called
        only when the file has no explicit mode, and the token is never read here.
        """
        settings = self.load().settings
        if settings.mode_explicit:
            self._launch_mode = None
            return settings.mode
        self._launch_mode = HOSTED_MODE if hosted_token_saved() else DEFAULT_MODE
        return self._launch_mode

    def _for_this_run(self, settings: Settings) -> Settings:
        """The settings as this run sees them. A file with no explicit mode takes the launch mode, once one is resolved."""
        if settings.mode_explicit or self._launch_mode is None:
            return settings
        return replace(settings, mode=self._launch_mode)

    def load(self) -> LoadResult:
        # Fail closed for the tunnel switch (decision, X1). A missing file has no saved choice, so it keeps the default,
        # which is on (amendment F). A file that is there but cannot be used has a saved choice nobody can read, so the
        # tunnel switch is off until the owner fixes the file.
        no_file = replace(Settings(), mode_explicit=False)
        unusable = replace(no_file, tunnel_control_enabled=False)
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return LoadResult(self._for_this_run(no_file))
        except (OSError, ValueError) as exc:
            return LoadResult(
                self._for_this_run(unusable),
                f"settings file unreadable ({exc.__class__.__name__}); using defaults, with the tunnel controls off",
            )

        try:
            data = json.loads(raw)
        except ValueError:
            return LoadResult(
                self._for_this_run(unusable),
                "settings file is corrupt (not valid JSON); using defaults, with the tunnel controls off",
            )

        if not isinstance(data, dict):
            return LoadResult(
                self._for_this_run(unusable),
                "settings file does not hold a JSON object; using defaults, with the tunnel controls off",
            )

        settings, problems = _from_mapping(data)
        return LoadResult(self._for_this_run(settings), "; ".join(problems) or None)

    def save(self, settings: Settings) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        data = asdict(settings)
        del data["mode_explicit"]  # a flag about the file, not a setting
        if not settings.mode_explicit:
            # No mode the owner chose: the file keeps no mode key, so a later launch can still pick one.
            del data["mode"]
        if not settings.autostart:
            # Off is the default, so a file that never opted in is written exactly as before.
            del data["autostart"]
        for key in CORE_KEYS + CONFIRMED_KEYS:
            if not data[key]:
                # "Not set" is the default, so a file that never set a core path or a confirmed value has no such key.
                del data[key]
        for key in CORE_AUTO_KEYS:
            if not data[key]:
                # False is the default, so only a path the locator found carries its flag.
                del data[key]
        if not settings.key_names:
            # No saved names is the default, so a file with no API keys has no such key.
            del data["key_names"]
        if settings.check_updates:
            # On is the default, so a file that never turned the update check off has no such key.
            del data["check_updates"]
        if settings.auto_update:
            # On is the default, so a file that never turned automatic updates off has no such key.
            del data["auto_update"]
        for key in CONTROL_FLAG_KEYS:
            if data[key] == CONTROL_FLAG_DEFAULTS[key]:
                # The default is never written, so a file that keeps the defaults has no such key.
                del data[key]
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def keep_unreadable_copy(self) -> None:
        """Copy the settings file aside before a write that would replace it, when the file did not load cleanly.

        The copy is <name>.unreadable.bak beside the file. Nothing is copied for a file that loads cleanly or does not
        exist. Never raises: a failed copy must not stop the write the owner asked for.
        """
        if self.load().warning is None:
            return
        try:
            if self._path.is_file():
                shutil.copyfile(self._path, self._path.with_name(self._path.name + ".unreadable.bak"))
        except OSError:
            return

    def set_mode(self, mode: str) -> Settings:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        # The owner chose this mode, so it is explicit from now on and wins over the launch mode.
        updated = replace(self.load().settings, mode=mode, mode_explicit=True)
        self.save(updated)
        return updated

    def set_core_path(self, key: str, value: str) -> Settings:
        """Validate and save one core setting. Raises ValueError with plain words when it is refused.

        A refused value writes nothing. Every other field in the saved file is kept, except that a saved core path turns
        off both launch-time switches (zotero_autostart and local_server_autostart). The owner must confirm them again
        against the new setup. The caller says so in the Settings status line.
        """
        text = value.strip()
        problem = core_path_problem(key, text)
        if problem is not None:
            raise ValueError(problem)
        updated = replace(
            self.load().settings,
            **{key: text, CORE_AUTO_FLAGS[key]: False},
            zotero_autostart=False,
            local_server_autostart=False,
        )
        self.save(updated)
        return updated
