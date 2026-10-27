"""Tests for meridian_desktop/client_setup_runner.py: the core's plan and apply output, read into typed results.

The process runner is always a fake, so no test starts the real core command. The one test that checks
the real run_process patches subprocess.run, so it still starts nothing. Every file the tests touch lives
in pytest's temp directory. FAKE_TOKEN and BODY_LEAK are obviously fake values for leak checks.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from meridian_desktop import client_setup_runner as runner_mod
from meridian_desktop.client_setup_runner import (
    BAD_MODE_TEXT,
    BAD_OUTPUT_TEXT,
    CHECKED_TEXT,
    CHECKOUT_MISSING_TEXT,
    CHECKOUT_NOT_CORE_TEXT,
    CLIENTS,
    CORE_CLIENTS,
    CORE_MODULE,
    COMMAND_TIMEOUT_SECONDS,
    FAILED_TEXT,
    IN_PLACE,
    KIND_BAD_OUTPUT,
    KIND_CANNOT_START,
    KIND_NOT_CONFIGURED,
    KIND_NOT_COVERED,
    KIND_NOTHING_TICKED,
    KIND_TIMEOUT,
    MISSING,
    NO_RESULT_TEXT,
    NOT_CHECKED,
    NOT_COVERED,
    NOT_COVERED_TEXT,
    NOT_INSTALLED_TEXT,
    NOT_READABLE,
    NOT_SELECTED,
    NOT_SELECTED_TEXT,
    NOT_SET_CHECKOUT_TEXT,
    NOT_SET_PYTHON_TEXT,
    NOTHING_TICKED_TEXT,
    PARTIAL_TEXT,
    PYTHON_MISSING_TEXT,
    STATE_TEXT,
    WOULD_CHANGE,
    ClientChange,
    ClientRow,
    ClientSetupRunner,
    CommandOutput,
    covered_ticked,
    names_text,
    parse_changes,
    run_process,
    ticked_clients,
    unchecked_rows,
)
from meridian_desktop.settings_store import CLIENT_CHOICE_NAMES, Settings, SettingsStore

REPO = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "sk_meridian_fake-runner-0006"
BODY_LEAK = "BODY-SECRET-DO-NOT-SHOW"
TRACE_LEAK = "SECRET-TRACE-DO-NOT-SHOW"


# ----------------------------------------------------------- helpers


def make_core(tmp_path: Path) -> Path:
    """A folder that looks like a Meridian core checkout: it holds the meridian package."""
    checkout = tmp_path / "core"
    (checkout / "meridian").mkdir(parents=True)
    (checkout / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    return checkout


def make_python(tmp_path: Path) -> str:
    """A file that stands in for the Python program. The runner never runs it: the run function is a fake."""
    python = tmp_path / "venv" / "python.exe"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("not a real interpreter", encoding="utf-8")
    return str(python)


def configured_store(tmp_path: Path, settings_path: Path) -> SettingsStore:
    store = SettingsStore(settings_path)
    store.save(Settings(core_checkout=str(make_core(tmp_path)), core_python=make_python(tmp_path)))
    return store


class FakeRun:
    """Records each call and answers from a responder. It never starts a process."""

    def __init__(self, respond) -> None:
        self._respond = respond
        self.calls: list[tuple[list[str], Path, int]] = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append((list(argv), Path(cwd), timeout))
        outcome = self._respond(list(argv))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _cwd, _timeout in self.calls]


def out(payload, returncode: int = 0, stderr: str = "") -> CommandOutput:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return CommandOutput(returncode, text, stderr)


def entry(client, path, action, *, added=(), changed=(), removed=(), backup=None, restart=False, **extra) -> dict:
    """One result entry, shaped the way the core's client_setup prints it."""
    item = {
        "client": client,
        "path": str(path),
        "action": action,
        "changes": {"added": list(added), "changed": list(changed), "removed": list(removed), "unchanged": []},
        "re_enabled": [],
        "backup": backup,
        "restart_required": restart,
    }
    item.update(extra)
    return item


def results(*entries, wrote: bool = False) -> dict:
    return {"results": list(entries), "wrote": wrote}


def core_error(message: str) -> CommandOutput:
    return CommandOutput(1, "", json.dumps({"error": message}, indent=2))


