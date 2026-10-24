"""Text can be drag-selected over several lines and copied with Ctrl+C.

Each test drives the widget through its window handle, the way a real mouse does: a press, moves with the button held,
and a release, then Ctrl+C through the same window. It then reads the clipboard. Offscreen Qt, fakes only: no socket,
no process, no real settings or keychain, and no modal dialog waits for an answer.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import claude_processes, local_client
from meridian_desktop.claude_processes import ClaudeProcessesResult
from meridian_desktop.data_source import DataSources
from meridian_desktop.home_status import CHECKING_HEADLINE, CardView, RowView
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tools_status import YES, ToolRow, ToolsStatus
from meridian_desktop.ui import app as ui_app
from meridian_desktop.ui import client_setup_section, confirm, sessions_panel
from meridian_desktop.ui.first_run import FirstRunOutcome
from meridian_desktop.ui.home_tab import HomeDeps, HomeTab, _Card
from meridian_desktop.ui.main_window import STATUS_MESSAGE, MainWindow
from meridian_desktop.ui.projects_tab import ProjectsTab
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.sessions_panel import SessionsPanel
from meridian_desktop.ui.text import SELECTABLE_TEXT, visible_text
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus

LEFT = Qt.MouseButton.LeftButton
NO_KEYS = Qt.KeyboardModifier.NoModifier
BEFORE = "<<before>>"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"


# ----------------------------------------------------------------- a mouse and a keyboard, through the window handle


def _activate(window: QWidget) -> None:
    window.show()
    window.activateWindow()
    assert QTest.qWaitForWindowActive(window, 3000)  # a shortcut fires only in the active window


def _drag(window: QWidget, start_widget: QWidget, start: QPoint, end_widget: QWidget, end: QPoint, steps: int = 12):
    """Press, move with the button held, and release, through the top-level window. Qt routes it as a real mouse:
    the widget under the press gets the press and click focus, and the moves and the release go to that widget."""
    handle = window.windowHandle()
    begin = start_widget.mapTo(window, start)
    finish = end_widget.mapTo(window, end)
    QTest.mouseMove(handle, begin)
    QTest.mousePress(handle, LEFT, NO_KEYS, begin)
    for step in range(1, steps + 1):
        QTest.mouseMove(
            handle,
            QPoint(begin.x() + (finish.x() - begin.x()) * step // steps, begin.y() + (finish.y() - begin.y()) * step // steps),
        )
    QTest.mouseRelease(handle, LEFT, NO_KEYS, finish)
    QApplication.processEvents()


def _drag_over(window: QWidget, label: QLabel) -> None:
    """Drag from the first character of a label to its last line's end."""
    rect = label.rect()
    _drag(window, label, QPoint(1, 3), label, QPoint(rect.width() - 2, rect.height() - 2))


def _click(window: QWidget, widget: QWidget, point: QPoint) -> None:
    where = widget.mapTo(window, point)
    QTest.mouseMove(window.windowHandle(), where)
    QTest.mouseClick(window.windowHandle(), LEFT, NO_KEYS, where)
    QApplication.processEvents()


def _ctrl_c(window: QWidget) -> str:
    """Ctrl+C through the window, as a key press reaches the focus widget. Returns what the clipboard holds after."""
    clipboard = QGuiApplication.clipboard()
    clipboard.setText(BEFORE)
    handle = window.windowHandle()
    QTest.keyClick(handle, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    # QTest's own Ctrl release still carries Ctrl, so the application would go on reading Ctrl as held, and a later
    # test's selectRow would add to the selection. A plain release leaves no key held.
    QTest.keyRelease(handle, Qt.Key.Key_Control, NO_KEYS)
    QApplication.processEvents()
    assert QGuiApplication.keyboardModifiers() == NO_KEYS
    return clipboard.text()


def _deferred_deletes() -> None:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ----------------------------------------------------------------- the shared helper


def test_visible_text_reads_the_shown_labels_top_to_bottom_and_skips_hidden_ones_and_buttons(qtbot):
    root = QFrame()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    first = QLabel("First line", root)
    hidden = QLabel("Hidden line", root)
    button = QPushButton("Button text", root)
    second = QLabel("Second line", root)
    empty = QLabel("", root)
    rich = QLabel("<b>Bold</b> words", root)
    rich.setTextFormat(Qt.TextFormat.RichText)
    box = QWidget(root)
    QVBoxLayout(box).addWidget(QLabel("Inside a hidden box", box))
    for widget in (first, hidden, button, second, empty, rich, box):
        layout.addWidget(widget)
    hidden.hide()
    box.hide()
    root.show()
    qtbot.waitExposed(root)
    assert visible_text(root) == "First line\nSecond line\nBold words"

    # A line added while the window shows is read in its layout place at once, before the layout has shown it.
    late = QLabel("Late line", root)
    layout.insertWidget(1, late)
    assert not late.isVisible()
    assert visible_text(root) == "First line\nLate line\nSecond line\nBold words"


# ----------------------------------------------------------------- Home cards


class _Zotero:
    """Zotero is not installed. Nothing is started."""

    def status(self):
        return ZoteroStatus(exe=None, process_running=None, api=ApiCheck(API_NOT_RUNNING, "Zotero is not answering."))

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, "Zotero is not answering.")

    def start_zotero(self, exe):
        raise AssertionError("no test starts Zotero")

    def search_items(self, query):
        return []


