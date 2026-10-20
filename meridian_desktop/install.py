"""Per-user install for the desktop bundle: copy the onedir folder, add shortcuts and the
Apps and Features entry, and start at sign-in only when the user has opted in.

No administrator rights are needed. Everything lives under the user's own profile:

    install folder       %LOCALAPPDATA%\\Programs\\Meridian Desktop
    Start Menu           %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Meridian Desktop.lnk
    desktop (opt in)     %USERPROFILE%\\Desktop\\Meridian Desktop.lnk
    Apps and Features    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MeridianDesktop
    start at sign-in     HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\MeridianDesktop
                         (only while settings.json says "autostart": true)

Shortcuts are written by PowerShell's WScript.Shell object. Paths reach PowerShell through
environment variables and are never spliced into the command text, so a path with quotes
or spaces cannot change the command. The shortcut icon is the exe's own icon, which the
build embeds from packaging/assets/meridian-desktop.ico.

Registry values go through a RegistryBackend object. The default is the real HKCU on
Windows. Elsewhere, or when MERIDIAN_DESKTOP_NO_REGISTRY=1 is set, it writes nothing, so a
machine or a test that has not asked for registry writes never gets them.

Windows cannot delete a running exe. When uninstall runs from inside the install folder,
the folder removal is handed to a helper that starts after this process exits.

Pure Python, no Qt import. Run it from the bundle as ``MeridianDesktop.exe --install``, or
from source as ``python -m meridian_desktop.install --install``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from meridian_desktop import __version__
from meridian_desktop.settings_store import SettingsStore, default_settings_path

APP_NAME = "Meridian Desktop"
EXE_NAME = "MeridianDesktop.exe"
PUBLISHER = "Meridian"
SHORTCUT_NAME = f"{APP_NAME}.lnk"
REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / "dist" / "MeridianDesktop"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MeridianDesktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MeridianDesktop"
# Set to 1 to keep the installer away from the registry (locked-down machines, CI, tests).
NO_REGISTRY_ENV = "MERIDIAN_DESKTOP_NO_REGISTRY"

# The script reads its three values from the environment. It never has a path written into it.
_POWERSHELL_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MERIDIAN_LNK);"
    "$s.TargetPath = $env:MERIDIAN_TARGET;"
    "$s.WorkingDirectory = $env:MERIDIAN_WORKDIR;"
    "$s.IconLocation = $env:MERIDIAN_TARGET + ',0';"
    "$s.Description = 'Meridian Desktop';"
    "$s.Save()"
)

# Runs after this process exits. It retries for about fifteen seconds, because the exe may
# still be releasing its files. The folder path comes through an environment variable.
_REMOVE_SCRIPT = (
    "for ($i = 0; $i -lt 30; $i++) {"
    " if (-not (Test-Path -LiteralPath $env:MERIDIAN_REMOVE_DIR)) { break };"
    " Remove-Item -LiteralPath $env:MERIDIAN_REMOVE_DIR -Recurse -Force -ErrorAction SilentlyContinue;"
    " Start-Sleep -Milliseconds 500"
    "}"
)

ShortcutWriter = Callable[[Path, Path], None]
Scheduler = Callable[[Path], None]


class InstallError(RuntimeError):
    """An install step failed. The message is safe to show the user."""


@dataclass(frozen=True)
class InstallResult:
    install_dir: Path
    shortcuts: tuple[Path, ...]


class RegistryBackend(Protocol):
    """The registry operations the installer uses. Every key is under HKEY_CURRENT_USER."""

    def write_strings(self, key: str, values: Mapping[str, str]) -> None: ...

    def delete_value(self, key: str, name: str) -> bool: ...

    def delete_key(self, key: str) -> bool: ...


class NullRegistry:
    """Writes nothing and reports nothing removed. Used off Windows and when NO_REGISTRY_ENV is set."""

    def write_strings(self, key: str, values: Mapping[str, str]) -> None:
        return None

    def delete_value(self, key: str, name: str) -> bool:
        return False

    def delete_key(self, key: str) -> bool:
        return False


class WindowsRegistry:
    """HKEY_CURRENT_USER through winreg. The winreg module can be injected, so tests never reach the real hive."""

    def __init__(self, winreg_module: object | None = None) -> None:
        self._winreg = winreg_module if winreg_module is not None else _import_winreg()

    def write_strings(self, key: str, values: Mapping[str, str]) -> None:
        winreg = self._winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            for name, value in values.items():
                winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def delete_value(self, key: str, name: str) -> bool:
        winreg = self._winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, name)
        except FileNotFoundError:
            return False
        return True

    def delete_key(self, key: str) -> bool:
        winreg = self._winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            return False
        return True


def _import_winreg() -> object:
    import winreg  # Windows only; imported here so the module loads everywhere

    return winreg


def default_registry(env: Mapping[str, str]) -> RegistryBackend:
    """The real registry on Windows. A no-op elsewhere, or when NO_REGISTRY_ENV is "1"."""
    if env.get(NO_REGISTRY_ENV) == "1" or os.name != "nt":
        return NullRegistry()
    return WindowsRegistry()


def _base(env: Mapping[str, str], key: str, fallback: tuple[str, ...]) -> Path:
    value = env.get(key)
    if value:
        return Path(value)
    home = Path(env.get("USERPROFILE") or Path.home())
    return home.joinpath(*fallback)


def install_dir(env: Mapping[str, str]) -> Path:
    return _base(env, "LOCALAPPDATA", ("AppData", "Local")) / "Programs" / APP_NAME


def start_menu_shortcut(env: Mapping[str, str]) -> Path:
    programs = _base(env, "APPDATA", ("AppData", "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return programs / SHORTCUT_NAME


def desktop_shortcut(env: Mapping[str, str]) -> Path:
    return _base(env, "USERPROFILE", ()) / "Desktop" / SHORTCUT_NAME


def create_shortcut(shortcut: Path, target: Path) -> None:
    """Write a .lnk that starts ``target``, using the exe's embedded icon. Windows only."""
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        MERIDIAN_LNK=str(shortcut),
        MERIDIAN_TARGET=str(target),
        MERIDIAN_WORKDIR=str(target.parent),
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _POWERSHELL_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not shortcut.is_file():
        raise InstallError(f"could not write the shortcut {shortcut.name} (PowerShell exit {completed.returncode})")


