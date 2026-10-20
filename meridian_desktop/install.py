"""Per-user install for the desktop bundle: copy the onedir folder, add shortcuts.

No administrator rights are needed. Everything lives under the user's own profile:

    install folder    %LOCALAPPDATA%\\Programs\\Meridian Desktop
    Start Menu        %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Meridian Desktop.lnk
    desktop (opt in)  %USERPROFILE%\\Desktop\\Meridian Desktop.lnk

Shortcuts are written by PowerShell's WScript.Shell object. Paths reach PowerShell through
environment variables and are never spliced into the command text, so a path with quotes
or spaces cannot change the command. The shortcut icon is the exe's own icon, which the
build embeds from packaging/assets/meridian-desktop.ico.

Pure Python, no Qt import. Run it from the bundle as ``MeridianDesktop.exe --install``, or
from source as ``python -m meridian_desktop.install --install``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Meridian Desktop"
EXE_NAME = "MeridianDesktop.exe"
SHORTCUT_NAME = f"{APP_NAME}.lnk"
REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / "dist" / "MeridianDesktop"

# The script reads its three values from the environment. It never has a path written into it.
_POWERSHELL_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MERIDIAN_LNK);"
    "$s.TargetPath = $env:MERIDIAN_TARGET;"
    "$s.WorkingDirectory = $env:MERIDIAN_WORKDIR;"
    "$s.IconLocation = $env:MERIDIAN_TARGET + ',0';"
    "$s.Description = 'Meridian Desktop';"
    "$s.Save()"
)

ShortcutWriter = Callable[[Path, Path], None]


class InstallError(RuntimeError):
    """An install step failed. The message is safe to show the user."""


@dataclass(frozen=True)
class InstallResult:
    install_dir: Path
    shortcuts: tuple[Path, ...]


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
) -> InstallResult:
    """Copy the bundle in ``source`` into the install folder and add the shortcuts."""
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
    return InstallResult(install_dir=target, shortcuts=tuple(links))


def uninstall(env: Mapping[str, str]) -> list[Path]:
    """Remove the install folder and the two shortcuts. Returns what was removed."""
    removed: list[Path] = []
    for link in (start_menu_shortcut(env), desktop_shortcut(env)):
        if link.is_file():
            link.unlink()
            removed.append(link)
    folder = install_dir(env)
    if folder.exists():
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            raise InstallError(f"could not remove {folder}; close Meridian Desktop first ({exc.strerror})") from exc
        removed.append(folder)
    return removed


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


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    writer: ShortcutWriter = create_shortcut,
) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if env is None else env
    try:
        if args.install:
            source = args.source or _default_source()
            result = install(source, environment, desktop=args.desktop, writer=writer)
            print(f"installed to {result.install_dir}")
            for link in result.shortcuts:
                print(f"shortcut: {link}")
            return 0
        removed = uninstall(environment)
        if not removed:
            print("nothing to remove: Meridian Desktop is not installed for this user")
        for path in removed:
            print(f"removed: {path}")
        return 0
    except InstallError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