def _tunnel_not_available(scope):
    return RunnerResult(verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE)


def _tools_with_rows():
    rows = (
        ToolRow("Codex", "Meridian entries", "no", "unchecked", "Not in Codex's settings yet."),
        ToolRow("This computer", "Local Meridian server", YES, YES, "The local server answers."),
    )
    return ToolsStatus(rows=rows, message="Checked. No tool is started or stopped.", checked_at=datetime(2026, 10, 23, 14, 5))


def _actions(store):
    return RunnerActions(
        store,
        make_control=lambda python, cwd: None,
        confirm=lambda parent, title, body, confirm_text: False,
        find_zotero=lambda: None,
        probe_zotero=lambda: ApiCheck(API_NOT_RUNNING, "not running"),
        start_zotero=lambda exe, log_path=None: (_ for _ in ()).throw(AssertionError("no test starts Zotero")),
        probe_local=lambda url: local_client.HEALTH_NOT_RUNNING,
    )


def _home(qtbot, tmp_path, *, probe=None, tools=None) -> HomeTab:
    store = SettingsStore(tmp_path / "settings.json")
    deps = HomeDeps(
        store=store,
        probe=probe or (lambda url: local_client.HEALTH_NOT_RUNNING),
        tunnel=_tunnel_not_available,
        actions=_actions(store),
        zotero=_Zotero(),
        tools=tools or _tools_with_rows,
        current_version="0.2.0",
    )
    tab = HomeTab(deps)
    qtbot.addWidget(tab)
    tab.resize(1100, 900)
    _activate(tab)
    return tab


def _card(tab, key) -> QFrame:
    card = tab.findChild(QFrame, f"home-card-{key}")
    assert card is not None, key
    return card


def _part(card, key, name) -> QLabel:
    return card.findChild(QLabel, f"home-{key}-{name}")


def _settled(qtbot, card, key, headline=None):
    def done():
        text = _part(card, key, "headline").text()
        checked = _part(card, key, "checked").text()
        if headline is not None:
            return text == headline and checked.startswith("Checked at")
        return text not in ("", CHECKING_HEADLINE) and checked.startswith("Checked at")

    qtbot.waitUntil(done, timeout=5000)


def _copy_action(widget):
    copies = [action for action in widget.actions() if action.text() == "Copy"]
    assert len(copies) == 1, widget.objectName()
    return copies[0]


