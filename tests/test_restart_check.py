"""Tests for the restart-needed check: the baseline, the server watch, the banner text and the start hook. No Qt.

Every file is written under tmp_path and its modification time is set by hand, so nothing depends on the clock. The
watch is given explicit modules and process readers, so a test never reads the real sys.modules or starts a process,
except the two Windows tests that read the creation time of a real process.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from meridian_desktop import runner_controls as rc
from meridian_desktop.restart_check import (
    GONE,
    FileStamp,
    Restart,
    RestartWatch,
    banner_text,
    display_name,
    package_files,
    process_started_ns,
    server_source_stamps,
)

BASE_NS = 1_700_000_000_000_000_000
NANOS_PER_SECOND = 1_000_000_000
# NTFS keeps 100 ns steps, so an edit a nanosecond after a time is stored as that same time. Tests step by seconds.
LATER_NS = BASE_NS + 60 * NANOS_PER_SECOND
NANOS_PER_HOUR = 3600 * NANOS_PER_SECOND


def _set_mtime(path, ns):
    os.utime(path, ns=(ns, ns))


def _write(path, text="x = 1\n", mtime=BASE_NS):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _set_mtime(path, mtime)
    return str(path)


def _module(name, file):
    module = ModuleType(name)
    module.__file__ = file
    return module


def _no_process(pid):
    return None


def _reader(alive):
    """A process reader over a dict of pid to creation time. A pid missing from the dict is GONE."""
    return lambda pid: alive.get(pid, GONE)


def _watch(modules, *, frozen=False, executable="", started_ns_of=_no_process):
    return RestartWatch(modules=modules, frozen=frozen, executable=executable, started_ns_of=started_ns_of)


def _locked_stat(path, real_stat):
    """An os.stat stand-in that fails with a sharing error for one path and passes every other call through."""

    def locked(target, *args, **kwargs):
        if os.fspath(target) == path:
            raise PermissionError(13, "locked by another process")
        return real_stat(target, *args, **kwargs)

    return locked


# ----------------------------------------------------------- the code baseline


def test_unchanged_code_raises_no_alert(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    assert watch.check() == Restart()
    assert not watch.check().needed


def test_changed_modification_time_is_reported_by_package_name(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "ui" / "main_window.py")
    watch = _watch({"meridian_desktop.ui.main_window": _module("meridian_desktop.ui.main_window", path)})
    _set_mtime(path, LATER_NS)
    assert watch.check() == Restart(code=("meridian_desktop/ui/main_window.py",))


def test_changed_size_is_reported_even_when_the_modification_time_is_the_same(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    _write(tmp_path / "meridian_desktop" / "a.py", text="x = 2222222\n", mtime=BASE_NS)
    assert watch.check().code == ("meridian_desktop/a.py",)


def test_a_deleted_code_file_is_reported(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    os.remove(path)
    assert watch.check() == Restart(code=("meridian_desktop/a.py",))


def test_a_new_file_on_disk_is_ignored(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    _write(tmp_path / "meridian_desktop" / "brand_new.py")
    assert watch.check() == Restart()


def test_a_module_imported_after_the_baseline_is_watched_from_its_first_check(tmp_path):
    _write(tmp_path / "meridian_desktop" / "a.py")
    modules = {}
    watch = _watch(modules)
    late = _write(tmp_path / "meridian_desktop" / "late.py")
    modules["meridian_desktop.late"] = _module("meridian_desktop.late", late)
    assert watch.check() == Restart()
    _set_mtime(late, LATER_NS)
    assert watch.check().code == ("meridian_desktop/late.py",)


def test_a_transient_stat_error_keeps_the_previous_stamp(tmp_path, monkeypatch):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", _locked_stat(path, real_stat))
    assert watch.check() == Restart()
    monkeypatch.setattr(os, "stat", real_stat)
    _set_mtime(path, LATER_NS)
    assert watch.check().code == ("meridian_desktop/a.py",)


def test_a_module_unreadable_at_the_baseline_is_taken_on_a_later_check(tmp_path, monkeypatch):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", _locked_stat(path, real_stat))
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    monkeypatch.setattr(os, "stat", real_stat)
    assert watch.check() == Restart()
    _set_mtime(path, LATER_NS)
    assert watch.check().code == ("meridian_desktop/a.py",)


def test_package_files_take_the_package_modules_and_their_init_files_only():
    modules = {
        "meridian_desktop": _module("meridian_desktop", "/x/meridian_desktop/__init__.py"),
        "meridian_desktop.ui.main_window": _module(
            "meridian_desktop.ui.main_window", "/x/meridian_desktop/ui/main_window.py"
        ),
        "meridian_desktopx": _module("meridian_desktopx", "/x/other.py"),
        "json": _module("json", "/lib/json/__init__.py"),
        "meridian_desktop.no_file": ModuleType("meridian_desktop.no_file"),
        "meridian_desktop.none": None,
    }
    assert package_files(modules) == {
        "/x/meridian_desktop/__init__.py",
        "/x/meridian_desktop/ui/main_window.py",
    }


def test_package_files_include_main_only_when_it_sits_in_the_package_folder():
    inside = {"__main__": _module("__main__", "C:\\app\\meridian_desktop\\__main__.py")}
    assert package_files(inside) == {"C:\\app\\meridian_desktop\\__main__.py"}
    outside = {"__main__": _module("__main__", "C:\\tools\\pytest\\__main__.py")}
    assert package_files(outside) == set()


# ----------------------------------------------------------- frozen build


def test_a_frozen_build_watches_its_executable(tmp_path):
    exe = _write(tmp_path / "Meridian.exe", "MZ", mtime=BASE_NS)
    watch = _watch({}, frozen=True, executable=exe)
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart(code=("Meridian.exe",))


def test_a_source_run_does_not_watch_the_python_executable(tmp_path):
    exe = _write(tmp_path / "python.exe", "MZ", mtime=BASE_NS)
    watch = _watch({}, frozen=False, executable=exe)
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart()


def test_the_frozen_flag_and_executable_are_read_when_the_watch_is_built(tmp_path, monkeypatch):
    exe = _write(tmp_path / "Meridian.exe", "MZ", mtime=BASE_NS)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    watch = RestartWatch(modules={}, started_ns_of=_no_process)  # no frozen or executable argument
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart(code=("Meridian.exe",))


def test_without_the_frozen_flag_the_executable_is_not_watched(tmp_path, monkeypatch):
    exe = _write(tmp_path / "python.exe", "MZ", mtime=BASE_NS)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    watch = RestartWatch(modules={}, started_ns_of=_no_process)  # no frozen or executable argument
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart()


# ----------------------------------------------------------- display names and banner text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:\\repo\\meridian_desktop\\ui\\main_window.py", "meridian_desktop/ui/main_window.py"),
        ("/x/meridian_desktop/__init__.py", "meridian_desktop/__init__.py"),
        ("C:/meridian_desktop/old/meridian_desktop/x.py", "meridian_desktop/x.py"),
        ("C:\\Program Files\\Meridian\\Meridian.exe", "Meridian.exe"),
    ],
)
def test_display_name_starts_at_the_package_folder(path, expected):
    assert display_name(path) == expected


def test_banner_text_is_empty_when_nothing_changed():
    assert banner_text(Restart()) == ""


def test_banner_text_names_one_changed_file():
    assert banner_text(Restart(code=("meridian_desktop/a.py",))) == (
        "Restart needed: meridian_desktop/a.py changed on disk since this window opened."
    )


def test_banner_text_names_three_files_and_counts_the_rest():
    names = ("meridian_desktop/a.py", "meridian_desktop/b.py", "meridian_desktop/c.py", "meridian_desktop/d.py")
    assert banner_text(Restart(code=names)) == (
        "Restart needed: meridian_desktop/a.py, meridian_desktop/b.py, meridian_desktop/c.py and 1 more "
        "changed on disk since this window opened."
    )


def test_banner_text_for_the_server_alone():
    assert banner_text(Restart(server=True)) == (
        "Restart needed: the local Meridian server changed on disk since it started."
    )


def test_banner_text_for_code_and_the_server_together():
    text = banner_text(Restart(code=("meridian_desktop/a.py",), server=True))
    assert text == (
        "Restart needed: meridian_desktop/a.py changed on disk since this window opened. "
        "The local Meridian server changed on disk since it started."
    )


# ----------------------------------------------------------- the local server watch


def _server_setup(tmp_path, *, pid=4321, started=BASE_NS + 10 * NANOS_PER_SECOND):
    """A checkout with one server file written before the server started, and a process reader that knows the pid."""
    checkout = tmp_path / "core"
    source = _write(checkout / "meridian" / "server.py", mtime=BASE_NS)
    alive = {pid: started}
    return checkout, source, alive


def test_a_server_file_edited_after_the_server_started_marks_it_stale(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    assert not watch.check().server
    _set_mtime(source, alive[4321] + NANOS_PER_SECOND)
    assert watch.check() == Restart(server=True)


def test_a_file_with_the_same_size_and_an_older_timestamp_is_caught(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    # Same size, and a timestamp from before the server started: a replacement that a newer-than-start check misses.
    _write(source, text="x = 9\n", mtime=BASE_NS - 60 * NANOS_PER_SECOND)
    assert watch.check() == Restart(server=True)


def test_a_size_change_with_the_same_timestamp_is_caught(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _write(source, text="x = 2222222\n", mtime=BASE_NS)
    assert watch.check() == Restart(server=True)


def test_a_non_python_file_under_the_server_package_does_not_mark_it_stale(tmp_path):
    checkout, _source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _write(checkout / "meridian" / "static" / "page.js", mtime=LATER_NS)
    assert not watch.check().server


def test_a_python_file_added_after_the_start_is_not_seen(tmp_path):
    # The snapshot is taken when the start is confirmed, so a file that appears later is outside it. This pins the limit.
    checkout, _source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _write(checkout / "meridian" / "new_module.py", mtime=LATER_NS)
    assert not watch.check().server


def test_the_server_watch_drops_the_server_when_its_process_is_gone(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    started = alive[4321]
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, started + NANOS_PER_SECOND)
    assert watch.check().server
    alive[4321] = GONE
    assert not watch.check().server
    # The same process back under the same pid is not watched again. The owner has to start it from the window.
    alive[4321] = started
    assert not watch.check().server


def test_the_server_watch_drops_the_server_when_its_pid_is_reused(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, alive[4321] + NANOS_PER_SECOND)
    alive[4321] = alive[4321] + 999  # the same pid now belongs to a process with another creation time
    assert not watch.check().server


def test_an_unreadable_creation_time_keeps_the_watch_and_it_is_read_again(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    started = alive[4321]
    reading = {"value": started}
    watch = _watch({}, started_ns_of=lambda pid: reading["value"])
    watch.note_server_started(4321, str(checkout))
    reading["value"] = None  # the query fails on this tick
    _set_mtime(source, started + NANOS_PER_SECOND)
    assert watch.check().server
    reading["value"] = started  # readable again, and the server is still watched
    assert watch.check().server


def test_a_server_whose_start_time_cannot_be_read_is_not_watched(tmp_path):
    checkout, source, _alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_no_process)
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, LATER_NS)
    assert not watch.check().server


def test_a_server_that_is_already_gone_at_the_start_is_not_watched(tmp_path):
    checkout, source, _alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=lambda pid: GONE)
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, LATER_NS)
    assert not watch.check().server


def test_no_checkout_means_no_server_is_watched(tmp_path, monkeypatch):
    # A .py file sits in the working folder's meridian package. Without the empty-checkout guard the watch would list it.
    monkeypatch.chdir(tmp_path)
    source = _write(tmp_path / "meridian" / "server.py", mtime=BASE_NS)
    alive = {4321: BASE_NS + 10 * NANOS_PER_SECOND}
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, "")
    _set_mtime(source, LATER_NS)
    assert not watch.check().server


def test_clearing_a_gone_server_does_not_drop_a_newer_start(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    started = alive[4321]
    alive[5555] = started
    state = {"replace": False}
    watch = None

    def reader(pid):
        if pid == 4321 and state["replace"]:
            # While the old server is being checked, a new start is confirmed and replaces it.
            watch.note_server_started(5555, str(checkout))
            return GONE
        return alive.get(pid, GONE)

    watch = _watch({}, started_ns_of=reader)
    watch.note_server_started(4321, str(checkout))
    state["replace"] = True
    assert not watch.check().server
    _set_mtime(source, started + NANOS_PER_SECOND)
    assert watch.check().server  # the newer start is still watched


def test_server_source_stamps_lists_python_files_only_and_a_missing_folder_is_empty(tmp_path):
    root = tmp_path / "meridian"
    _write(root / "a.py", mtime=BASE_NS)
    _write(root / "sub" / "b.py", mtime=LATER_NS)
    _write(root / "data.json", mtime=LATER_NS + NANOS_PER_HOUR)
    stamps = server_source_stamps(root)
    assert sorted(os.path.basename(path) for path in stamps) == ["a.py", "b.py"]
    b_path = os.path.join(str(root), "sub", "b.py")
    assert stamps[b_path] == FileStamp(LATER_NS, os.stat(b_path).st_size)
    assert server_source_stamps(tmp_path / "missing") == {}


# ----------------------------------------------------------- the process creation time


@pytest.mark.skipif(sys.platform != "win32", reason="the creation time is read through Windows")
def test_process_started_ns_reads_the_creation_time_of_this_process():
    started = process_started_ns(os.getpid())
    assert isinstance(started, int)
    age_ns = time.time_ns() - started
    assert 0 < age_ns < 24 * NANOS_PER_HOUR


@pytest.mark.skipif(sys.platform != "win32", reason="the creation time is read through Windows")
def test_process_started_ns_is_gone_for_a_pid_that_names_no_process():
    assert process_started_ns(0x7FFFFFF0) is GONE


@pytest.mark.skipif(sys.platform != "win32", reason="the creation time is read through Windows")
def test_process_started_ns_is_gone_for_a_process_that_has_exited_but_is_still_open_here():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=60)
    # Popen still holds the process handle, so the query can open the process. Its exit time is set, so it is gone.
    assert process_started_ns(child.pid) is GONE


def test_process_started_ns_is_none_off_windows_and_gone_for_pid_zero(monkeypatch):
    assert process_started_ns(0) is GONE
    monkeypatch.setattr(sys, "platform", "linux")
    assert process_started_ns(os.getpid()) is None


# ----------------------------------------------------------- the runner's start hook


class _FakeSpawn:
    def __init__(self, *replies):
        self.replies = list(replies)

    def __call__(self, argv, **kwargs):
        return self.replies.pop(0)


def _done(payload, code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=json.dumps(payload).encode("utf-8"), stderr=b"")


def _control(tmp_path, spawn, calls):
    return rc.RunnerControl(
        sys.executable,
        str(tmp_path),
        spawn=spawn,
        log_path=tmp_path / "control.log",
        on_started=lambda pid, cwd: calls.append((pid, cwd)),
    )


def test_a_confirmed_local_start_hands_its_pid_and_checkout_to_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "running", "pid": 4321}}))
    result = _control(tmp_path, spawn, calls).start("local")
    assert result.outcome == rc.CONFIRMED
    assert calls == [(4321, str(tmp_path))]


def test_a_tunnel_start_does_not_reach_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "tunnel", "child": {"state": "running", "pid": 5555}}))
    assert _control(tmp_path, spawn, calls).start("tunnel").outcome == rc.CONFIRMED
    assert calls == []


def test_a_local_start_that_is_not_running_does_not_reach_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "crashed", "pid": 4321}}))
    assert _control(tmp_path, spawn, calls).start("local").outcome == rc.FAILED
    assert calls == []


def test_a_status_call_does_not_reach_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "running", "pid": 4321}}))
    _control(tmp_path, spawn, calls).status("local")
    assert calls == []
