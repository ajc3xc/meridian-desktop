"""Per-user install for the desktop bundle: copy the onedir folder, add shortcuts and the
Apps and Features entry, and start at sign-in only when the user has opted in.

No administrator rights are needed. Everything lives under the user's own profile:

    install folder       %LOCALAPPDATA%\\Programs\\Meridian Desktop
    versions             <install folder>\\versions\\<version>\\MeridianDesktop.exe, one folder per installed version.
                         The shortcuts point at the one that current.json names. A flat install (the exe directly
                         in the install folder, from before versions existed) is moved into versions\\ on the next
                         install.
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

Dev mode is ``--install --dev``, for a source checkout. Its shortcuts run
``pythonw -m meridian_desktop`` from the repository root, with the repository's icon, so a code
change shows the next time the app starts. Dev mode copies nothing, and it does not touch the
installed bundle, the Apps and Features entry or the start-at-sign-in entry. Uninstall removes
the shortcuts it wrote and never deletes the source tree.
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

from meridian_desktop import __version__, updater
from meridian_desktop.settings_store import SettingsStore, default_settings_path

APP_NAME = "Meridian Desktop"
EXE_NAME = "MeridianDesktop.exe"
VERSIONS_DIR = updater.VERSIONS_DIR  # install folder\versions\<version>, one folder per installed version
PUBLISHER = "Meridian"
SHORTCUT_NAME = f"{APP_NAME}.lnk"
# The dev shortcut has its own name, so it never replaces the launch version's shortcut.
DEV_SHORTCUT_NAME = f"{APP_NAME} (dev).lnk"
REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / "dist" / "MeridianDesktop"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MeridianDesktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MeridianDesktop"
# Set to 1 to keep the installer away from the registry (locked-down machines, CI, tests).
NO_REGISTRY_ENV = "MERIDIAN_DESKTOP_NO_REGISTRY"
# A dev shortcut runs this module's package from the source tree, with the tree's icon.
DEV_ARGUMENTS = "-m meridian_desktop"
ICON_RELATIVE = Path("packaging") / "assets" / "meridian-desktop.ico"

# The script reads its three values from the environment. It never has a path written into it.
_POWERSHELL_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MERIDIAN_LNK);"
    "$s.TargetPath = $env:MERIDIAN_TARGET;"
    "$s.WorkingDirectory = $env:MERIDIAN_WORKDIR;"
    "$s.IconLocation = $env:MERIDIAN_TARGET + ',0';"
    "$s.Description = 'Meridian Desktop';"
    "$s.Save()"
)

# A dev shortcut also sets its arguments, working folder and icon. Still only env vars, never text.
_DEV_POWERSHELL_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MERIDIAN_LNK);"
    "$s.TargetPath = $env:MERIDIAN_TARGET;"
    "$s.Arguments = $env:MERIDIAN_ARGS;"
    "$s.WorkingDirectory = $env:MERIDIAN_WORKDIR;"
    "$s.IconLocation = $env:MERIDIAN_ICON + ',0';"
    "$s.Description = 'Meridian Desktop (dev: runs from source)';"
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


@dataclass(frozen=True)
class DevShortcut:
    """One dev shortcut to write: the .lnk, and what it starts, with which arguments, folder and icon."""

    shortcut: Path
    target: Path
    arguments: str
    working_dir: Path
    icon: Path


@dataclass(frozen=True)
class DevInstallResult:
    interpreter: Path
    repo: Path
    shortcuts: tuple[Path, ...]


DevShortcutWriter = Callable[[DevShortcut], None]


class RegistryBackend(Protocol):
    """The registry operations the installer uses. Every key is under HKEY_CURRENT_USER."""

    def write_strings(self, key: str, values: Mapping[str, str]) -> None: ...

    def read_string(self, key: str, name: str) -> str | None: ...

    def delete_value(self, key: str, name: str) -> bool: ...

    def delete_key(self, key: str) -> bool: ...


class NullRegistry:
    """Writes nothing and reports nothing removed. Used off Windows and when NO_REGISTRY_ENV is set."""

    def write_strings(self, key: str, values: Mapping[str, str]) -> None:
        return None

    def read_string(self, key: str, name: str) -> str | None:
        return None

    def delete_value(self, key: str, name: str) -> bool:
        return False

    def delete_key(self, key: str) -> bool:
        return False


_KEY_READ = 0x20019  # the winreg access right for reading a key, the same value on every Windows


class WindowsRegistry:
    """HKEY_CURRENT_USER through winreg. The winreg module can be injected, so tests never reach the real hive."""

    def __init__(self, winreg_module: object | None = None) -> None:
        self._winreg = winreg_module if winreg_module is not None else _import_winreg()

    def write_strings(self, key: str, values: Mapping[str, str]) -> None:
        winreg = self._winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            for name, value in values.items():
                winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def read_string(self, key: str, name: str) -> str | None:
        """A string value, or None when the key or the value is missing."""
        winreg = self._winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, _KEY_READ) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)
        except OSError:
            return None
        return value if isinstance(value, str) else None

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


def start_menu_shortcut(env: Mapping[str, str], *, dev: bool = False) -> Path:
    programs = _base(env, "APPDATA", ("AppData", "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return programs / (DEV_SHORTCUT_NAME if dev else SHORTCUT_NAME)


def desktop_shortcut(env: Mapping[str, str], *, dev: bool = False) -> Path:
    return _base(env, "USERPROFILE", ()) / "Desktop" / (DEV_SHORTCUT_NAME if dev else SHORTCUT_NAME)


def _write_lnk(shortcut: Path, script: str, values: Mapping[str, str]) -> None:
    """Run ``script`` in PowerShell to write ``shortcut``. ``values`` reach it only as environment variables."""
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, MERIDIAN_LNK=str(shortcut), **values)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not shortcut.is_file():
        raise InstallError(f"could not write the shortcut {shortcut.name} (PowerShell exit {completed.returncode})")


def create_shortcut(shortcut: Path, target: Path) -> None:
    """Write a .lnk that starts ``target``, using the exe's embedded icon. Windows only."""
    _write_lnk(
        shortcut,
        _POWERSHELL_SCRIPT,
        {"MERIDIAN_TARGET": str(target), "MERIDIAN_WORKDIR": str(target.parent)},
    )