def test_a_home_card_copies_its_visible_text_from_the_right_click_menu(qtbot, tmp_path):
    tab = _home(qtbot, tmp_path)
    card = _card(tab, "tools")
    _settled(qtbot, card, "tools", "1 need attention")
    assert card.contextMenuPolicy() == Qt.ContextMenuPolicy.ActionsContextMenu
    # A right-click on any of the card's lines opens the card's menu, so Copy is there wherever the owner clicks.
    for label in card.findChildren(QLabel):
        if label.objectName().startswith("home-tools-"):
            assert label.contextMenuPolicy() == Qt.ContextMenuPolicy.NoContextMenu, label.objectName()

    clipboard = QGuiApplication.clipboard()
    clipboard.setText(BEFORE)
    _copy_action(card).trigger()
    copied = clipboard.text()

    lines = copied.split("\n")
    assert lines[:3] == ["AI clients and tools", "1 need attention", "Checked. No tool is started or stopped."]
    assert lines[3].startswith("Checked at ")
    assert lines[4:] == [
        "Codex: Meridian entries. Not configured.",
        "Not in Codex's settings yet.",
        "This computer: Local Meridian server. Live.",
        "The local server answers.",
    ]
    # Button text and hidden widgets are never copied.
    for button in card.findChildren(QPushButton):
        if button.text():
            assert button.text() not in lines, button.objectName()
    assert _part(card, "tools", "note").isHidden()
    assert copied == visible_text(card)

    # A right-click on a row's line opens the card's menu, and its Copy copies the whole card. The mouse's context menu
    # event goes to the line itself: offscreen QTest sometimes hands the one it makes from a click to the top-level
    # widget instead. The probe in the report makes the real right-click through the window.
    row = _part(card, "tools", "row-1-detail")
    seen = []

    def use_open_menu():
        menu = QApplication.activePopupWidget()
        if menu is None:
            return
        seen.append([action.text() for action in menu.actions()])
        next(action for action in menu.actions() if action.text() == "Copy").trigger()
        menu.close()

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(use_open_menu)
    timer.start()
    clipboard.setText(BEFORE)
    point = QPoint(4, 4)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, row.mapToGlobal(point))
    try:
        QApplication.sendEvent(row, event)  # the menu runs until the timer has used it
        qtbot.waitUntil(lambda: bool(seen), timeout=3000)
    finally:
        timer.stop()
    assert seen == [["Copy"]]
    assert clipboard.text() == copied


def test_ctrl_c_copies_the_whole_card_when_the_card_has_focus_and_only_the_selection_in_a_label(qtbot, tmp_path):
    tab = _home(qtbot, tmp_path)
    card = _card(tab, "local")
    _settled(qtbot, card, "local", "Not running")
    corner = QPoint(card.width() - 6, card.height() - 4)
    assert card.childAt(corner) is None  # the card's own margin, not one of its lines
    _click(tab, card, corner)
    assert QApplication.focusWidget() is card
    copied = _ctrl_c(tab)
    assert copied == visible_text(card)
    assert copied.startswith("Local Meridian server\nNot running\n")

    # Inside one line, Ctrl+C copies what was selected there, not the whole card.
    detail = _part(card, "local", "detail")
    _drag_over(tab, detail)
    assert QApplication.focusWidget() is detail
    assert _ctrl_c(tab) == detail.text()


def test_a_recheck_keeps_the_headline_and_a_selection_and_says_checking_in_the_checked_line(qtbot, tmp_path):
    gate = threading.Event()
    calls = []

    def probe(url):
        calls.append(url)
        if len(calls) > 1:
            gate.wait(5)
        return local_client.HEALTH_NOT_RUNNING

    tab = _home(qtbot, tmp_path, probe=probe)
    card = _card(tab, "local")
    _settled(qtbot, card, "local", "Not running")
    detail = _part(card, "local", "detail")
    _drag_over(tab, detail)
    assert detail.hasSelectedText()

    card.check()
    assert _part(card, "local", "headline").text() == "Not running"
    assert _part(card, "local", "checked").text() == CHECKING_HEADLINE
    assert detail.hasSelectedText()

    gate.set()
    _settled(qtbot, card, "local", "Not running")
    assert len(calls) == 2
    assert detail.hasSelectedText(), "the same answer again leaves the selection alone"
    assert _ctrl_c(tab) == detail.text()


