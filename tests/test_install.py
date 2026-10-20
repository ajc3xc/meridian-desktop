"""Tests for the per-user install (meridian_desktop/install.py) and the icon wiring in packaging/build.py.

Every call passes an explicit environment that points at pytest's temp dir, so no test
can reach the real Start Menu, desktop or install folder. The conftest isolates only
LOCALAPPDATA and USERPROFILE, so these tests never rely on it for APPDATA.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meridian_desktop import install as inst

REPO = Path(__file__).resolve().parents[1]


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "profile"),
    }


def _bundle(tmp_path: Path, *, marker: str = "v1") -> Path:
    source = tmp_path / "bundle"
    (source / "_internal").mkdir(parents=True)
    (source / inst.EXE_NAME).write_bytes(b"MZ-fake")
    (source / "_internal" / "data.txt").write_text(marker, encoding="utf-8")
    return source


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, shortcut: Path, target: Path) -> None:
        self.calls.append((shortcut, target))


def test_paths_come_from_the_given_environment(tmp_path):
    env = _env(tmp_path)
    assert inst.install_dir(env) == tmp_path / "local" / "Programs" / "Meridian Desktop"
    assert inst.start_menu_shortcut(env) == (
        tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Meridian Desktop.lnk"
    )
    assert inst.desktop_shortcut(env) == tmp_path / "profile" / "Desktop" / "Meridian Desktop.lnk"


def test_paths_fall_back_to_the_home_folder_when_variables_are_unset(tmp_path):
    env = {"USERPROFILE": str(tmp_path)}
    assert inst.install_dir(env) == tmp_path / "AppData" / "Local" / "Programs" / "Meridian Desktop"
    assert inst.start_menu_shortcut(env) == (
        tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Meridian Desktop.lnk"
    )


def test_install_copies_the_bundle_and_writes_one_shortcut(tmp_path):
    env = _env(tmp_path)
    recorder = _Recorder()
    result = inst.install(_bundle(tmp_path), env, writer=recorder)

    target = inst.install_dir(env)
    assert (target / inst.EXE_NAME).read_bytes() == b"MZ-fake"
    assert (target / "_internal" / "data.txt").read_text(encoding="utf-8") == "v1"
    assert result.install_dir == target
    assert result.shortcuts == (inst.start_menu_shortcut(env),)
    assert recorder.calls == [(inst.start_menu_shortcut(env), target / inst.EXE_NAME)]


def test_desktop_flag_adds_a_second_shortcut(tmp_path):
    env = _env(tmp_path)
    recorder = _Recorder()
    result = inst.install(_bundle(tmp_path), env, desktop=True, writer=recorder)

    assert result.shortcuts == (inst.start_menu_shortcut(env), inst.desktop_shortcut(env))
    assert [call[0] for call in recorder.calls] == [inst.start_menu_shortcut(env), inst.desktop_shortcut(env)]


def test_install_over_an_older_copy_replaces_it(tmp_path):
    env = _env(tmp_path)
    inst.install(_bundle(tmp_path, marker="v1"), env, writer=_Recorder())
    stale = inst.install_dir(env) / "old_only.txt"
    stale.write_text("left over", encoding="utf-8")

    inst.install(_bundle(tmp_path / "second", marker="v2"), env, writer=_Recorder())

    assert (inst.install_dir(env) / "_internal" / "data.txt").read_text(encoding="utf-8") == "v2"
    assert not stale.exists()


def test_install_refuses_a_source_without_the_exe(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(inst.InstallError, match="build the bundle first"):
        inst.install(empty, _env(tmp_path), writer=_Recorder())
    assert not inst.install_dir(_env(tmp_path)).exists()


def test_install_from_the_installed_folder_keeps_it(tmp_path):
    env = _env(tmp_path)
    inst.install(_bundle(tmp_path), env, writer=_Recorder())
    installed = inst.install_dir(env)

    inst.install(installed, env, writer=_Recorder())

    assert (installed / inst.EXE_NAME).is_file()


def test_uninstall_removes_the_folder_and_both_shortcuts(tmp_path):
    env = _env(tmp_path)
    inst.install(_bundle(tmp_path), env, desktop=True, writer=_Recorder())
    # The writer is a stub, so create the shortcut files the real writer would have made.
    inst.start_menu_shortcut(env).parent.mkdir(parents=True, exist_ok=True)
    inst.start_menu_shortcut(env).write_bytes(b"lnk")
    inst.desktop_shortcut(env).parent.mkdir(parents=True, exist_ok=True)
    inst.desktop_shortcut(env).write_bytes(b"lnk")

    removed = inst.uninstall(env)

    assert set(removed) == {inst.install_dir(env), inst.start_menu_shortcut(env), inst.desktop_shortcut(env)}
    assert not inst.install_dir(env).exists()
    assert not inst.start_menu_shortcut(env).exists()
    assert not inst.desktop_shortcut(env).exists()


def test_uninstall_when_nothing_is_installed_removes_nothing(tmp_path):
    assert inst.uninstall(_env(tmp_path)) == []


def test_create_shortcut_passes_paths_through_the_environment_not_the_command(tmp_path, monkeypatch):
    shortcut = tmp_path / "Start Menu" / "Meridian Desktop.lnk"
    target = tmp_path / "Programs" / "x'y \"z\"" / inst.EXE_NAME
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"lnk")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    inst.create_shortcut(shortcut, target)

    assert str(target) not in " ".join(seen["argv"])
    assert seen["env"]["MERIDIAN_TARGET"] == str(target)
    assert seen["env"]["MERIDIAN_LNK"] == str(shortcut)
    assert seen["env"]["MERIDIAN_WORKDIR"] == str(target.parent)


def test_create_shortcut_reports_a_powershell_failure(tmp_path, monkeypatch):
    def failing_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    monkeypatch.setattr(inst.subprocess, "run", failing_run)
    with pytest.raises(inst.InstallError, match="could not write the shortcut"):
        inst.create_shortcut(tmp_path / "x.lnk", tmp_path / inst.EXE_NAME)


def test_main_install_succeeds_with_an_injected_environment(tmp_path, capsys):
    env = _env(tmp_path)
    recorder = _Recorder()
    code = inst.main(["--install", "--source", str(_bundle(tmp_path))], env=env, writer=recorder)

    assert code == 0
    assert "installed to" in capsys.readouterr().out
    assert len(recorder.calls) == 1


def test_main_install_reports_a_missing_bundle_and_returns_1(tmp_path, capsys):
    code = inst.main(["--install", "--source", str(tmp_path / "nowhere")], env=_env(tmp_path), writer=_Recorder())

    assert code == 1
    assert "install failed" in capsys.readouterr().err


def test_main_uninstall_with_nothing_installed_returns_0(tmp_path, capsys):
    assert inst.main(["--uninstall"], env=_env(tmp_path)) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_main_requires_exactly_one_action(tmp_path):
    with pytest.raises(SystemExit):
        inst.main([], env=_env(tmp_path))


def test_entry_point_handles_uninstall_without_starting_the_ui(tmp_path):
    """python -m meridian_desktop --uninstall must return before any Qt import or window."""
    env = dict(os.environ, **_env(tmp_path))
    completed = subprocess.run(
        [sys.executable, "-m", "meridian_desktop", "--uninstall"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "nothing to remove" in completed.stdout


def _load_build():
    spec = importlib.util.spec_from_file_location("build_for_install_tests", REPO / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_build_embeds_the_icon():
    build = _load_build()
    cmd = build.pyinstaller_command([])
    assert "--icon" in cmd
    icon_arg = cmd[cmd.index("--icon") + 1]
    assert Path(icon_arg) == build.ICON
    assert build.ICON.is_file(), "packaging/assets/meridian-desktop.ico is missing; run make_icon.ps1"


def test_icon_file_is_a_real_ico():
    header = (REPO / "packaging" / "assets" / "meridian-desktop.ico").read_bytes()[:6]
    # ICONDIR: reserved 0, type 1 (icon), at least one image.
    assert header[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(header[4:6], "little") >= 1