def row_for(result, client: str) -> ClientRow:
    return next(row for row in result.rows if row.client == client)


# ----------------------------------------------------------- refusals: no process starts


def test_unset_checkout_is_refused_and_names_the_setting(settings_path):
    run = FakeRun(lambda argv: out(results()))
    result = ClientSetupRunner(SettingsStore(settings_path), run=run).check()

    assert result.configured is False
    assert result.message == NOT_SET_CHECKOUT_TEXT
    assert "Meridian core checkout" in result.message
    assert run.calls == [], "no process may start when the checkout is not set"


def test_unset_python_is_refused_and_names_the_setting(settings_path, tmp_path):
    SettingsStore(settings_path).save(Settings(core_checkout=str(make_core(tmp_path))))
    run = FakeRun(lambda argv: out(results()))
    result = ClientSetupRunner(SettingsStore(settings_path), run=run).check()

    assert result.configured is False
    assert result.message == NOT_SET_PYTHON_TEXT
    assert "Python for Meridian core" in result.message
    assert run.calls == []


def test_missing_checkout_folder_is_refused_with_the_path(settings_path, tmp_path):
    gone = tmp_path / "no-such-checkout"
    SettingsStore(settings_path).save(Settings(core_checkout=str(gone), core_python=make_python(tmp_path)))
    run = FakeRun(lambda argv: out(results()))
    result = ClientSetupRunner(SettingsStore(settings_path), run=run).check()

    assert result.message == CHECKOUT_MISSING_TEXT.format(path=str(gone))
    assert run.calls == []


def test_checkout_without_the_meridian_package_is_refused(settings_path, tmp_path):
    folder = tmp_path / "not-core"
    folder.mkdir()
    SettingsStore(settings_path).save(Settings(core_checkout=str(folder), core_python=make_python(tmp_path)))
    run = FakeRun(lambda argv: out(results()))
    result = ClientSetupRunner(SettingsStore(settings_path), run=run).check()

    assert result.message == CHECKOUT_NOT_CORE_TEXT.format(path=str(folder))
    assert run.calls == []


def test_missing_python_program_is_refused_with_the_path(settings_path, tmp_path):
    python = tmp_path / "gone" / "python.exe"
    SettingsStore(settings_path).save(Settings(core_checkout=str(make_core(tmp_path)), core_python=str(python)))
    run = FakeRun(lambda argv: out(results()))
    result = ClientSetupRunner(SettingsStore(settings_path), run=run).check()

    assert result.message == PYTHON_MISSING_TEXT.format(path=str(python))
    assert run.calls == []


def test_apply_and_preview_refuse_before_any_process_when_unset(settings_path):
    run = FakeRun(lambda argv: out(results()))
    runner = ClientSetupRunner(SettingsStore(settings_path), run=run)

    preview = runner.preview(["codex"], "apply")
    change = runner.run_change(["codex"], "apply")

    assert preview.ok is False and preview.kind == KIND_NOT_CONFIGURED
    assert change.ok is False and change.kind == KIND_NOT_CONFIGURED
    assert run.calls == []


def test_describe_command_says_what_is_missing_when_unset(settings_path):
    runner = ClientSetupRunner(SettingsStore(settings_path), run=FakeRun(lambda argv: out(results())))

    assert runner.describe_command(["codex"], "apply") == NOT_SET_CHECKOUT_TEXT


# ----------------------------------------------------------- the command itself