def test_unchanged_rows_are_kept_and_changed_rows_replace_the_old_ones(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    card = _Card("tools", "Tools", work=None, render=None, on_action=lambda key: None, parent=parent)
    QVBoxLayout(parent).addWidget(card)
    parent.show()
    qtbot.waitExposed(parent)

    first = CardView(headline="A", rows=(RowView("row a", action="x", action_label="Go"), RowView("row b")))
    card.show_view(first)
    row = card.findChild(QLabel, "home-tools-row-0")
    card.show_view(first)
    _deferred_deletes()
    assert card.findChild(QLabel, "home-tools-row-0") is row, "the same rows are not rebuilt"

    card.show_view(CardView(headline="B", rows=(RowView("row c"),)))
    assert "row a" not in visible_text(card)
    _deferred_deletes()
    rows = [label.text() for label in card.findChildren(QLabel) if label.objectName().startswith("home-tools-row-")]
    assert rows == ["row c"]
    assert card.findChildren(QPushButton, "home-tools-row-0-action") == []


# ----------------------------------------------------------------- Projects and Sessions tables


def _refuse(*args, **kwargs):
    raise AssertionError("not read in this test")


def test_the_projects_table_drags_over_several_rows_and_ctrl_c_copies_them(qtbot, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(mode="local"))
    rows = [
        {"id": f"id-{i}", "name": f"project-{i}", "status": "active", "priority": str(i), "created_at": f"2026-10-0{i + 1}"}
        for i in range(3)
    ]
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_RUNNING,
        read_server=lambda url: rows,
        read_file=_refuse,
        get_token=lambda: None,
        list_hosted=_refuse,
    )
    tab = ProjectsTab(store, sources)
    qtbot.addWidget(tab)
    tab.resize(1000, 500)
    _activate(tab)
    table = tab.findChild(QTableWidget, "projects-table")
    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    assert table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection

    viewport = table.viewport()
    _drag(tab, viewport, table.visualItemRect(table.item(0, 0)).center(), viewport, table.visualItemRect(table.item(2, 3)).center())
    assert sorted({item.row() for item in table.selectedItems()}) == [0, 1, 2]
    assert _ctrl_c(tab) == "\n".join(f"project-{i}\tactive\t{i}\t2026-10-0{i + 1}" for i in range(3))
    # The detail pane shows the first selected row.
    assert "Name: project-0" in tab.findChild(QLabel, "projects-detail").text()


class _Sessions:
    """A local transport that answers three sessions. No socket is opened."""

    def __init__(self):
        self.calls = 0

    def __call__(self, base_url, path, timeout):
        self.calls += 1
        rows = [
            {"id": f"s-{i}", "project_id": PID, "name": f"session-{i}", "status": "active",
             "last_seen": "2026-10-23T11:00:00+00:00", "client_type": "claude-code"}
            for i in range(3)
        ]
        return SimpleNamespace(status=200, body=json.dumps(rows).encode("utf-8"))


class _Clock(datetime):
    """Stands in for datetime inside sessions_panel only. Each reading is five seconds after the last."""

    readings = 0

    @classmethod
    def now(cls, tz=None):
        cls.readings += 1
        return datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=5 * cls.readings)


def test_the_sessions_table_copies_several_rows_and_a_tick_keeps_the_status_selection(qtbot, monkeypatch):
    monkeypatch.setattr(sessions_panel, "datetime", _Clock)
    local = _Sessions()
    panel = SessionsPanel(
        lambda: "http://127.0.0.1:7878",
        lambda: ("https://hosted.invalid", None),
        lambda: [(PID, "Alpha")],
        lambda: "local",
        local_transport=local,
        process_lister=lambda: ClaudeProcessesResult(claude_processes.LISTED, "", ()),
    )
    qtbot.addWidget(panel)
    panel.resize(1000, 700)
    _activate(panel)
    table = panel.findChild(QTableWidget, "sessions-table")
    qtbot.waitUntil(lambda: panel._in_flight is None and table.rowCount() == 3, timeout=5000)  # noqa: SLF001
    panel._timer.stop()  # noqa: SLF001 - the test sends each tick itself
    for name in ("sessions-table", "sessions-claude-table"):
        assert panel.findChild(QTableWidget, name).selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection

    status = panel.findChild(QLabel, "sessions-status")
    checked = panel.findChild(QLabel, "sessions-checked")
    assert status.text() == "3 session(s) from this computer's Meridian server."
    assert checked.text().startswith("Checked at ")
    assert checked.textInteractionFlags() == SELECTABLE_TEXT
    _drag_over(panel, status)
    assert status.hasSelectedText()
    before = checked.text()

    panel._timer.timeout.emit()  # noqa: SLF001 - one 5-second tick
    qtbot.waitUntil(lambda: panel._in_flight is None and checked.text() != before, timeout=5000)  # noqa: SLF001
    assert local.calls == 2
    assert status.hasSelectedText(), "the tick changes only the time, which has its own line"
    assert _ctrl_c(panel) == status.text()

    viewport = table.viewport()
    _drag(panel, viewport, table.visualItemRect(table.item(0, 0)).center(), viewport, table.visualItemRect(table.item(2, 1)).center())
    assert sorted({item.row() for item in table.selectedItems()}) == [0, 1, 2]
    copied = _ctrl_c(panel).split("\n")
    assert [line.split("\t")[0] for line in copied] == ["session-0", "session-1", "session-2"]
    assert all(line.split("\t")[3] == "claude-code" for line in copied)