def create_dev_shortcut(spec: DevShortcut) -> None:
    """Write a .lnk that runs ``spec.target`` with its arguments, working folder and icon. Windows only."""
    _write_lnk(
        spec.shortcut,
        _DEV_POWERSHELL_SCRIPT,
        {
            "MERIDIAN_TARGET": str(spec.target),
            "MERIDIAN_ARGS": spec.arguments,
            "MERIDIAN_WORKDIR": str(spec.working_dir),
            "MERIDIAN_ICON": str(spec.icon),
        },
    )


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
    """The exe a Run entry starts: the installed version that current.json names, else this frozen exe, else None."""
    root = install_dir(env)
    current = updater.read_current(root)
    if current is not None:
        installed = updater.exe_for(root, current)
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


# A flat install that records no plain version in Apps and Features still gets a folder, named so that rollback can
# find it. 0.0.0 is the lowest plain version, so every real release is newer than it.
UNKNOWN_FLAT_VERSION = "0.0.0"
# The pointer files and the versions folder stay in the install folder when a flat install is moved.
_LAYOUT_NAMES = frozenset({updater.CURRENT_NAME, updater.PENDING_NAME, updater.VERSIONS_DIR})


def _flat_version(backend: RegistryBackend) -> str:
    """The version a flat install reports in Apps and Features, or UNKNOWN_FLAT_VERSION when it reports none."""
    recorded = backend.read_string(UNINSTALL_KEY, "DisplayVersion")
    try:
        updater.parse_version(recorded)
    except updater.Refused:
        return UNKNOWN_FLAT_VERSION
    return str(recorded)