def schedule_folder_removal(folder: Path) -> None:
    """Remove ``folder`` after this process exits, from a hidden PowerShell that retries. Windows only."""
    env = dict(os.environ, MERIDIAN_REMOVE_DIR=str(folder))
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _REMOVE_SCRIPT,
        ],
        env=env,
        creationflags=flags,
        close_fds=True,
    )


def apps_entry(exe: Path) -> dict[str, str]:
    """The values under the per-user Uninstall key that Apps and Features reads."""
    return {
        "DisplayName": APP_NAME,
        "DisplayVersion": __version__,
        "Publisher": PUBLISHER,
        "DisplayIcon": f"{exe},0",
        "UninstallString": f'"{exe}" --uninstall',
    }


def autostart_target(env: Mapping[str, str]) -> Path | None:
    """The exe a Run entry starts: the installed copy if there is one, else this frozen exe, else None."""
    installed = install_dir(env) / EXE_NAME
    if installed.is_file():
        return installed
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


def _autostart_saved(env: Mapping[str, str]) -> bool:
    """The saved start-at-sign-in choice. A missing or unreadable settings file means off."""
    return SettingsStore(default_settings_path(env)).load().settings.autostart


def set_autostart(
    enabled: bool,
    env: Mapping[str, str],
    *,
    registry: RegistryBackend | None = None,
    target: Path | None = None,
) -> Path | None:
    """Write the per-user Run entry when ``enabled``, or remove it. Returns the exe the entry starts, or None.

    Raises InstallError, with the next step, when it is asked to enable with no exe to start.
    """
    backend = registry if registry is not None else default_registry(env)
    if not enabled:
        backend.delete_value(RUN_KEY, RUN_VALUE)
        return None
    exe = target if target is not None else autostart_target(env)
    if exe is None:
        raise InstallError(
            "Start with Windows is saved, but Windows cannot start Meridian Desktop until it is installed. "
            "Run install.cmd from the release folder and it will be applied."
        )
    backend.write_strings(RUN_KEY, {RUN_VALUE: f'"{exe}"'})
    return exe


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def install(
    source: Path,
    env: Mapping[str, str],
    *,
    desktop: bool = False,
    writer: ShortcutWriter = create_shortcut,
    registry: RegistryBackend | None = None,
) -> InstallResult:
    """Copy the bundle in ``source`` into the install folder, then add the shortcuts and the Apps and Features entry.

    The start-at-sign-in entry follows the saved choice: written while settings say on, removed while off.
    """
    source = Path(source)
    if not (source / EXE_NAME).is_file():
        raise InstallError(f"{source} has no {EXE_NAME}; build the bundle first with pixi run build")
    target = install_dir(env)
    if source.resolve() != target.resolve():
        _replace_tree(source, target)
    exe = target / EXE_NAME
    links = [start_menu_shortcut(env)]
    if desktop:
        links.append(desktop_shortcut(env))
    for link in links:
        writer(link, exe)
    backend = registry if registry is not None else default_registry(env)
    try:
        backend.write_strings(UNINSTALL_KEY, apps_entry(exe))
        set_autostart(_autostart_saved(env), env, registry=backend, target=exe)
    except OSError as exc:
        raise InstallError(f"could not update the Windows registry for Meridian Desktop ({exc.strerror})") from exc
    return InstallResult(install_dir=target, shortcuts=tuple(links))


