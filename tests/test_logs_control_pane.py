"""The control log pane of the Logs view (Settings, Diagnostics): its last lines, its redaction, and what it says when the log is
missing, empty, or cannot be read.

The control log gets one line for each Start or Stop of the local server or the tunnel (runner_controls). Every read here is
a fake or a file in a temp folder, so no test reads the real %LOCALAPPDATA%. The secret values are fakes. Offscreen Qt, from
conftest.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop.log_tail import (
    DEFAULT_MAX_BYTES,
    NO_LOG_YET_TEXT,
    STATUS_NO_LOG_YET,
    STATUS_OK,
    STATUS_UNREADABLE,
    LogTail,
)
from meridian_desktop.runner_controls import LOG_NAME, default_log_path, read_control_log_tail
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.logs_page import (
    CONTROL_EMPTY_TEXT,
    CONTROL_MISSING_TEXT,
    CONTROL_UNREADABLE_TEXT,
    LogsPage,
    runner_tail_not_configured,
)
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.settings_tab import SettingsTab

FAKE_BEARER = "sk_meridian_fake-control-bearer-0011"


def _ok(lines, path="desktop.log"):
    return LogTail(STATUS_OK, Path(path), tuple(lines), "")


def _missing(path):
    return LogTail(STATUS_NO_LOG_YET, Path(path), (), NO_LOG_YET_TEXT)


def _page(qtbot, *, control, desktop=None):
    page = LogsPage(
        desktop_log=desktop if desktop is not None else (lambda: _ok(["desk-one"])),
        tunnel_log=lambda: _missing("tunnel.log"),
        runner=runner_tail_not_configured,
        control_log=control,
    )
    qtbot.addWidget(page)
    return page


def _press(qtbot, page):
    button = page.findChild(QPushButton, "logs-refresh")
    button.click()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)


def _text(page, name):
    return page.findChild(QPlainTextEdit, name).toPlainText()


def _label(page, name):
    return page.findChild(QLabel, name).text()


def _write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_control_pane_shows_the_last_lines_of_the_control_log(qtbot, tmp_path):
    path = tmp_path / LOG_NAME
    _write(path, ("\n".join(f"control-line-{index:07d} " + "x" * 40 for index in range(4000)) + "\n").encode("utf-8"))
    page = _page(qtbot, control=lambda: read_control_log_tail(path))

    _press(qtbot, page)

    shown = _text(page, "logs-control-text")
    assert "control-line-0003999" in shown
    assert "control-line-0000000" not in shown
    assert len(shown.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert _label(page, "logs-control-heading") == f"Control log: {path}"


def test_control_pane_redacts_secret_shaped_values(qtbot, tmp_path):
    path = tmp_path / LOG_NAME
    _write(path, f"start scope=tunnel outcome=confirmed pid=41\nAuthorization: Bearer {FAKE_BEARER}\n".encode("utf-8"))
    page = _page(qtbot, control=lambda: read_control_log_tail(path))

    _press(qtbot, page)

    shown = _text(page, "logs-control-text")
    assert FAKE_BEARER not in shown
    assert "Bearer [redacted]" in shown
    assert "start scope=tunnel outcome=confirmed pid=41" in shown


def test_missing_control_log_says_what_writes_it_and_what_to_press(qtbot, tmp_path):
    page = _page(qtbot, control=lambda: read_control_log_tail(tmp_path / "absent" / LOG_NAME))

    _press(qtbot, page)

    assert _text(page, "logs-control-text") == CONTROL_MISSING_TEXT
    assert "Start or Stop" in CONTROL_MISSING_TEXT
    assert "Refresh" in CONTROL_MISSING_TEXT


def test_empty_control_log_says_it_is_empty_and_what_to_press(qtbot, tmp_path):
    path = tmp_path / LOG_NAME
    _write(path, b"")
    page = _page(qtbot, control=lambda: read_control_log_tail(path))

    _press(qtbot, page)

    assert _text(page, "logs-control-text") == CONTROL_EMPTY_TEXT
    assert "empty" in CONTROL_EMPTY_TEXT


def test_unreadable_control_log_shows_fixed_text_without_the_error(qtbot):
    page = _page(qtbot, control=lambda: LogTail(STATUS_UNREADABLE, Path("control.log"), (), "x"))

    _press(qtbot, page)

    assert _text(page, "logs-control-text") == CONTROL_UNREADABLE_TEXT


def test_a_failed_control_read_names_its_class_and_the_desktop_log_still_shows(qtbot):
    def failing():
        raise PermissionError("C:/private/path/control.log")

    page = _page(qtbot, control=failing)

    _press(qtbot, page)

    assert _text(page, "logs-control-text") == "The control log check failed unexpectedly (PermissionError)."
    assert "private" not in _text(page, "logs-control-text")
    assert _text(page, "logs-desktop-text") == "desk-one"


def test_settings_has_one_diagnostics_group_with_one_logs_view(qtbot, settings_path):
    tab = SettingsTab(SettingsStore(settings_path), secrets=KeyringSecretStore(backend=MemoryKeyring()))
    qtbot.addWidget(tab)

    groups = [group for group in tab.findChildren(CollapsibleGroup) if group.objectName() == "settings-group-diagnostics"]
    views = tab.findChildren(LogsPage)

    assert len(groups) == 1
    assert len(views) == 1
    assert groups[0].isAncestorOf(views[0])


def test_default_reader_reads_the_control_log_beside_the_settings_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "meridian-desktop" / LOG_NAME
    _write(path, b"start scope=local outcome=confirmed pid=7\n")

    tail = read_control_log_tail()

    assert default_log_path() == path
    assert tail.status == STATUS_OK
    assert tail.path == path
    assert tail.lines == ("start scope=local outcome=confirmed pid=7",)