def _migrate_flat_install(root: Path, backend: RegistryBackend) -> Path | None:
    """Move a flat install (MeridianDesktop.exe directly in root) into versions\\<its version>. Return that folder.

    Returns None when there is no flat install. Everything in root moves except the pointer files and versions\\.
    """
    if not (root / EXE_NAME).is_file():
        return None
    dest = root / VERSIONS_DIR / _flat_version(backend)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for entry in list(root.iterdir()):
        if entry.name not in _LAYOUT_NAMES:
            shutil.move(str(entry), str(dest / entry.name))
    return dest


def _publish_copy(source: Path, target: Path) -> None:
    """Copy the bundle to target through target.partial, so a failed copy never leaves a half-written version."""
    partial = target.with_name(f"{target.name}.partial")
    if partial.exists():
        shutil.rmtree(partial)
    shutil.copytree(source, partial)
    if target.exists():
        shutil.rmtree(target)
    partial.rename(target)


def _prune_versions(root: Path, keep: set[str]) -> None:
    """Delete every version folder and leftover partial copy under root\\versions that keep does not name."""
    versions = root / VERSIONS_DIR
    if not versions.is_dir():
        return
    for entry in versions.iterdir():
        if entry.is_dir() and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)


def install(
    source: Path,
    env: Mapping[str, str],
    *,
    desktop: bool = False,
    writer: ShortcutWriter = create_shortcut,
    registry: RegistryBackend | None = None,
) -> InstallResult:
    """Copy the bundle in ``source`` into versions\\<version>, then point the shortcuts and the Apps and Features entry at it.

    The version is this build's. A flat install in the install folder is moved into versions\\ first. The version that
    was current stays with the new one, so rollback has a version to return to. Older versions are removed. The
    start-at-sign-in entry follows the saved choice: written while settings say on, removed while off.
    """
    source = Path(source)
    if not (source / EXE_NAME).is_file():
        raise InstallError(f"{source} has no {EXE_NAME}; build the bundle first with pixi run build")
    root = install_dir(env)
    backend = registry if registry is not None else default_registry(env)
    version = __version__
    target = root / VERSIONS_DIR / version
    try:
        previous = updater.read_current(root)
        migrated = _migrate_flat_install(root, backend)
        if migrated is not None and source.resolve() == root.resolve():
            source = migrated  # the installed copy was the source, and it has just moved into versions\
        if source.resolve() != target.resolve():
            _publish_copy(source, target)
        updater.write_current(root, version)
    except OSError as exc:
        raise InstallError(f"could not copy Meridian Desktop into {root} ({exc.strerror})") from exc
    exe = target / EXE_NAME
    links = [start_menu_shortcut(env)]
    if desktop or desktop_shortcut(env).is_file():
        # An existing desktop shortcut is repointed too, so it never keeps a version that is about to be removed.
        links.append(desktop_shortcut(env))
    for link in links:
        writer(link, exe)
    try:
        backend.write_strings(UNINSTALL_KEY, apps_entry(exe))
        set_autostart(_autostart_saved(env), env, registry=backend, target=exe)
    except OSError as exc:
        raise InstallError(f"could not update the Windows registry for Meridian Desktop ({exc.strerror})") from exc
    keep = {version, previous, migrated.name if migrated is not None else None}
    _prune_versions(root, {name for name in keep if name})
    return InstallResult(install_dir=root, shortcuts=tuple(links))


def update_shortcut_writers(env: Mapping[str, str]) -> list[updater.ShortcutWriter]:
    """One writer for each Meridian Desktop shortcut that already exists, and for the start-at-sign-in entry if it is on.

    An update repoints only what this user already has. A shortcut that was never installed is never created.
    """
    writers: list[updater.ShortcutWriter] = []
    for link in (start_menu_shortcut(env), desktop_shortcut(env)):
        if link.is_file():
            writers.append(_link_writer(link))
    if _autostart_saved(env):
        writers.append(lambda exe: set_autostart(True, env, target=exe))
    return writers


