"""Tests for the versioned install layout in meridian_desktop/install.py: the flat-install migration, the version kept for
rollback, pruning, the current pointer, and the shortcut writers an update uses.

Every path comes from an explicit environment under pytest's temp dir. The registry is a fake that starts empty and holds
the values a test sets, so no test reaches the real hive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian_desktop import __version__
from meridian_desktop import install as inst
from meridian_desktop import updater
from meridian_desktop.settings_store import Settings, SettingsStore, default_settings_path


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "profile"),
        inst.NO_REGISTRY_ENV: "1",
    }


def _bundle(tmp_path: Path, *, marker: str = "new") -> Path:
    source = tmp_path / "bundle"
    (source / "_internal").mkdir(parents=True)
    (source / inst.EXE_NAME).write_bytes(b"MZ-" + marker.encode())
    (source / "_internal" / "data.txt").write_text(marker, encoding="utf-8")
    return source


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, shortcut: Path, target: Path) -> None:
        self.calls.append((shortcut, target))


class FakeRegistry:
    """In-memory stand-in for HKEY_CURRENT_USER. Values are read back as the installer reads them."""

    def __init__(self, values: dict[str, dict[str, str]] | None = None) -> None:
        self.keys: dict[str, dict[str, str]] = {key: dict(vals) for key, vals in (values or {}).items()}

    def write_strings(self, key: str, values: dict[str, str]) -> None:
        self.keys.setdefault(key, {}).update(values)

    def read_string(self, key: str, name: str) -> str | None:
        return self.keys.get(key, {}).get(name)

    def delete_value(self, key: str, name: str) -> bool:
        values = self.keys.get(key, {})
        if name not in values:
            return False
        del values[name]
        return True

    def delete_key(self, key: str) -> bool:
        return self.keys.pop(key, None) is not None


def _apps(version: str) -> dict[str, dict[str, str]]:
    """The Apps and Features values a flat install from `version` left behind."""
    return {inst.UNINSTALL_KEY: {"DisplayVersion": version}}


def _flat_install(root: Path, marker: str) -> None:
    """A flat install: the exe and _internal sit directly in the install folder, as before versions existed."""
    (root / "_internal").mkdir(parents=True)
    (root / inst.EXE_NAME).write_bytes(b"MZ-" + marker.encode())
    (root / "_internal" / "data.txt").write_text(marker, encoding="utf-8")


def _version_exe(root: Path, version: str) -> Path:
    return root / "versions" / version / inst.EXE_NAME


def test_a_flat_install_moves_into_versions_under_the_version_it_recorded(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    recorder = _Recorder()

    inst.install(_bundle(tmp_path), env, writer=recorder, registry=FakeRegistry(_apps("0.1.0")))

    assert _version_exe(root, "0.1.0").read_bytes() == b"MZ-old"
    assert (root / "versions" / "0.1.0" / "_internal" / "data.txt").read_text(encoding="utf-8") == "old"
    assert not (root / inst.EXE_NAME).exists()
    assert not (root / "_internal").exists()
    assert _version_exe(root, __version__).read_bytes() == b"MZ-new"
    assert updater.read_current(root) == __version__
    assert recorder.calls == [(inst.start_menu_shortcut(env), _version_exe(root, __version__))]


def test_the_previous_flat_version_stays_for_rollback(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))

    assert updater.exe_for(root, "0.1.0").is_file()
    assert updater.rollback(__version__, root, []) == "0.1.0"


def test_a_flat_install_that_records_no_version_is_kept_as_the_lowest_version(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry())

    assert _version_exe(root, inst.UNKNOWN_FLAT_VERSION).read_bytes() == b"MZ-old"


def test_a_flat_install_that_records_a_bad_version_is_kept_as_the_lowest_version(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("v0.1.0-beta")))

    assert _version_exe(root, inst.UNKNOWN_FLAT_VERSION).is_file()


def test_installing_from_the_flat_folder_itself_migrates_it_and_copies_from_the_moved_copy(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(root, env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))

    assert _version_exe(root, "0.1.0").read_bytes() == b"MZ-old"
    assert _version_exe(root, __version__).read_bytes() == b"MZ-old"
    assert not (root / inst.EXE_NAME).exists()


def test_a_flat_install_of_the_same_version_is_replaced_by_the_new_bundle(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "same")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps(__version__)))

    assert _version_exe(root, __version__).read_bytes() == b"MZ-new"
    assert sorted(p.name for p in (root / "versions").iterdir()) == [__version__]


def test_the_previous_version_is_kept_and_older_ones_and_partial_copies_are_pruned(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    for version in ("0.0.9", "0.1.0"):
        _version_exe(root, version).parent.mkdir(parents=True)
        _version_exe(root, version).write_bytes(b"MZ-" + version.encode())
    (root / "versions" / "0.0.8.partial").mkdir()
    updater.write_current(root, "0.1.0")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry())

    assert sorted(p.name for p in (root / "versions").iterdir()) == ["0.1.0", __version__]


def test_the_shortcut_points_at_the_version_current_json_names_after_an_install(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    recorder = _Recorder()

    inst.install(_bundle(tmp_path), env, writer=recorder, registry=FakeRegistry())

    assert updater.read_current(root) == __version__
    assert all(target == _version_exe(root, __version__) for _shortcut, target in recorder.calls)


def test_autostart_follows_the_version_current_json_names(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _version_exe(root, "0.1.0").parent.mkdir(parents=True)
    _version_exe(root, "0.1.0").write_bytes(b"MZ")
    updater.write_current(root, "0.1.0")

    assert inst.autostart_target(env) == _version_exe(root, "0.1.0")


def test_update_shortcut_writers_cover_only_the_shortcuts_that_exist(tmp_path, monkeypatch):
    env = _env(tmp_path)
    written: list[tuple[Path, Path]] = []
    monkeypatch.setattr(inst, "create_shortcut", lambda shortcut, target: written.append((shortcut, target)))
    assert inst.update_shortcut_writers(env) == []  # nothing installed, so nothing is created

    start = inst.start_menu_shortcut(env)
    start.parent.mkdir(parents=True)
    start.write_bytes(b"lnk")
    writers = inst.update_shortcut_writers(env)
    assert len(writers) == 1
    exe = _version_exe(inst.install_dir(env), "0.2.0")
    writers[0](exe)
    assert written == [(start, exe)]


def test_update_shortcut_writers_include_the_run_entry_only_when_autostart_is_on(tmp_path, monkeypatch):
    env = _env(tmp_path)
    registry = FakeRegistry()
    monkeypatch.setattr(inst, "default_registry", lambda env: registry)
    SettingsStore(default_settings_path(env)).save(Settings(autostart=True))
    exe = _version_exe(inst.install_dir(env), "0.2.0")

    writers = inst.update_shortcut_writers(env)

    assert len(writers) == 1
    writers[0](exe)
    assert registry.keys[inst.RUN_KEY] == {inst.RUN_VALUE: f'"{exe}"'}


def test_uninstall_removes_every_version_folder_and_the_pointer_files(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))
    assert (root / updater.CURRENT_NAME).is_file()

    inst.uninstall(env, registry=FakeRegistry(), scheduler=None)

    assert not root.exists()


@pytest.mark.parametrize("flat", [True, False])
def test_the_installed_exe_is_never_left_in_the_install_folder_root(tmp_path, flat):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    if flat:
        _flat_install(root, "old")
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))
    assert not (root / inst.EXE_NAME).exists()
    assert _version_exe(root, __version__).is_file()


# --- a flat-install move that fails part-way, and the Apps and Features values the recorder writes ------------


def _state(root: Path) -> dict[str, bytes | None]:
    """Every path under root with its bytes (None for a folder), so a test can show that nothing changed."""
    paths = sorted(root.rglob("*"))
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None) for path in paths}


def _fail_the_third_move_out_of(root: Path, monkeypatch) -> None:
    """Make the third move out of root raise, as a disk error would. Moves back into root are not counted."""
    real_move = inst.shutil.move
    forward = {"moves": 0}

    def failing_move(source, destination):
        if Path(source).parent == root:
            forward["moves"] += 1
            if forward["moves"] == 3:
                raise OSError("the disk went away")
        return real_move(source, destination)

    monkeypatch.setattr(inst.shutil, "move", failing_move)


def _flat_install_with_three_entries(root: Path) -> None:
    _flat_install(root, "old")
    (root / "b.txt").write_bytes(b"b")
    (root / "c.txt").write_bytes(b"c")


def test_a_flat_install_move_that_fails_part_way_puts_every_entry_back(tmp_path, monkeypatch):
    root = inst.install_dir(_env(tmp_path))
    _flat_install_with_three_entries(root)
    before = _state(root)
    _fail_the_third_move_out_of(root, monkeypatch)

    with pytest.raises(OSError, match="disk went away"):
        inst._migrate_flat_install(root, FakeRegistry())

    assert _state(root) == before  # every entry is back where it was, and the new folder is gone


def test_install_reports_a_failed_flat_migration_and_leaves_the_flat_install_as_it_was(tmp_path, monkeypatch):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install_with_three_entries(root)
    before = _state(root)
    _fail_the_third_move_out_of(root, monkeypatch)

    with pytest.raises(inst.InstallError, match="could not copy"):
        inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))

    assert _state(root) == before


def test_the_recorder_writes_the_values_install_writes_for_the_version_it_is_given(tmp_path):
    registry = FakeRegistry()
    exe = tmp_path / "versions" / "0.2.0" / inst.EXE_NAME
    inst.apps_entry_recorder({}, registry=registry)(exe, "0.2.0")
    assert registry.keys[inst.UNINSTALL_KEY] == inst.apps_entry(exe, "0.2.0")
    assert inst.apps_entry(exe)["DisplayVersion"] == __version__  # install() still names the build it installs
