"""Tests for the agent program registry and process tree grouping (meridian_desktop/agent_processes.py), and for the listing
that uses them (claude_processes.list_claude_processes).

Fake process tables and fake runners only. The one smoke test calls the real snapshot and checks only that it returns a
list. Nothing here starts, stops or changes a process.
"""

import dataclasses
import json
import subprocess
import sys

import pytest

from meridian_desktop import agent_processes as ap
from meridian_desktop import claude_processes as cp
from meridian_desktop import settings_store
from meridian_desktop.agent_processes import AgentProcess
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.sessions_panel import CLAUDE_NONE_TEXT, CLAUDE_TASKLIST_NOTE, claude_status_text

SECRET_PATH = r"C:\Users\13144\AppData\Roaming\Agent\secret-prompt-text"
SECRET_ARG = "--token=sk_meridian_AGENTSENTINEL0123456789"
CHECKED = "12:00:00"

# Fixed tasklist /FO LIST text: two Claude processes, one Codex process and node.exe, which is not counted. The path and
# the argument sit in fields the parser must ignore.
TASKLIST = (
    "Image Name:   claude.exe\r\n"
    "PID:          900\r\n"
    f"Session Name: {SECRET_PATH}\r\n"
    f"Session#:     {SECRET_ARG}\r\n"
    "\r\n"
    "Image Name:   Claude.exe\r\n"
    "PID:          14232\r\n"
    "Session Name: Console\r\n"
    "\r\n"
    "Image Name:   codex.exe\r\n"
    "PID:          777\r\n"
    "Session Name: Console\r\n"
    "\r\n"
    "Image Name:   node.exe\r\n"
    "PID:          7744\r\n"
)


def proc(image, pid, ppid=0):
    return AgentProcess(image=image, pid=pid, ppid=ppid)


def _group_tuples(groups):
    return [(group.label, group.roots, group.total) for group in groups]


def _grouped(processes, custom=()):
    return _group_tuples(ap.group_processes(processes, ap.registry(custom)))


def _snapshot_raises():
    raise OSError(SECRET_PATH)


def _tasklist_fails():
    raise OSError(SECRET_PATH)


# ----------------------------------------------------------------- registry


def test_the_registry_matches_claude_codex_cursor_and_a_custom_name_without_regard_to_case():
    labels = ap.registry(["myagent.exe"])
    assert labels["claude.exe"] == "Claude"
    assert labels["codex.exe"] == "Codex"
    assert labels["cursor.exe"] == "Cursor"
    assert labels["myagent.exe"] == "myagent"
    table = [
        proc("CLAUDE.EXE", 1),
        proc("Codex.exe", 2),
        proc("cursor.EXE", 3),
        proc("MyAgent.exe", 4),
        proc("node.exe", 5),
    ]
    assert [process.pid for process in ap.matching(table, labels)] == [1, 2, 3, 4]


def test_the_built_in_programs_come_first_and_then_the_custom_names():
    assert list(ap.registry(["myagent.exe"])) == ["claude.exe", "codex.exe", "cursor.exe", "myagent.exe"]


def test_a_custom_name_is_labelled_with_its_file_name_without_exe():
    assert ap.registry(["MyAgent.EXE"])["myagent.exe"] == "MyAgent"


# ----------------------------------------------------------------- grouping


def test_grouping_counts_the_roots_and_every_descendant_of_the_same_program():
    table = [
        proc("claude.exe", 100, ppid=4),  # a root: its parent is not a Claude process
        proc("claude.exe", 101, ppid=100),
        proc("claude.exe", 102, ppid=100),
        proc("claude.exe", 103, ppid=102),
        proc("node.exe", 200, ppid=4),
        proc("claude.exe", 201, ppid=200),  # a root: its parent is node.exe, which is not counted
    ]
    assert _grouped(table) == [("Claude", 2, 5)]


def test_a_child_whose_parent_has_the_same_label_is_not_a_root():
    assert _grouped([proc("codex.exe", 10, ppid=4), proc("codex.exe", 11, ppid=10)]) == [("Codex", 1, 2)]


def test_a_program_started_by_another_agent_program_is_a_root_of_its_own_label():
    table = [proc("claude.exe", 300, ppid=4), proc("cursor.exe", 301, ppid=300), proc("cursor.exe", 302, ppid=301)]
    assert _grouped(table) == [("Claude", 1, 1), ("Cursor", 1, 2)]