def test_check_with_no_list_plans_the_saved_ticks_from_the_checkout_as_an_argument_list(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings
    run = FakeRun(lambda argv: out(results()))

    ClientSetupRunner(store, run=run).check()

    assert run.argvs == [plan_argv(settings.core_python, "claude-desktop,codex")]
    assert "all" not in run.argvs[0], "a check never asks for every client at once"
    assert run.calls[0][1] == Path(settings.core_checkout)
    assert run.calls[0][2] == COMMAND_TIMEOUT_SECONDS == 60
    assert all(isinstance(part, str) for part in run.argvs[0])
    assert "--yes" not in run.argvs[0], "a check must never write"


def test_with_cursor_unticked_the_plan_never_names_cursor(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    store.set_client_choices(["claude-desktop", "codex"])
    python = store.load().settings.core_python
    run = FakeRun(lambda argv: out(results()))

    result = ClientSetupRunner(store, run=run).check()

    assert run.argvs == [plan_argv(python, "claude-desktop,codex")]
    assert all("cursor" not in part for part in run.argvs[0][1:])
    assert row_for(result, "cursor").state == NOT_SELECTED


def test_with_nothing_ticked_check_runs_nothing(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    store.set_client_choices([])
    run = FakeRun(lambda argv: out(results()))

    result = ClientSetupRunner(store, run=run).check()

    assert run.calls == [], "with no client ticked, no process starts"
    assert result.message == NOTHING_TICKED_TEXT
    assert all(row.state == NOT_SELECTED for row in result.rows)


def test_run_process_passes_an_argument_list_and_no_shell(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    output = run_process(["python", "-m", CORE_MODULE, "plan"], tmp_path, 60)

    assert isinstance(captured["args"], list)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 60
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert output == CommandOutput(0, "{}", "")


def test_claude_code_is_never_sent_to_the_core(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results()))
    runner = ClientSetupRunner(store, run=run)

    runner.check()
    runner.preview(["claude-code"], "apply")
    runner.run_change(["claude-code"], "remove")

    assert all("claude-code" not in " ".join(argv) for argv in run.argvs)
    assert run.argvs == [plan_argv(store.load().settings.core_python, "claude-desktop,codex")], "only the check ran"


# ----------------------------------------------------------- reading the plan


def test_plan_renders_each_state(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    existing = tmp_path / "claude_desktop_config.json"
    existing.write_text("{}", encoding="utf-8")
    absent = tmp_path / "codex" / "config.toml"  # does not exist: a new file
    payload = results(
        entry("claude-desktop", existing, "would_write", added=["meridian-docs"], changed=["meridian-search"]),
        entry("codex", absent, "would_write", added=["meridian-outputs"]),
    )
    run = FakeRun(lambda argv: out(payload))

    result = ClientSetupRunner(store, run=run).check()

    desktop = row_for(result, "claude-desktop")
    assert desktop.state == WOULD_CHANGE
    assert desktop.path == str(existing)
    assert desktop.detail == "Adds: meridian-docs. Changes: meridian-search."

    codex = row_for(result, "codex")
    assert codex.state == MISSING
    assert codex.path == str(absent)
    assert codex.detail == "Adds: meridian-outputs."


def test_unchanged_entry_is_in_place(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    run = FakeRun(lambda argv: out(results(entry("codex", config, "unchanged"))))

    result = ClientSetupRunner(store, run=run).check()

    assert row_for(result, "codex").state == IN_PLACE
    assert result.message == "Checked. Nothing was written."


def test_claude_code_row_is_not_covered_and_says_what_to_do(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    store.set_client_choices(["claude-desktop", "codex", "cursor", "claude-code"])
    run = FakeRun(lambda argv: out(results()))

    result = ClientSetupRunner(store, run=run).check()

    assert run.argvs == [plan_argv(store.load().settings.core_python, "claude-desktop,codex,cursor")]
    row = row_for(result, "claude-code")
    assert row.state == NOT_COVERED
    assert row.covered is False
    assert "Update the Meridian core checkout" in row.detail


def test_a_core_client_missing_from_the_output_is_not_checked(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    run = FakeRun(lambda argv: out(results(entry("codex", config, "unchanged"))))

    result = ClientSetupRunner(store, run=run).check()

    desktop = row_for(result, "claude-desktop")
    assert desktop.state == NOT_CHECKED
    assert desktop.detail == NO_RESULT_TEXT


def test_an_unknown_action_is_not_readable(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results(entry("codex", tmp_path / "x", "frobnicated"))))

    result = ClientSetupRunner(store, run=run).check()

    row = row_for(result, "codex")
    assert row.state == NOT_READABLE
    assert "frobnicated" in row.detail


def test_only_names_and_paths_are_kept_from_the_output(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    payload = results(
        entry(
            "codex",
            tmp_path / "config.toml",
            "would_write",
            added=["meridian-docs"],
            document={"mcp_servers": {"x": {"env": BODY_LEAK}}},
            text=BODY_LEAK,
        )
    )
    run = FakeRun(lambda argv: out(payload))

    result = ClientSetupRunner(store, run=run).check()

    assert BODY_LEAK not in repr(result)
    for row_type in (ClientRow, ClientChange):
        names = {field.name for field in dataclasses.fields(row_type)}
        assert "document" not in names and "text" not in names


def test_names_text_says_plainly_when_nothing_changes():
    change = ClientChange(client="codex", path="x", action="would_write")
    assert names_text(change) == "No Meridian entry changes."


def test_parse_changes_rejects_a_missing_results_list():
    with pytest.raises(ValueError):
        parse_changes('{"wrote": false}')


def test_parse_changes_rejects_a_result_without_a_path():
    with pytest.raises(ValueError):
        parse_changes(json.dumps({"results": [{"client": "codex", "action": "unchanged"}]}))


def test_the_unchecked_rows_cover_every_client_in_order():
    rows = unchecked_rows()

    assert [row.client for row in rows] == [client for client, _label in CLIENTS]
    assert {row.client for row in rows if row.covered} == CORE_CLIENTS
    assert row_for_list(rows, "claude-code").state == NOT_COVERED
    assert row_for_list(rows, "codex").state == NOT_CHECKED


def row_for_list(rows, client):
    return next(row for row in rows if row.client == client)


# ----------------------------------------------------------- failures become typed results


def test_a_timeout_is_a_typed_error(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: subprocess.TimeoutExpired(argv, COMMAND_TIMEOUT_SECONDS))
    runner = ClientSetupRunner(store, run=run)

    result = runner.check()
    change = runner.run_change(["codex"], "apply")

    assert "60 seconds" in result.message
    assert row_for(result, "codex").state == NOT_CHECKED
    assert change.ok is False and change.kind == KIND_TIMEOUT
    assert "60 seconds" in change.message


def test_a_program_that_cannot_start_names_only_the_error_class(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: FileNotFoundError(f"[WinError 2] {make_python(tmp_path)}"))
    change = ClientSetupRunner(store, run=run).run_change(["codex"], "apply")

    assert change.ok is False and change.kind == KIND_CANNOT_START
    assert "FileNotFoundError" in change.message
    assert "WinError" not in change.message


def test_a_core_error_is_read_per_client_so_one_bad_file_does_not_hide_the_rest(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    bad_json = "claude_desktop_config.json is not valid JSON; refusing to rewrite it"

    def respond(argv):
        if argv[-1] == "claude-desktop,codex":
            return core_error(bad_json)
        if "claude-desktop" in argv:
            return core_error(bad_json)
        return out(results(entry("codex", config, "unchanged")))

    run = FakeRun(respond)
    result = ClientSetupRunner(store, run=run).check()

    python = store.load().settings.core_python
    assert run.argvs[0] == plan_argv(python, "claude-desktop,codex")
    assert [python, "-m", CORE_MODULE, "plan", "--client", "claude-desktop"] in run.argvs
    assert [python, "-m", CORE_MODULE, "plan", "--client", "codex"] in run.argvs
    assert result.message == PARTIAL_TEXT
    desktop = row_for(result, "claude-desktop")
    assert desktop.state == NOT_READABLE
    assert desktop.detail == bad_json
    assert row_for(result, "codex").state == IN_PLACE


def test_a_failure_without_the_core_error_shape_shows_the_exit_code_only(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: CommandOutput(1, "", f"Traceback (most recent call last): {TRACE_LEAK}"))

    result = ClientSetupRunner(store, run=run).check()

    assert result.message == FAILED_TEXT.format(code=1)
    assert TRACE_LEAK not in result.message
    planned = [row for row in result.rows if row.covered and row.state != NOT_SELECTED]
    assert [row.client for row in planned] == ["claude-desktop", "codex"]
    assert all(row.state == NOT_CHECKED for row in planned)


def test_output_that_is_not_json_is_bad_output(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out("this is not json"))
    result = ClientSetupRunner(store, run=run).check()

    assert result.message == BAD_OUTPUT_TEXT


def test_output_without_a_results_list_is_bad_output_for_a_change(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out({"wrote": True}))

    change = ClientSetupRunner(store, run=run).run_change(["codex"], "apply")

    assert change.ok is False and change.kind == KIND_BAD_OUTPUT


# ----------------------------------------------------------- preview and change


def test_preview_apply_never_passes_yes(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings
    config = tmp_path / "config.toml"
    run = FakeRun(lambda argv: out(results(entry("codex", config, "would_write", added=["meridian-docs"]))))

    preview = ClientSetupRunner(store, run=run).preview(["codex"], "apply")

    assert run.argvs == [[settings.core_python, "-m", CORE_MODULE, "apply", "--client", "codex"]]
    assert run.calls[0][1] == Path(settings.core_checkout)
    assert preview.ok is True
    assert preview.changes[0].added == ("meridian-docs",)


def test_preview_remove_never_passes_yes(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    run = FakeRun(lambda argv: out(results(entry("claude-desktop", config, "would_remove", removed=["a"]))))

    preview = ClientSetupRunner(store, run=run).preview(["claude-desktop"], "remove")

    assert "--yes" not in run.argvs[0]
    assert run.argvs[0][3:] == ["remove", "--client", "claude-desktop"]
    assert preview.changes[0].removed == ("a",)


def test_run_change_apply_passes_yes_with_the_right_arguments(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings
    config = tmp_path / "claude_desktop_config.json"
    backup = str(tmp_path / "claude_desktop_config.json.meridian-backup-20261019-120000")
    run = FakeRun(
        lambda argv: out(
            results(entry("claude-desktop", config, "written", added=["meridian-docs"], backup=backup, restart=True), wrote=True)
        )
    )

    change = ClientSetupRunner(store, run=run).run_change(["claude-desktop"], "apply")

    assert run.argvs == [[settings.core_python, "-m", CORE_MODULE, "apply", "--client", "claude-desktop", "--yes"]]
    assert change.ok is True
    assert change.changes[0].backup == backup
    assert change.changes[0].restart_required is True


def test_run_change_remove_passes_yes(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    run = FakeRun(lambda argv: out(results(entry("codex", config, "removed", removed=["meridian-docs"], backup=None), wrote=True)))

    change = ClientSetupRunner(store, run=run).run_change(["codex"], "remove")

    assert run.argvs[0][3:] == ["remove", "--client", "codex", "--yes"]
    assert change.changes[0].backup is None


def test_a_client_without_a_core_command_is_refused_without_a_process(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results()))
    runner = ClientSetupRunner(store, run=run)

    preview = runner.preview(["claude-code"], "apply")
    change = runner.run_change(["claude-code"], "apply")

    assert preview.kind == KIND_NOT_COVERED and change.kind == KIND_NOT_COVERED
    assert run.calls == []


def test_an_unknown_mode_is_refused_without_a_process(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results()))

    result = ClientSetupRunner(store, run=run).preview(["codex"], "install")

    assert result.ok is False and result.message == BAD_MODE_TEXT
    assert run.calls == []


def test_a_change_answer_for_another_client_is_bad_output(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results(entry("claude-desktop", tmp_path / "x", "would_write"))))

    result = ClientSetupRunner(store, run=run).preview(["codex"], "apply")

    assert result.ok is False and result.kind == KIND_BAD_OUTPUT


def test_a_preview_answer_that_is_a_real_write_is_bad_output(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results(entry("codex", tmp_path / "x", "written"))))

    result = ClientSetupRunner(store, run=run).preview(["codex"], "apply")

    assert result.ok is False and result.kind == KIND_BAD_OUTPUT


def test_describe_command_shows_the_yes_flag_and_the_checkout(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings

    text = ClientSetupRunner(store, run=FakeRun(lambda argv: out(results()))).describe_command(["codex"], "apply")

    assert CORE_MODULE in text
    assert "--client codex" in text
    assert text.endswith("--yes  (run in " + str(Path(settings.core_checkout)) + ")")


# ----------------------------------------------------------- the clients the owner ticks


def plan_argv(python: str, clients: str) -> list[str]:
    return [python, "-m", CORE_MODULE, "plan", "--client", clients]


def apply_argv(python: str, clients: str, *, yes: bool) -> list[str]:
    argv = [python, "-m", CORE_MODULE, "apply", "--client", clients]
    return argv + ["--yes"] if yes else argv


def test_the_desktop_clients_are_four_in_display_order_and_match_the_saved_names():
    assert [client for client, _label in CLIENTS] == ["claude-desktop", "codex", "cursor", "claude-code"]
    assert [label for _client, label in CLIENTS] == ["Claude Desktop", "Codex", "Cursor", "Claude Code"]
    assert {client for client, _label in CLIENTS} == set(CLIENT_CHOICE_NAMES)


def test_a_cursor_row_is_covered_and_is_sent_to_the_core_when_ticked():
    row = row_for_list(unchecked_rows(["cursor"]), "cursor")

    assert row.label == "Cursor"
    assert row.state == NOT_CHECKED
    assert row.covered is True
    assert covered_ticked(["cursor"]) == ("cursor",)


def test_a_ticked_cursor_is_planned_with_the_other_ticked_clients_in_display_order(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    python = store.load().settings.core_python
    cursor_config = tmp_path / "mcp.json"
    cursor_config.write_text("{}", encoding="utf-8")
    run = FakeRun(lambda argv: out(results(entry("cursor", cursor_config, "unchanged"))))

    result = ClientSetupRunner(store, run=run).check(["cursor", "codex"])

    assert run.argvs == [plan_argv(python, "codex,cursor")]
    assert row_for(result, "cursor").state == IN_PLACE
    assert row_for(result, "codex").state == NOT_CHECKED


def test_apply_with_cursor_ticked_sends_cursor_and_yes_only_on_change(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    python = store.load().settings.core_python
    cursor_config = tmp_path / "mcp.json"
    preview_payload = results(entry("cursor", cursor_config, "would_write", added=["meridian-docs"]))
    change_payload = results(entry("cursor", cursor_config, "written", added=["meridian-docs"]), wrote=True)
    run = FakeRun(lambda argv: out(change_payload if "--yes" in argv else preview_payload))
    runner = ClientSetupRunner(store, run=run)

    preview = runner.preview(["cursor"], "apply")
    change = runner.run_change(["cursor"], "apply")

    assert run.argvs == [apply_argv(python, "cursor", yes=False), apply_argv(python, "cursor", yes=True)]
    assert preview.ok is True and [item.client for item in preview.changes] == ["cursor"]
    assert change.ok is True and [item.action for item in change.changes] == ["written"]


def test_a_row_for_an_unticked_client_is_not_selected_and_says_how_to_include_it():
    rows = unchecked_rows(["codex"])

    assert row_for_list(rows, "claude-desktop").state == NOT_SELECTED
    assert row_for_list(rows, "claude-desktop").detail == NOT_SELECTED_TEXT
    assert STATE_TEXT[NOT_SELECTED] == "Not selected"
    assert row_for_list(rows, "codex").state == NOT_CHECKED


def test_ticked_clients_keep_display_order_and_drop_unknown_names():
    assert ticked_clients(["codex", "not-a-client", "claude-desktop"]) == ("claude-desktop", "codex")
    assert covered_ticked(["cursor", "codex", "claude-code"]) == ("codex", "cursor")


def test_check_with_two_ticked_clients_sends_one_comma_list(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    python = store.load().settings.core_python
    run = FakeRun(lambda argv: out(results()))

    result = ClientSetupRunner(store, run=run).check(["codex", "claude-desktop"])

    assert run.argvs == [plan_argv(python, "claude-desktop,codex")]
    assert result.message == CHECKED_TEXT


def test_check_sends_only_the_ticked_clients_and_leaves_the_others_not_selected(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    python = store.load().settings.core_python
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    run = FakeRun(lambda argv: out(results(entry("codex", config, "unchanged"))))

    result = ClientSetupRunner(store, run=run).check(["codex"])

    assert run.argvs == [plan_argv(python, "codex")]
    assert "claude-desktop" not in " ".join(run.argvs[0])
    assert row_for(result, "claude-desktop").state == NOT_SELECTED
    assert row_for(result, "codex").state == IN_PLACE


def test_check_with_nothing_ticked_sends_nothing_to_the_core(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results()))

    result = ClientSetupRunner(store, run=run).check([])

    assert run.calls == []
    assert result.message == NOTHING_TICKED_TEXT
    assert all(row.state == NOT_SELECTED for row in result.rows)


def test_a_ticked_client_the_core_does_not_cover_is_never_sent(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    python = store.load().settings.core_python
    run = FakeRun(lambda argv: out(results()))

    result = ClientSetupRunner(store, run=run).check(["claude-code", "codex"])

    assert run.argvs == [plan_argv(python, "codex")]
    claude_code = row_for(result, "claude-code")
    assert claude_code.state == NOT_COVERED
    assert claude_code.detail == NOT_COVERED_TEXT.format(label="Claude Code")


def test_only_uncovered_ticked_clients_send_nothing(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results()))
    runner = ClientSetupRunner(store, run=run)

    result = runner.check(["claude-code"])
    preview = runner.preview(["claude-code"], "apply")

    assert run.calls == []
    assert row_for(result, "claude-code").state == NOT_COVERED
    assert preview.ok is False and preview.kind == KIND_NOT_COVERED
    assert "Claude Code" in preview.message


def test_apply_with_two_ticked_clients_sends_one_comma_list_and_yes_only_on_change(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    python = store.load().settings.core_python
    config = tmp_path / "config.toml"
    preview_payload = results(
        entry("claude-desktop", tmp_path / "a.json", "would_write", added=["meridian-docs"]),
        entry("codex", config, "would_write", added=["meridian-docs"]),
    )
    change_payload = results(
        entry("claude-desktop", tmp_path / "a.json", "written", added=["meridian-docs"], restart=True),
        entry("codex", config, "written", added=["meridian-docs"], restart=True),
        wrote=True,
    )
    run = FakeRun(lambda argv: out(change_payload if "--yes" in argv else preview_payload))
    runner = ClientSetupRunner(store, run=run)

    preview = runner.preview(["codex", "claude-desktop"], "apply")
    change = runner.run_change(["codex", "claude-desktop"], "apply")

    assert run.argvs == [apply_argv(python, "claude-desktop,codex", yes=False), apply_argv(python, "claude-desktop,codex", yes=True)]
    assert [item.client for item in preview.changes] == ["claude-desktop", "codex"]
    assert change.ok is True and [item.action for item in change.changes] == ["written", "written"]


def test_apply_and_remove_with_none_ticked_send_nothing(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results()))
    runner = ClientSetupRunner(store, run=run)

    preview = runner.preview([], "apply")
    change = runner.run_change([], "remove")

    assert run.calls == []
    assert preview.ok is False and preview.kind == KIND_NOTHING_TICKED
    assert change.ok is False and change.kind == KIND_NOTHING_TICKED
    assert preview.message == NOTHING_TICKED_TEXT


def test_a_core_failure_for_one_client_is_read_and_stays_on_that_client(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    message = "claude_desktop_config.json is not valid JSON; refusing to rewrite it"
    payload = results(
        entry("claude-desktop", tmp_path / "a.json", "failed", message=message),
        entry("codex", config, "unchanged"),
    )
    # The core prints every client's result and exits 1 when one of them failed.
    run = FakeRun(lambda argv: CommandOutput(1, json.dumps(payload), ""))

    result = ClientSetupRunner(store, run=run).check(["claude-desktop", "codex"])

    assert result.message == CHECKED_TEXT
    desktop = row_for(result, "claude-desktop")
    assert desktop.state == NOT_READABLE
    assert desktop.detail == message
    assert row_for(result, "codex").state == IN_PLACE


def test_a_failed_client_in_a_change_is_read_as_a_failed_change(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    payload = results(
        entry("codex", config, "failed", message="config.toml could not be read"),
        entry("claude-desktop", tmp_path / "a.json", "written", backup=None, restart=True),
        wrote=True,
    )
    run = FakeRun(lambda argv: CommandOutput(1, json.dumps(payload), ""))

    change = ClientSetupRunner(store, run=run).run_change(["codex", "claude-desktop"], "apply")

    assert change.ok is True
    failed = next(item for item in change.changes if item.client == "codex")
    assert failed.action == "failed" and failed.message == "config.toml could not be read"


def test_describe_command_names_the_ticked_clients_as_one_list(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings

    text = ClientSetupRunner(store, run=FakeRun(lambda argv: out(results()))).describe_command(
        ["codex", "claude-desktop"], "remove"
    )

    assert "--client claude-desktop,codex" in text
    assert text.endswith("--yes  (run in " + str(Path(settings.core_checkout)) + ")")


# ----------------------------------------------------------- a client whose app is not installed


def missing_folder(client, path) -> dict:
    """The core's result for a client whose config folder does not exist, with the keys the core prints for it."""
    return entry(
        client,
        path,
        "not_installed",
        ok=True,
        message=f"{client} is not installed here: its config folder does not exist.",
        stays_off=[],
        kept={},
    )


def test_a_client_whose_folder_is_missing_is_missing_on_its_row_and_the_others_are_still_read(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    desktop_path = tmp_path / "Claude" / "claude_desktop_config.json"  # its folder does not exist
    payload = results(missing_folder("claude-desktop", desktop_path), entry("codex", config, "unchanged"))
    run = FakeRun(lambda argv: out(payload))

    result = ClientSetupRunner(store, run=run).check(["claude-desktop", "codex"])

    assert result.message == CHECKED_TEXT
    desktop = row_for(result, "claude-desktop")
    assert desktop.state == MISSING
    assert desktop.path == str(desktop_path)
    assert desktop.detail == NOT_INSTALLED_TEXT.format(label="Claude Desktop")
    assert "does not know" not in desktop.detail
    assert row_for(result, "codex").state == IN_PLACE


def test_a_preview_with_one_missing_client_is_accepted_and_the_other_client_is_previewed(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings
    desktop_path = tmp_path / "Claude" / "claude_desktop_config.json"
    config = tmp_path / "config.toml"
    payload = results(
        missing_folder("claude-desktop", desktop_path),
        entry("codex", config, "would_write", added=["meridian-docs"]),
    )
    run = FakeRun(lambda argv: out(payload))

    preview = ClientSetupRunner(store, run=run).preview(["claude-desktop", "codex"], "apply")

    assert preview.ok is True
    assert run.argvs == [[settings.core_python, "-m", CORE_MODULE, "apply", "--client", "claude-desktop,codex"]]
    assert [change.action for change in preview.changes] == ["not_installed", "would_write"]
    assert preview.changes[1].added == ("meridian-docs",)


def test_a_remove_with_one_missing_client_is_accepted_and_the_other_client_is_removed(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    desktop_path = tmp_path / "Claude" / "claude_desktop_config.json"
    config = tmp_path / "config.toml"
    payload = results(
        missing_folder("claude-desktop", desktop_path),
        entry("codex", config, "removed", removed=["meridian-docs"]),
        wrote=True,
    )
    run = FakeRun(lambda argv: out(payload))

    change = ClientSetupRunner(store, run=run).run_change(["claude-desktop", "codex"], "remove")

    assert change.ok is True
    assert run.argvs[0][3:] == ["remove", "--client", "claude-desktop,codex", "--yes"]
    assert [item.action for item in change.changes] == ["not_installed", "removed"]
    assert change.changes[1].removed == ("meridian-docs",)


def test_a_preview_where_every_ticked_client_is_missing_is_accepted_with_nothing_to_write(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    desktop_path = tmp_path / "Claude" / "claude_desktop_config.json"
    run = FakeRun(lambda argv: out(results(missing_folder("claude-desktop", desktop_path))))

    preview = ClientSetupRunner(store, run=run).preview(["claude-desktop"], "apply")

    assert preview.ok is True
    assert [change.action for change in preview.changes] == ["not_installed"]


def test_a_not_installed_answer_for_a_client_that_was_not_asked_for_is_bad_output(settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    desktop_path = tmp_path / "Claude" / "claude_desktop_config.json"
    run = FakeRun(lambda argv: out(results(missing_folder("claude-desktop", desktop_path))))

    preview = ClientSetupRunner(store, run=run).preview(["codex"], "apply")

    assert preview.ok is False and preview.kind == KIND_BAD_OUTPUT


# ----------------------------------------------------------- boundary


def test_the_runner_imports_no_qt():
    tree = ast.parse((REPO / "meridian_desktop" / "client_setup_runner.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    qt_roots = {"PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2"}
    assert [name for name in imported if name.split(".")[0] in qt_roots] == []