def _running_from(folder: Path) -> bool:
    """True when this process is the frozen exe inside ``folder``, which Windows will not let us delete."""
    if not getattr(sys, "frozen", False):
        return False
    try:
        Path(sys.executable).resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def _remove_folder(folder: Path, scheduler: Scheduler | None) -> str:
    """Remove the install folder and return the line to report.

    The folder is renamed aside first. If a file inside is in use, the rename fails and nothing
    is deleted, so a copy that is still running is never left with half its files gone. That
    failure is an error, except when this process is the exe inside the folder: then the removal
    goes to ``scheduler`` and runs after this process exits.
    """
    parked = folder.with_name(f"{folder.name}.removing-{uuid.uuid4().hex[:8]}")
    try:
        folder.rename(parked)
    except OSError as exc:
        if scheduler is not None and _running_from(folder):
            scheduler(folder)
            return f"scheduled removal of {folder} once Meridian Desktop closes"
        raise InstallError(f"could not remove {folder}; close Meridian Desktop first ({exc.strerror})") from exc
    try:
        shutil.rmtree(parked)
    except OSError as exc:
        if scheduler is None:
            raise InstallError(f"could not finish removing {folder} ({exc.strerror})") from exc
        scheduler(parked)
        return f"scheduled removal of {parked} once Meridian Desktop closes"
    return f"removed {folder}"


def uninstall(
    env: Mapping[str, str],
    *,
    registry: RegistryBackend | None = None,
    scheduler: Scheduler | None = None,
) -> list[str]:
    """Remove the start-at-sign-in entry, both shortcuts, the install folder and the Apps and Features entry.

    Returns one line per step done. The Apps and Features entry goes last, after the folder is gone or its
    removal is scheduled, so a failed uninstall still shows up in Apps and Features and can be run again.
    """
    backend = registry if registry is not None else default_registry(env)
    done: list[str] = []
    try:
        if backend.delete_value(RUN_KEY, RUN_VALUE):
            done.append(f"removed start-with-Windows entry HKCU\\{RUN_KEY}\\{RUN_VALUE}")
        for link in (start_menu_shortcut(env), desktop_shortcut(env)):
            if link.is_file():
                link.unlink()
                done.append(f"removed {link}")
        folder = install_dir(env)
        if folder.exists():
            done.append(_remove_folder(folder, scheduler))
        if backend.delete_key(UNINSTALL_KEY):
            done.append(f"removed Apps and Features entry HKCU\\{UNINSTALL_KEY}")
    except OSError as exc:
        raise InstallError(f"could not update the Windows registry for Meridian Desktop ({exc.strerror})") from exc
    return done


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="MeridianDesktop", description="Install or remove Meridian Desktop for this user.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true", help="copy the bundle and add the shortcuts")
    action.add_argument("--uninstall", action="store_true", help="remove the install folder and shortcuts")
    parser.add_argument("--desktop", action="store_true", help="also add a desktop shortcut (install only)")
    parser.add_argument("--source", type=Path, default=None, help="bundle folder to copy (install only)")
    return parser


def _default_source() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return DEFAULT_SOURCE


def _say(text: str, *, error: bool = False) -> None:
    """Print to the console. The windowed bundle has no console, so an error goes to a message box instead."""
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(text, file=stream)
    elif error:
        _message_box(text)


def _message_box(text: str) -> None:
    if os.name != "nt":
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)  # MB_ICONERROR


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    writer: ShortcutWriter = create_shortcut,
    registry: RegistryBackend | None = None,
    scheduler: Scheduler | None = schedule_folder_removal,
) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if env is None else env
    try:
        if args.install:
            source = args.source or _default_source()
            result = install(source, environment, desktop=args.desktop, writer=writer, registry=registry)
            _say(f"installed to {result.install_dir}")
            for link in result.shortcuts:
                _say(f"shortcut: {link}")
            return 0
        lines = uninstall(environment, registry=registry, scheduler=scheduler)
        if not lines:
            _say("nothing to remove: Meridian Desktop is not installed for this user")
        for line in lines:
            _say(line)
        return 0
    except InstallError as exc:
        _say(f"install failed: {exc}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