def _link_writer(link: Path) -> updater.ShortcutWriter:
    def write(exe: Path) -> None:
        create_shortcut(link, exe)

    return write


def dev_interpreter(interpreter: Path) -> Path:
    """The program a dev shortcut starts: pythonw.exe beside ``interpreter``, else python.exe beside it.

    pythonw.exe is the windowed build, so the app opens without a console. Raises InstallError when
    neither is there, which is the case when ``interpreter`` is the frozen exe.
    """
    for name in ("pythonw.exe", "python.exe"):
        candidate = interpreter.with_name(name)
        if candidate.is_file():
            return candidate
    raise InstallError(f"no pythonw.exe or python.exe beside {interpreter}; run --dev from a Python install")


def install_dev(
    env: Mapping[str, str],
    *,
    desktop: bool = False,
    writer: DevShortcutWriter = create_dev_shortcut,
    interpreter: Path | None = None,
    repo: Path | None = None,
) -> DevInstallResult:
    """Point the Start Menu shortcut (and the desktop one, with ``desktop``) at the source tree.

    The shortcut runs ``pythonw -m meridian_desktop`` from the repository root, so the next start shows the
    code as it is now. ``interpreter`` defaults to the Python running this install, and ``repo`` to this
    checkout. Nothing is copied, and the installed bundle, the Apps and Features entry and the
    start-at-sign-in entry are left as they are.
    """
    program = dev_interpreter(Path(sys.executable) if interpreter is None else Path(interpreter))
    tree = REPO if repo is None else Path(repo)
    if not (tree / "meridian_desktop" / "__main__.py").is_file():
        raise InstallError(f"{tree} is not a Meridian Desktop source tree; run --dev from the repository")
    links = [start_menu_shortcut(env, dev=True)]
    if desktop:
        links.append(desktop_shortcut(env, dev=True))
    for link in links:
        writer(
            DevShortcut(
                shortcut=link,
                target=program,
                arguments=DEV_ARGUMENTS,
                working_dir=tree,
                icon=tree / ICON_RELATIVE,
            )
        )
    return DevInstallResult(interpreter=program, repo=tree, shortcuts=tuple(links))


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
        for link in (
            start_menu_shortcut(env),
            desktop_shortcut(env),
            start_menu_shortcut(env, dev=True),
            desktop_shortcut(env, dev=True),
        ):
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
    parser.add_argument(
        "--dev",
        action="store_true",
        help="run the shortcuts from this source tree, not the frozen copy (install only)",
    )
    parser.add_argument("--source", type=Path, default=None, help="bundle folder to copy (install without --dev only)")
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
    dev_writer: DevShortcutWriter = create_dev_shortcut,
    registry: RegistryBackend | None = None,
    scheduler: Scheduler | None = schedule_folder_removal,
    interpreter: Path | None = None,
    repo: Path | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.dev and not args.install:
        parser.error("--dev goes with --install")
    if args.dev and args.source is not None:
        parser.error("--source copies the frozen bundle, so it cannot be used with --dev")
    environment = os.environ if env is None else env
    try:
        if args.install and args.dev:
            dev = install_dev(environment, desktop=args.desktop, writer=dev_writer, interpreter=interpreter, repo=repo)
            _say(f"installed (mode: dev): shortcuts run {dev.interpreter} {DEV_ARGUMENTS} from {dev.repo}")
            for link in dev.shortcuts:
                _say(f"shortcut: {link}")
            return 0
        if args.install:
            source = args.source or _default_source()
            result = install(source, environment, desktop=args.desktop, writer=writer, registry=registry)
            _say(f"installed to {result.install_dir} (mode: bundle)")
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