def test_only_programs_with_a_process_get_a_group_and_they_keep_the_registry_order():
    table = [proc("cursor.exe", 1), proc("claude.exe", 2), proc("myagent.exe", 3)]
    assert _grouped(table, custom=["myagent.exe"]) == [("Claude", 1, 1), ("Cursor", 1, 1), ("myagent", 1, 1)]
    assert _grouped([proc("notepad.exe", 9)]) == []


def test_a_parent_loop_is_counted_as_no_root_and_does_not_hang():
    table = [proc("codex.exe", 5, ppid=5), proc("codex.exe", 6, ppid=7), proc("codex.exe", 7, ppid=6)]
    assert _grouped(table) == []


# ----------------------------------------------------------------- custom names


def test_invalid_custom_names_are_dropped_and_counted_and_valid_ones_are_kept():
    values = [
        "a.exe",  # the shortest valid name
        "codex.exe",  # a built-in program: already listed, so skipped and not counted
        "MyAgent.EXE",
        "myagent.exe",  # a repeat of MyAgent: skipped and not counted
        ".exe",  # 4 characters
        "a" * 61 + ".exe",  # 65 characters
        "b" * 60 + ".exe",  # 64 characters, the longest valid name
        "dir/tool.exe",
        "dir\\tool.exe",
        "C:tool.exe",  # a drive letter
        "tool.com",  # not .exe
        "bad*name.exe",
        "   ",
        42,
        None,
        "..exe",
        "ends .exe",  # the name part ends in a space
    ]
    names, dropped = ap.clean_custom_names(values)
    assert names == ("a.exe", "MyAgent.EXE", "b" * 60 + ".exe")
    assert dropped == 12


# ----------------------------------------------------------------- the listing: snapshot first, then tasklist


def test_a_readable_snapshot_is_used_and_tasklist_is_not_run(monkeypatch):
    def tasklist_must_not_run():
        raise AssertionError("tasklist must not run when the snapshot can be read")

    monkeypatch.setattr(cp, "_tasklist_text", tasklist_must_not_run)
    table = [proc("claude.exe", 1, ppid=4), proc("claude.exe", 2, ppid=1), proc("codex.exe", 3, ppid=4)]
    result = cp.list_claude_processes(snapshot=lambda: table, custom_names=())
    assert result.state == cp.LISTED
    assert result.source == cp.SOURCE_SNAPSHOT
    assert _group_tuples(result.groups) == [("Claude", 1, 2), ("Codex", 1, 1)]
    assert [process.pid for process in result.processes] == [1, 2, 3]


@pytest.mark.parametrize("failing", [lambda: None, _snapshot_raises])
def test_a_snapshot_that_cannot_be_taken_falls_back_to_tasklist_and_the_result_says_so(monkeypatch, failing):
    monkeypatch.setattr(cp, "_tasklist_text", lambda: TASKLIST)
    result = cp.list_claude_processes(snapshot=failing, custom_names=())
    assert result.state == cp.LISTED
    assert result.source == cp.SOURCE_TASKLIST
    # tasklist gives no parent id, so each process is a root of its program
    assert _group_tuples(result.groups) == [("Claude", 2, 2), ("Codex", 1, 1)]
    line = claude_status_text(result, CHECKED)
    # The time comes first, and the tasklist note after it, so the line still starts with the time.
    assert line == f"3 agent processes running on this computer. Checked at {CHECKED}. {CLAUDE_TASKLIST_NOTE}"


def test_when_the_snapshot_and_tasklist_both_fail_the_listing_is_unavailable_with_fixed_text(monkeypatch):
    monkeypatch.setattr(cp, "_tasklist_text", _tasklist_fails)
    result = cp.list_claude_processes(snapshot=lambda: None, custom_names=())
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.UNAVAILABLE_MESSAGE
    assert SECRET_PATH not in repr(result)


def test_a_timed_out_tasklist_after_a_failed_snapshot_says_so(monkeypatch):
    def slow():
        raise subprocess.TimeoutExpired(cmd="tasklist", timeout=5.0)

    monkeypatch.setattr(cp, "_tasklist_text", slow)
    result = cp.list_claude_processes(snapshot=lambda: None, custom_names=())
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.TIMEOUT_MESSAGE