# ----------------------------------------------------------------- the status bar


class _Runner:
    def status(self, scope):
        return _tunnel_not_available(scope)


def _window(qtbot, tmp_path) -> MainWindow:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(mode="local"))

    class _Secrets:
        def __init__(self):
            self.values = {}

        def get_password(self, service, user):
            return self.values.get((service, user))

        def set_password(self, service, user, password):
            self.values[(service, user)] = password

        def delete_password(self, service, user):
            self.values.pop((service, user), None)

    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_refuse,
        read_file=_refuse,
        get_token=lambda: None,
        list_hosted=_refuse,
    )
    window = MainWindow(
        store,
        secret_store=KeyringSecretStore(backend=_Secrets()),
        data_sources=sources,
        zotero=_Zotero(),
        tunnel_runner=_Runner(),
        tools_status=lambda: ToolsStatus(rows=(), message="Not checked in this test."),
        update_transport=None,
        runner_actions=_actions(store),
    )
    qtbot.addWidget(window)
    return window


def test_the_status_bar_line_is_a_label_that_drags_and_copies(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    _activate(window)
    line = window.findChild(QLabel, "status-line")
    assert line is not None
    assert line.parent() is window.statusBar()
    assert line.textInteractionFlags() == SELECTABLE_TEXT
    assert line.text() == STATUS_MESSAGE == window.status_message()
    assert window.statusBar().currentMessage() == "", "no painted message hides the label"

    _drag_over(window, line)
    assert line.hasSelectedText()
    assert _ctrl_c(window) == STATUS_MESSAGE

    window.show_launch_notes("Launch note: nothing was started.")
    assert line.text() == "Launch note: nothing was started."
    assert window.statusBar().currentMessage() == ""


# ----------------------------------------------------------------- message boxes


def _copy_from_box(monkeypatch, wanted: str, record: dict) -> None:
    """Instead of waiting for an answer, show the box, drag over the line that says ``wanted``, press Ctrl+C, and close it.

    Nothing is clicked, so the box answers as a close does.
    """

    def run(box):
        _activate(box)
        label = next(label for label in box.findChildren(QLabel) if label.text() == wanted)
        record["flags"] = label.textInteractionFlags()
        _drag_over(box, label)
        record["selected"] = label.hasSelectedText()
        record["copied"] = _ctrl_c(box)
        box.close()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", run)


QUESTION = "Remove the RunPod key from this computer?\nThe key is deleted from the keychain."


@pytest.mark.parametrize("ask", [confirm.ask_yes_no, client_setup_section._ask_yes_no], ids=["confirm", "client-setup"])
def test_a_question_box_text_drags_and_copies_and_a_close_is_still_a_no(qtbot, monkeypatch, ask):
    record = {}
    _copy_from_box(monkeypatch, QUESTION, record)
    assert ask(None, "Meridian Desktop", QUESTION) is False
    assert record["flags"] & SELECTABLE_TEXT == SELECTABLE_TEXT
    assert record["selected"]
    assert record["copied"] == QUESTION


def test_the_first_run_notice_is_a_built_box_whose_text_drags_and_copies(qtbot, monkeypatch, tmp_path):
    notice = "Autostart could not be turned on.\nRun install.cmd, then start Meridian Desktop again."
    record = {}
    _copy_from_box(monkeypatch, notice, record)

    def static_box(*args, **kwargs):
        raise AssertionError("the static information box cannot be selected; build the box instead")

    class Stop(Exception):
        pass

    monkeypatch.setattr(QMessageBox, "information", static_box)
    monkeypatch.setattr(ui_app, "run_first_run", lambda store: FirstRunOutcome(settings=Settings(), notice=notice))
    # Neither the runner controls nor the window is built: the test stops main once the notice is closed.
    monkeypatch.setattr(ui_app, "RunnerActions", lambda *args, **kwargs: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        ui_app.main([], settings_path=tmp_path / "settings" / "settings.json")
    assert record["flags"] & SELECTABLE_TEXT == SELECTABLE_TEXT
    assert record["selected"]
    assert record["copied"] == notice
