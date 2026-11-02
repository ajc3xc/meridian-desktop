"""Tests for the client setup section on the Settings page (meridian_desktop/ui/client_setup_section.py), with pytest-qt.

The runner is always a fake, so no test starts the real core command. The confirmation dialog is an injected
fake that records its text and returns a chosen answer, so no test opens a real dialog or changes a file
outside pytest's temp directory. Offscreen Qt, from conftest.

Each client has a box the owner ticks. Check, Apply and Remove act only on the ticked clients. The tests that
are about one client tick only that client, so each responder answers for every client the command asked for.
"""

from __future__ import annotations

import gc
import json
import subprocess
import threading
import weakref
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QPushButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop.client_setup_runner import (
    CHECKED_TEXT,
    CORE_MODULE,
    IN_PLACE,
    MISSING,
    NOT_CHECKED,
    NOT_INSTALLED_TEXT,
    NOT_READABLE,
    NOT_SELECTED,
    NOT_SELECTED_TEXT,
    NOT_SET_CHECKOUT_TEXT,
    NOT_SET_PYTHON_TEXT,
    NOT_COVERED_TEXT,
    STATE_TEXT,
    WOULD_CHANGE,
    ClientSetupRunner,
    CommandOutput,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.client_setup_section import (
    APPLY_DIALOG_TITLE,
    CANCELLED_TEXT,
    CHECKOUT_SAVED_TEXT,
    DEFAULT_STATUS_TEXT,
    NO_TICK_STATUS_TEXT,
    PYTHON_SAVED_TEXT,
    REASON_NOT_COVERED,
    REASON_NOT_TICKED,
    REMOVE_DIALOG_TITLE,
    ClientSetupSection,
    confirmation_text,
)
from meridian_desktop.ui.settings_tab import SettingsTab

from _shared_delivery import emit, recording_deliveries, visible_state

FAKE_TOKEN = "sk_meridian_fake-section-0006"
SELECTABLE_MOUSE = Qt.TextInteractionFlag.TextSelectableByMouse
SELECTABLE_KEYBOARD = Qt.TextInteractionFlag.TextSelectableByKeyboard


# ----------------------------------------------------------- helpers


def make_core(tmp_path: Path) -> Path:
    checkout = tmp_path / "core"
    (checkout / "meridian").mkdir(parents=True)
    (checkout / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    return checkout


def make_python(tmp_path: Path) -> str:
    python = tmp_path / "venv" / "python.exe"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("not a real interpreter", encoding="utf-8")
    return str(python)


def configured_store(tmp_path: Path, settings_path: Path) -> SettingsStore:
    store = SettingsStore(settings_path)
    store.save(Settings(core_checkout=str(make_core(tmp_path)), core_python=make_python(tmp_path)))
    return store


def store_with_choices(tmp_path: Path, settings_path: Path, *clients: str) -> SettingsStore:
    """A configured store whose ticked clients are exactly the ones given."""
    store = configured_store(tmp_path, settings_path)
    store.set_client_choices(list(clients))
    return store


class FakeRun:
    """Records each command and answers from a responder. It never starts a process."""

    def __init__(self, respond) -> None:
        self._respond = respond
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, timeout):
        self.argvs.append(list(argv))
        outcome = self._respond(list(argv))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Recorder:
    """The confirmation dialog stand-in. It records each call and returns the answer it was given."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(self, parent, title, text) -> bool:
        self.calls.append((title, text))
        return self.answer


def out(payload, returncode: int = 0, stderr: str = "") -> CommandOutput:
    return CommandOutput(returncode, json.dumps(payload), stderr)


def entry(client, path, action, *, added=(), changed=(), removed=(), backup=None, restart=False, message=None) -> dict:
    item = {
        "client": client,
        "path": str(path),
        "action": action,
        "changes": {"added": list(added), "changed": list(changed), "removed": list(removed), "unchanged": []},
        "re_enabled": [],
        "backup": backup,
        "restart_required": restart,
    }
    if message is not None:
        item["message"] = message
    return item


def results(*entries, wrote: bool = False) -> dict:
    return {"results": list(entries), "wrote": wrote}


def is_plan(argv: list[str]) -> bool:
    return "plan" in argv


def clients_in(argv: list[str]) -> set[str]:
    """The clients one command asked for, from its --client comma list."""
    return set(argv[argv.index("--client") + 1].split(","))


def _section(qtbot, store, *, run=None, runner=None, confirm=None, choose_folder=None, choose_python=None):
    if runner is None:
        runner = ClientSetupRunner(store, run=run if run is not None else FakeRun(lambda argv: out(results())))
    kwargs = {"runner": runner}
    if confirm is not None:
        kwargs["confirm"] = confirm
    if choose_folder is not None:
        kwargs["choose_folder"] = choose_folder
    if choose_python is not None:
        kwargs["choose_python"] = choose_python
    section = ClientSetupSection(store, **kwargs)
    qtbot.addWidget(section)
    return section


def _label(section, name) -> QLabel:
    return section.findChild(QLabel, name)


def _text(section, name) -> str:
    return section.findChild(QLabel, name).text()


def _button(section, name) -> QPushButton:
    return section.findChild(QPushButton, name)


def _apply(section) -> QPushButton:
    return _button(section, "client-setup-apply")


def _remove(section) -> QPushButton:
    return _button(section, "client-setup-remove")


def _choice(section, client) -> QCheckBox:
    return section.findChild(QCheckBox, f"client-setup-{client}-choice")


def _tick(section, client, on: bool = True) -> None:
    _choice(section, client).setChecked(on)


def _all_text(section) -> str:
    texts = [label.text() for label in section.findChildren(QLabel)]
    texts += [button.text() for button in section.findChildren(QPushButton)]
    return "\n".join(texts)


def _selectable(widget) -> bool:
    flags = widget.textInteractionFlags()
    return bool(flags & SELECTABLE_MOUSE) and bool(flags & SELECTABLE_KEYBOARD)


def _wait_for_state(qtbot, section, client, state):
    qtbot.waitUntil(lambda: _text(section, f"client-setup-{client}-state") == STATE_TEXT[state], timeout=5000)


def _wait_for_result(qtbot, section, fragment):
    qtbot.waitUntil(lambda: fragment in _text(section, "client-setup-result"), timeout=5000)


def _existing(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("{}", encoding="utf-8")
    return path


# ----------------------------------------------------------- not set


def test_unset_core_says_so_and_no_command_is_offered(qtbot, settings_path):
    run = FakeRun(lambda argv: out(results()))
    section = _section(qtbot, SettingsStore(settings_path), run=run)

    assert _text(section, "client-setup-status") == NOT_SET_CHECKOUT_TEXT
    assert _apply(section).isEnabled() is False
    assert _remove(section).isEnabled() is False
    assert run.argvs == [], "no process may start while the core is not set"


def test_unset_python_names_the_python_setting(qtbot, settings_path, tmp_path):
    SettingsStore(settings_path).save(Settings(core_checkout=str(make_core(tmp_path))))
    section = _section(qtbot, SettingsStore(settings_path))

    assert _text(section, "client-setup-status") == NOT_SET_PYTHON_TEXT


def test_check_while_unset_shows_the_plain_refusal(qtbot, settings_path):
    run = FakeRun(lambda argv: out(results()))
    section = _section(qtbot, SettingsStore(settings_path), run=run)

    _button(section, "client-setup-check").click()

    qtbot.waitUntil(lambda: _text(section, "client-setup-status") == NOT_SET_CHECKOUT_TEXT, timeout=5000)
    assert run.argvs == []


# ----------------------------------------------------------- saving the core paths


def test_refused_checkout_shows_the_store_message_and_writes_nothing(qtbot, settings_path, tmp_path):
    gone = tmp_path / "gone"
    section = _section(qtbot, SettingsStore(settings_path))
    section.findChild(QLineEdit, "client-setup-checkout").setText(str(gone))

    _button(section, "client-setup-save-checkout").click()

    message = _label(section, "client-setup-settings-message")
    assert message.text().startswith("Not saved. ")
    assert str(gone) in message.text()
    assert not message.isHidden()
    assert not settings_path.exists(), "a refused path writes nothing"


def test_refused_python_shows_plain_words(qtbot, settings_path, tmp_path):
    section = _section(qtbot, SettingsStore(settings_path))
    section.findChild(QLineEdit, "client-setup-python").setText(str(tmp_path / "nope" / "python.exe"))

    _button(section, "client-setup-save-python").click()

    assert _text(section, "client-setup-settings-message").startswith("Not saved. The Python program does not exist")
    assert not settings_path.exists()


def test_saving_both_paths_makes_the_section_ready(qtbot, settings_path, tmp_path):
    section = _section(qtbot, SettingsStore(settings_path))
    section.findChild(QLineEdit, "client-setup-checkout").setText(str(make_core(tmp_path)))
    _button(section, "client-setup-save-checkout").click()
    assert _text(section, "client-setup-settings-message") == CHECKOUT_SAVED_TEXT
    assert _text(section, "client-setup-status") == NOT_SET_PYTHON_TEXT

    section.findChild(QLineEdit, "client-setup-python").setText(make_python(tmp_path))
    _button(section, "client-setup-save-python").click()

    assert _text(section, "client-setup-settings-message") == PYTHON_SAVED_TEXT
    assert _text(section, "client-setup-status") == DEFAULT_STATUS_TEXT
    saved = SettingsStore(settings_path).load().settings
    assert saved.core_checkout and saved.core_python
    assert _apply(section).isEnabled() is True
    assert _remove(section).isEnabled() is True


def test_saving_one_path_keeps_what_was_typed_in_the_other_box(qtbot, settings_path, tmp_path):
    section = _section(qtbot, SettingsStore(settings_path))
    section.findChild(QLineEdit, "client-setup-checkout").setText("typed-but-not-saved")
    section.findChild(QLineEdit, "client-setup-python").setText(make_python(tmp_path))

    _button(section, "client-setup-save-python").click()

    assert _text(section, "client-setup-settings-message") == PYTHON_SAVED_TEXT
    assert section.findChild(QLineEdit, "client-setup-checkout").text() == "typed-but-not-saved"


def test_browse_fills_the_box_but_does_not_save(qtbot, settings_path, tmp_path):
    chosen = make_core(tmp_path)
    section = _section(qtbot, SettingsStore(settings_path), choose_folder=lambda parent, start: str(chosen))

    _button(section, "client-setup-browse-checkout").click()

    assert section.findChild(QLineEdit, "client-setup-checkout").text() == str(chosen)
    assert not settings_path.exists(), "Browse does not save; Save does"


def test_a_cancelled_browse_changes_nothing(qtbot, settings_path):
    section = _section(qtbot, SettingsStore(settings_path), choose_python=lambda parent, start: "")

    section.findChild(QLineEdit, "client-setup-python").setText("keep-this")
    _button(section, "client-setup-browse-python").click()

    assert section.findChild(QLineEdit, "client-setup-python").text() == "keep-this"


# ----------------------------------------------------------- the boxes: one per client, saved at once


def test_there_is_one_box_per_client_in_order_with_the_default_two_ticked(qtbot, settings_path, tmp_path):
    section = _section(qtbot, configured_store(tmp_path, settings_path))

    assert [_choice(section, client).text() for client in ("claude-desktop", "codex", "cursor", "claude-code")] == [
        "Claude Desktop",
        "Codex",
        "Cursor",
        "Claude Code",
    ]
    assert _choice(section, "claude-desktop").isChecked() is True
    assert _choice(section, "codex").isChecked() is True
    assert _choice(section, "cursor").isChecked() is False
    assert _choice(section, "claude-code").isChecked() is False
    assert _text(section, "client-setup-cursor-state") == STATE_TEXT[NOT_SELECTED]
    assert _text(section, "client-setup-claude-code-state") == STATE_TEXT[NOT_SELECTED]


def test_a_box_saves_at_once_and_the_choice_reads_back_in_a_new_section(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    section = _section(qtbot, store)

    _tick(section, "cursor")
    _tick(section, "codex", False)

    assert SettingsStore(settings_path).load().settings.client_choices == ("claude-desktop", "cursor")
    again = _section(qtbot, SettingsStore(settings_path))
    assert _choice(again, "cursor").isChecked() is True
    assert _choice(again, "codex").isChecked() is False


def test_a_ticked_cursor_is_sent_to_the_core_with_the_other_ticked_clients(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(
        lambda argv: out(
            results(
                entry("claude-desktop", _existing(tmp_path, "desktop.json"), "unchanged"),
                entry("codex", _existing(tmp_path, "config.toml"), "unchanged"),
                entry("cursor", _existing(tmp_path, "mcp.json"), "unchanged"),
            )
        )
    )
    section = _section(qtbot, store, run=run)

    _tick(section, "cursor")

    assert _text(section, "client-setup-cursor-state") == STATE_TEXT[NOT_CHECKED]
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "cursor", IN_PLACE)
    python = store.load().settings.core_python
    assert run.argvs == [[python, "-m", CORE_MODULE, "plan", "--client", "claude-desktop,codex,cursor"]]


def test_an_unticked_client_shows_not_selected_and_is_not_sent(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results(entry("codex", _existing(tmp_path, "config.toml"), "unchanged"))))
    section = _section(qtbot, store, run=run)

    _tick(section, "claude-desktop", False)
    _button(section, "client-setup-check").click()

    _wait_for_state(qtbot, section, "codex", IN_PLACE)
    assert _text(section, "client-setup-claude-desktop-state") == STATE_TEXT[NOT_SELECTED]
    assert _text(section, "client-setup-claude-desktop-detail") == NOT_SELECTED_TEXT
    assert all("claude-desktop" not in argv[-1] for argv in run.argvs)
    assert run.argvs == [[store.load().settings.core_python, "-m", CORE_MODULE, "plan", "--client", "codex"]]


def test_check_sends_the_ticked_clients_as_one_comma_list(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    python = store.load().settings.core_python
    run = FakeRun(
        lambda argv: out(
            results(
                entry("claude-desktop", _existing(tmp_path, "desktop.json"), "unchanged"),
                entry("codex", _existing(tmp_path, "config.toml"), "unchanged"),
            )
        )
    )
    section = _section(qtbot, store, run=run)

    _button(section, "client-setup-check").click()

    _wait_for_state(qtbot, section, "codex", IN_PLACE)
    assert run.argvs == [[python, "-m", CORE_MODULE, "plan", "--client", "claude-desktop,codex"]]


# ----------------------------------------------------------- check renders each state


SCENARIOS = {
    "would change and missing": (
        lambda tmp_path: (
            lambda argv: out(
                results(
                    entry("claude-desktop", _existing(tmp_path, "desktop.json"), "would_write", added=["meridian-docs"]),
                    entry("codex", tmp_path / "codex" / "config.toml", "would_write", added=["meridian-outputs"]),
                )
            )
            if is_plan(argv)
            else out(results())
        ),
        {"claude-desktop": WOULD_CHANGE, "codex": MISSING},
    ),
    "in place": (
        lambda tmp_path: (
            lambda argv: out(
                results(
                    entry("claude-desktop", _existing(tmp_path, "desktop.json"), "unchanged"),
                    entry("codex", _existing(tmp_path, "config.toml"), "unchanged"),
                )
            )
            if is_plan(argv)
            else out(results())
        ),
        {"claude-desktop": IN_PLACE, "codex": IN_PLACE},
    ),
    "not readable": (
        lambda tmp_path: (
            lambda argv: (
                CommandOutput(1, "", json.dumps({"error": "desktop.json is not valid JSON"}))
                if is_plan(argv) and "claude-desktop" in clients_in(argv)
                else out(results(entry("codex", _existing(tmp_path, "config.toml"), "unchanged")))
            )
        ),
        {"claude-desktop": NOT_READABLE, "codex": IN_PLACE},
    ),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_check_renders_each_state(qtbot, settings_path, tmp_path, scenario):
    responder_factory, expected = SCENARIOS[scenario]
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(responder_factory(tmp_path))
    section = _section(qtbot, store, run=run)

    _button(section, "client-setup-check").click()

    for client, state in expected.items():
        _wait_for_state(qtbot, section, client, state)
    assert _text(section, "client-setup-claude-code-state") == STATE_TEXT[NOT_SELECTED]


def test_rows_show_the_file_and_the_names_of_the_change(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    desktop_file = _existing(tmp_path, "desktop.json")
    run = FakeRun(
        lambda argv: out(
            results(entry("claude-desktop", desktop_file, "would_write", added=["meridian-docs"], changed=["meridian-search"]))
        )
    )
    section = _section(qtbot, store, run=run)

    _button(section, "client-setup-check").click()

    _wait_for_state(qtbot, section, "claude-desktop", WOULD_CHANGE)
    assert _text(section, "client-setup-claude-desktop-path") == f"File: {desktop_file}"
    assert _text(section, "client-setup-claude-desktop-detail") == "Adds: meridian-docs. Changes: meridian-search."
    assert _apply(section).isEnabled() is True


def test_apply_stays_on_for_ticked_clients_already_in_place_and_says_so_in_the_preview(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(
        lambda argv: out(
            results(
                entry("claude-desktop", _existing(tmp_path, "desktop.json"), "unchanged"),
                entry("codex", _existing(tmp_path, "config.toml"), "unchanged"),
            )
        )
    )
    section = _section(qtbot, store, run=run)

    _button(section, "client-setup-check").click()

    _wait_for_state(qtbot, section, "codex", IN_PLACE)
    assert _apply(section).isEnabled() is True
    assert _remove(section).isEnabled() is True


def test_only_uncovered_ticked_clients_turn_apply_and_remove_off_and_say_why(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    section = _section(qtbot, store)

    _tick(section, "claude-desktop", False)
    _tick(section, "codex", False)
    _tick(section, "claude-code")

    assert _apply(section).isEnabled() is False
    assert _remove(section).isEnabled() is False
    assert _apply(section).toolTip() == REASON_NOT_COVERED
    assert _text(section, "client-setup-claude-code-detail") == NOT_COVERED_TEXT.format(label="Claude Code")


def test_with_no_client_ticked_apply_and_remove_are_off_and_nothing_is_sent(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: out(results()))
    section = _section(qtbot, store, run=run)

    _tick(section, "claude-desktop", False)
    _tick(section, "codex", False)

    assert _apply(section).isEnabled() is False
    assert _remove(section).isEnabled() is False
    assert _apply(section).toolTip() == REASON_NOT_TICKED
    assert _text(section, "client-setup-status") == NO_TICK_STATUS_TEXT
    _apply(section).click()
    _remove(section).click()
    assert run.argvs == [], "Apply and Remove with none ticked send nothing to the core"


def test_a_failed_check_says_why_and_leaves_rows_not_checked(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    run = FakeRun(lambda argv: subprocess.TimeoutExpired(argv, 60))
    section = _section(qtbot, store, run=run)

    _button(section, "client-setup-check").click()

    qtbot.waitUntil(lambda: "did not finish within 60 seconds" in _text(section, "client-setup-status"), timeout=5000)
    _wait_for_state(qtbot, section, "codex", NOT_CHECKED)


def test_a_core_failure_for_one_client_shows_on_that_row_only(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    message = "desktop.json is not valid JSON; refusing to rewrite it"
    payload = results(
        entry("claude-desktop", tmp_path / "desktop.json", "failed", message=message),
        entry("codex", _existing(tmp_path, "config.toml"), "unchanged"),
    )
    run = FakeRun(lambda argv: CommandOutput(1, json.dumps(payload), ""))
    section = _section(qtbot, store, run=run)

    _button(section, "client-setup-check").click()

    _wait_for_state(qtbot, section, "claude-desktop", NOT_READABLE)
    assert _text(section, "client-setup-claude-desktop-detail") == message
    _wait_for_state(qtbot, section, "codex", IN_PLACE)
    assert _text(section, "client-setup-codex-detail") == ""
    assert message not in _text(section, "client-setup-codex-detail")


# ----------------------------------------------------------- apply: the dialog and the Yes


def test_cancelling_the_dialog_runs_nothing(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    config = _existing(tmp_path, "desktop.json")
    before = config.read_bytes()

    run = FakeRun(lambda argv: out(results(entry("claude-desktop", config, "would_write", added=["meridian-docs"]))))
    confirm = Recorder(answer=False)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "claude-desktop", WOULD_CHANGE)

    _apply(section).click()

    _wait_for_result(qtbot, section, CANCELLED_TEXT)
    assert len(confirm.calls) == 1, "the dialog is shown once, after the dry run"
    assert not any("--yes" in argv for argv in run.argvs), "a cancelled dialog must never send --yes"
    assert any(argv[3:] == ["apply", "--client", "claude-desktop"] for argv in run.argvs), "the dry run did happen"
    assert config.read_bytes() == before


def test_yes_runs_apply_with_the_right_arguments_and_shows_the_backup(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    settings = store.load().settings
    config = _existing(tmp_path, "desktop.json")
    backup = str(tmp_path / "desktop.json.meridian-backup-20261019-120000")
    state = {"written": False}

    def respond(argv):
        if "--yes" in argv:
            state["written"] = True
            return out(results(entry("claude-desktop", config, "written", added=["meridian-docs"], backup=backup, restart=True), wrote=True))
        if is_plan(argv) and state["written"]:
            return out(results(entry("claude-desktop", config, "unchanged")))
        return out(results(entry("claude-desktop", config, "would_write", added=["meridian-docs"])))

    run = FakeRun(respond)
    confirm = Recorder(answer=True)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "claude-desktop", WOULD_CHANGE)

    _apply(section).click()

    _wait_for_result(qtbot, section, "Written:")
    assert [argv for argv in run.argvs if "--yes" in argv] == [
        [settings.core_python, "-m", CORE_MODULE, "apply", "--client", "claude-desktop", "--yes"]
    ]
    result = _text(section, "client-setup-result")
    assert f"Backup saved at: {backup}" in result
    assert "Restart Claude Desktop to load the change." in result
    assert confirm.calls[0][0] == APPLY_DIALOG_TITLE
    _wait_for_state(qtbot, section, "claude-desktop", IN_PLACE)


def test_apply_writes_every_ticked_client_behind_one_confirmation_that_names_each(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings
    desktop_file = tmp_path / "desktop.json"
    codex_file = tmp_path / "config.toml"
    state = {"written": False}

    def respond(argv):
        if "--yes" in argv:
            state["written"] = True
            return out(
                results(
                    entry("claude-desktop", desktop_file, "written", added=["meridian-docs"], backup=None, restart=True),
                    entry("codex", codex_file, "written", added=["meridian-docs"], backup=None, restart=True),
                    wrote=True,
                )
            )
        if is_plan(argv) and state["written"]:
            return out(
                results(
                    entry("claude-desktop", desktop_file, "unchanged"),
                    entry("codex", codex_file, "unchanged"),
                )
            )
        return out(
            results(
                entry("claude-desktop", desktop_file, "would_write", added=["meridian-docs"]),
                entry("codex", codex_file, "would_write", added=["meridian-docs"]),
            )
        )

    run = FakeRun(respond)
    confirm = Recorder(answer=True)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "codex", MISSING)

    _apply(section).click()

    _wait_for_result(qtbot, section, "Written:")
    assert len(confirm.calls) == 1, "one confirmation covers every ticked client that will be written"
    title, text = confirm.calls[0]
    assert title == APPLY_DIALOG_TITLE
    assert "Apply Meridian's entries to Claude Desktop and Codex?" in text
    assert f"File: {desktop_file}" in text and f"File: {codex_file}" in text
    assert f"-m {CORE_MODULE} apply --client claude-desktop,codex --yes" in text
    assert [argv for argv in run.argvs if "--yes" in argv] == [
        [settings.core_python, "-m", CORE_MODULE, "apply", "--client", "claude-desktop,codex", "--yes"]
    ]
    assert _text(section, "client-setup-result").count("Written:") == 2


MISSING_FOLDER_MESSAGE = "is not installed here: its config folder does not exist."


def test_apply_with_one_missing_client_confirms_only_the_installed_one_and_the_missing_row_says_so(
    qtbot, settings_path, tmp_path
):
    store = configured_store(tmp_path, settings_path)
    settings = store.load().settings
    desktop_path = tmp_path / "Claude" / "claude_desktop_config.json"  # the Claude Desktop folder is not here
    codex_file = _existing(tmp_path, "config.toml")
    state = {"written": False}

    def respond(argv):
        missing = entry("claude-desktop", desktop_path, "not_installed", message=MISSING_FOLDER_MESSAGE)
        if "--yes" in argv:
            state["written"] = True
            return out(results(entry("codex", codex_file, "written", added=["meridian-docs"], restart=True), wrote=True))
        if state["written"]:
            return out(results(missing, entry("codex", codex_file, "unchanged")))
        return out(results(missing, entry("codex", codex_file, "would_write", added=["meridian-docs"])))

    run = FakeRun(respond)
    confirm = Recorder(answer=True)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "codex", WOULD_CHANGE)
    _wait_for_state(qtbot, section, "claude-desktop", MISSING)

    _apply(section).click()

    _wait_for_result(qtbot, section, "Written:")
    assert len(confirm.calls) == 1, "only the client that will be written is confirmed"
    title, text = confirm.calls[0]
    assert title == APPLY_DIALOG_TITLE
    assert "Apply Meridian's entries to Codex?" in text
    assert "Claude Desktop" not in text
    assert f"-m {CORE_MODULE} apply --client codex --yes" in text
    assert [argv for argv in run.argvs if "--yes" in argv] == [
        [settings.core_python, "-m", CORE_MODULE, "apply", "--client", "codex", "--yes"]
    ]
    assert _text(section, "client-setup-result").count("Written:") == 1
    qtbot.waitUntil(lambda: _text(section, "client-setup-claude-desktop-state") == STATE_TEXT[MISSING], timeout=5000)
    assert NOT_INSTALLED_TEXT.format(label="Claude Desktop") in _text(section, "client-setup-claude-desktop-detail")


def test_apply_when_the_only_ticked_client_is_missing_asks_nothing_and_says_it_is_not_set_up(
    qtbot, settings_path, tmp_path
):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    desktop_path = tmp_path / "Claude" / "claude_desktop_config.json"
    run = FakeRun(
        lambda argv: out(results(entry("claude-desktop", desktop_path, "not_installed", message=MISSING_FOLDER_MESSAGE)))
    )
    confirm = Recorder(answer=True)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "claude-desktop", MISSING)

    _apply(section).click()

    _wait_for_result(qtbot, section, "is not set up on this computer")
    assert confirm.calls == [], "nothing is written, so there is nothing to confirm"
    assert not any("--yes" in argv for argv in run.argvs)
    assert _text(section, "client-setup-result") == NOT_INSTALLED_TEXT.format(label="Claude Desktop")


def test_the_dialog_names_the_exact_file_change_and_command(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    config = _existing(tmp_path, "desktop.json")
    run = FakeRun(
        lambda argv: out(results(entry("claude-desktop", config, "would_write", added=["meridian-docs"], changed=["meridian-search"])))
    )
    confirm = Recorder(answer=False)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "claude-desktop", WOULD_CHANGE)

    _apply(section).click()

    _wait_for_result(qtbot, section, CANCELLED_TEXT)
    title, text = confirm.calls[0]
    assert title == APPLY_DIALOG_TITLE
    assert f"File: {config}" in text
    assert "Adds: meridian-docs." in text and "Changes: meridian-search." in text
    assert f"-m {CORE_MODULE} apply --client claude-desktop --yes" in text
    assert "backup" in text.lower()


def test_the_confirmation_text_names_each_client_and_its_own_file(tmp_path):
    from meridian_desktop.client_setup_runner import ClientChange

    entries = [
        ("Codex", ClientChange(client="codex", path=str(tmp_path / "config.toml"), action="would_write", added=("a",))),
        ("Cursor", ClientChange(client="cursor", path=str(tmp_path / "mcp.json"), action="would_write", removed=("b",))),
    ]
    text = confirmation_text("remove", entries, "the command")

    assert text.splitlines()[0] == "Remove Meridian's entries from Codex and Cursor?"
    assert f"File: {tmp_path / 'config.toml'}" in text and f"File: {tmp_path / 'mcp.json'}" in text
    assert "Removes: b." in text and "Adds: a." in text
    assert text.endswith("Command: the command")


def test_a_new_file_says_no_backup_was_made(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "codex")
    config = tmp_path / "fresh" / "config.toml"
    state = {"written": False}

    def respond(argv):
        if "--yes" in argv:
            state["written"] = True
            return out(results(entry("codex", config, "written", added=["meridian-docs"], backup=None), wrote=True))
        return out(results(entry("codex", config, "would_write", added=["meridian-docs"])))

    section = _section(qtbot, store, run=FakeRun(respond), confirm=Recorder(answer=True))
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "codex", MISSING)

    _apply(section).click()

    _wait_for_result(qtbot, section, "Written:")
    assert "No backup was made: the file did not exist before this change." in _text(section, "client-setup-result")


def test_remove_uses_a_dry_run_then_remove_with_yes(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    config = _existing(tmp_path, "desktop.json")
    state = {"removed": False}

    def respond(argv):
        if "remove" in argv and "--yes" in argv:
            state["removed"] = True
            return out(results(entry("claude-desktop", config, "removed", removed=["meridian-docs"], backup="b.bak", restart=True), wrote=True))
        if is_plan(argv):
            return out(results(entry("claude-desktop", config, "unchanged")))
        return out(results(entry("claude-desktop", config, "would_remove", removed=["meridian-docs"])))

    run = FakeRun(respond)
    confirm = Recorder(answer=True)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "claude-desktop", IN_PLACE)

    _remove(section).click()

    _wait_for_result(qtbot, section, "Removed Meridian's entries")
    assert confirm.calls[0][0] == REMOVE_DIALOG_TITLE
    assert "Removes: meridian-docs." in confirm.calls[0][1]
    assert [argv[3:] for argv in run.argvs if "--yes" in argv] == [
        ["remove", "--client", "claude-desktop", "--yes"]
    ]
    assert "Backup saved at: b.bak" in _text(section, "client-setup-result")


def test_a_refused_change_shows_the_core_message(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    config = _existing(tmp_path, "desktop.json")

    def respond(argv):
        if "--yes" in argv:
            return CommandOutput(1, "", json.dumps({"error": "uvx was not found on PATH or in ~/.local/bin"}))
        return out(results(entry("claude-desktop", config, "would_write", added=["meridian-docs"])))

    section = _section(qtbot, store, run=FakeRun(respond), confirm=Recorder(answer=True))
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "claude-desktop", WOULD_CHANGE)

    _apply(section).click()

    _wait_for_result(qtbot, section, "uvx was not found")


def test_remove_on_an_in_place_client_with_nothing_to_remove_asks_nothing(qtbot, settings_path, tmp_path):
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    config = _existing(tmp_path, "desktop.json")
    confirm = Recorder(answer=True)

    def respond(argv):
        return out(results(entry("claude-desktop", config, "unchanged")))

    run = FakeRun(respond)
    section = _section(qtbot, store, run=run, confirm=confirm)
    _button(section, "client-setup-check").click()
    _wait_for_state(qtbot, section, "claude-desktop", IN_PLACE)
    run_before = len(run.argvs)

    _remove(section).click()

    _wait_for_result(qtbot, section, "No Meridian entries are in")
    assert confirm.calls == [], "nothing to remove means no question"
    assert not any("--yes" in argv for argv in run.argvs[run_before:])


# ----------------------------------------------------------- busy and robustness


def test_buttons_are_off_while_a_command_runs(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    gate = threading.Event()

    def respond(argv):
        gate.wait(5)
        return out(results())

    section = _section(qtbot, store, run=FakeRun(respond))

    _button(section, "client-setup-check").click()

    assert _button(section, "client-setup-check").isEnabled() is False
    assert _apply(section).isEnabled() is False
    assert _choice(section, "codex").isEnabled() is False
    gate.set()
    qtbot.waitUntil(lambda: _button(section, "client-setup-check").isEnabled(), timeout=5000)


def test_an_unexpected_error_names_only_its_class(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)

    class Broken:
        def check(self, clients=None):
            raise RuntimeError(f"leaked {FAKE_TOKEN}")

        def describe_command(self, clients, mode):
            return ""

    section = _section(qtbot, store, runner=Broken())

    _button(section, "client-setup-check").click()

    qtbot.waitUntil(lambda: "RuntimeError" in _text(section, "client-setup-status"), timeout=5000)
    assert FAKE_TOKEN not in _all_text(section)


# ----------------------------------------------------------- page wiring, wording, selectable text


def test_the_section_is_wired_into_the_settings_tab(qtbot, settings_path):
    tab = SettingsTab(
        SettingsStore(settings_path),
        secrets=KeyringSecretStore(backend=MemoryKeyring()),
        list_hosted=lambda url, token: [],
    )
    qtbot.addWidget(tab)

    section = tab.findChild(ClientSetupSection, "client-setup-section")

    assert section is not None
    # The section sits in the Client setup group, which sits on the tab, so it is inside the tab
    assert tab.isAncestorOf(section)
    assert tab.findChild(QWidget, "settings-group-client-setup") is not None


def test_every_label_in_the_section_is_selectable(qtbot, settings_path, tmp_path):
    section = _section(qtbot, SettingsStore(settings_path))
    labels = section.findChildren(QLabel)

    assert len(labels) >= 10, "the walk reaches the real labels"
    missing = [label.objectName() or label.text() for label in labels if not _selectable(label)]
    assert missing == []


def test_no_not_built_yet_wording_in_the_section(qtbot, settings_path, tmp_path):
    section = _section(qtbot, configured_store(tmp_path, settings_path))
    combined = _all_text(section).lower()

    assert "not built" not in combined
    assert "wave" not in combined


def test_a_plain_status_and_a_refusal_never_show_the_token(qtbot, settings_path, tmp_path):
    store = configured_store(tmp_path, settings_path)
    section = _section(qtbot, store, run=FakeRun(lambda argv: out(results())))

    _button(section, "client-setup-check").click()
    qtbot.waitUntil(lambda: _text(section, "client-setup-status") == CHECKED_TEXT, timeout=5000)

    assert FAKE_TOKEN not in _all_text(section)


def test_a_dry_run_still_running_when_the_section_is_deleted_does_not_keep_the_section(
    qtbot, settings_path, tmp_path, monkeypatch
):
    """A section deleted while an Apply dry run still runs is freed on the GUI thread, because the dry run holds no section.

    The dry run used to hold the section through its runner lambda. When the pool ended the dry run on a worker, the
    worker dropped the last reference, so the section could be freed off the GUI thread. The collector is off here,
    so only reference counting can free the section. Nothing reaches the section after it is gone.
    """
    entered = threading.Event()
    gate = threading.Event()
    desktop_file = _existing(tmp_path, "desktop.json")

    def respond(argv):
        if is_plan(argv):
            return out(results(entry("claude-desktop", desktop_file, "would_write", added=["meridian-docs"])))
        entered.set()  # the Apply dry run is held at the gate
        gate.wait(5)
        return out(results())

    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    shown = []
    monkeypatch.setattr(ClientSetupSection, "_after_preview", lambda self, pending, result: shown.append(result))

    gc.disable()
    try:
        container = QWidget()
        runner = ClientSetupRunner(store, run=FakeRun(respond))
        section = ClientSetupSection(store, parent=container, runner=runner)
        _button(section, "client-setup-check").click()
        _wait_for_state(qtbot, section, "claude-desktop", WOULD_CHANGE)
        _apply(section).click()  # the dry run starts and is held at the gate
        assert entered.wait(5)
        ref = weakref.ref(section)
        shiboken6.delete(container)  # the section's widgets are deleted on the GUI thread while the dry run still runs
        del section
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert shown == []
    finally:
        gate.set()
        gc.enable()


# ----------------------------------------------------------- a result for another owner, or a superseded check


def test_a_result_from_another_owner_or_from_a_superseded_check_is_dropped(qtbot, settings_path, tmp_path):
    """A sibling section's answer, and an answer from a check that this section's newer check superseded, show nothing.

    Two sections share one saved setup, and each runs its own commands. The section's first check emits its Delivered.
    The sibling's first check then emits one at the same generation, and it reaches the section too, so the section drops
    it by owner alone. The section checks again, and its first Delivered is replayed: it carries the section's own owner
    with a superseded generation, so it is dropped by generation alone. The section's second result is what the user sees
    throughout, and it is the control that the section's own current Delivered is accepted.
    """
    store = store_with_choices(tmp_path, settings_path, "claude-desktop")
    responder = {"current": SCENARIOS["would change and missing"][0](tmp_path)}
    section = _section(qtbot, store, run=FakeRun(lambda argv: responder["current"](argv)))
    sibling = _section(qtbot, store, run=FakeRun(SCENARIOS["in place"][0](tmp_path)))
    with recording_deliveries() as emitted:
        _button(section, "client-setup-check").click()
        _wait_for_state(qtbot, section, "claude-desktop", WOULD_CHANGE)
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(section)
        _button(sibling, "client-setup-check").click()
        _wait_for_state(qtbot, sibling, "claude-desktop", IN_PLACE)
        assert len(emitted) == 2
        assert visible_state(section) == shown_first
        responder["current"] = SCENARIOS["in place"][0](tmp_path)
        _button(section, "client-setup-check").click()
        _wait_for_state(qtbot, section, "claude-desktop", IN_PLACE)
        shown_second = visible_state(section)
        assert shown_second != shown_first
        emit(own_first)  # this section's answer from the first check, which the second check superseded
        qtbot.wait(20)
    assert visible_state(section) == shown_second