def test_a_caller_that_passes_a_runner_gets_the_tasklist_path_only():
    def snapshot_must_not_run():
        raise AssertionError("the snapshot must not be read when a runner is passed")

    result = cp.list_claude_processes(run=lambda: TASKLIST, snapshot=snapshot_must_not_run, custom_names=())
    assert result.source == cp.SOURCE_TASKLIST
    assert _group_tuples(result.groups) == [("Claude", 2, 2), ("Codex", 1, 1)]


def test_tasklist_text_matches_the_registry_names_and_nothing_else():
    processes = cp.parse_tasklist(TASKLIST)
    assert [(process.image, process.pid) for process in processes] == [
        ("claude.exe", 900),
        ("Claude.exe", 14232),
        ("codex.exe", 777),
    ]


def test_the_snapshot_is_not_taken_outside_windows(monkeypatch):
    monkeypatch.setattr(ap.sys, "platform", "linux")
    assert ap.snapshot_processes() is None


def test_the_listing_reads_the_saved_custom_names_from_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = settings_store.default_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent_process_names": ["myagent.exe", "bad/x.exe"]}), encoding="utf-8")
    result = cp.list_claude_processes(snapshot=lambda: [proc("myagent.exe", 9)])
    assert _group_tuples(result.groups) == [("myagent", 1, 1)]
    assert result.dropped == 1


# ----------------------------------------------------------------- what reaches the screen


def test_a_process_and_a_group_hold_only_names_and_numbers():
    assert [field.name for field in dataclasses.fields(AgentProcess)] == ["image", "pid", "ppid"]
    assert [field.name for field in dataclasses.fields(ap.AgentGroup)] == ["label", "roots", "total"]


def test_no_output_carries_a_path_or_an_argument_from_the_tasklist_text():
    result = cp.list_claude_processes(run=lambda: TASKLIST, custom_names=())
    assert SECRET_PATH not in repr(result)
    assert SECRET_ARG not in repr(result)
    line = claude_status_text(result, CHECKED)
    for marker in ("\\", "/", "--", "token", "secret", "C:"):
        assert marker not in line


def test_the_status_line_says_not_connected_only_when_no_program_matches():
    empty = cp.list_claude_processes(snapshot=lambda: [proc("notepad.exe", 9)], custom_names=())
    assert claude_status_text(empty, CHECKED) == f"{CLAUDE_NONE_TEXT} Checked at {CHECKED}."
    listed = cp.list_claude_processes(snapshot=lambda: [proc("cursor.exe", 9)], custom_names=())
    assert "Not connected" not in claude_status_text(listed, CHECKED)
    assert claude_status_text(listed, CHECKED) == f"1 agent process running on this computer. Checked at {CHECKED}."


def test_saved_names_that_are_not_valid_are_counted_in_the_status_line():
    result = cp.list_claude_processes(snapshot=lambda: [], custom_names=["bad/x.exe", "ok_agent.exe"])
    assert result.state == cp.LISTED
    assert result.dropped == 1
    assert claude_status_text(result, CHECKED) == (
        f"{CLAUDE_NONE_TEXT} Checked at {CHECKED}. 1 saved program name is not valid and was left out."
    )


# ----------------------------------------------------------------- settings


def test_an_old_settings_file_without_the_key_loads_with_no_custom_names(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mode": "local", "local_url": "http://127.0.0.1:7878"}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.warning is None
    assert result.settings.agent_process_names == ()


def test_saved_custom_names_round_trip_and_an_empty_list_is_not_written(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(Settings(agent_process_names=("myagent.exe",)))
    assert json.loads(path.read_text(encoding="utf-8"))["agent_process_names"] == ["myagent.exe"]
    assert SettingsStore(path).load().settings.agent_process_names == ("myagent.exe",)
    store.save(Settings())
    assert "agent_process_names" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_saved_value_that_is_not_a_list_warns_and_gives_no_names(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent_process_names": "myagent.exe"}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.agent_process_names == ()
    assert "agent_process_names must be a list" in result.warning


def test_a_non_text_entry_loads_as_an_empty_name_that_is_counted_as_dropped(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent_process_names": ["myagent.exe", 7]}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.warning is None
    assert ap.clean_custom_names(result.settings.agent_process_names) == (("myagent.exe",), 1)


# ----------------------------------------------------------------- smoke test on the real table


@pytest.mark.skipif(sys.platform != "win32", reason="the toolhelp snapshot is Windows only")
def test_the_real_snapshot_returns_a_list():
    assert isinstance(ap.snapshot_processes(), list)
