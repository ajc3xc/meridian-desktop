"""The LaTeX engine texts and re-checks (symptom F: "LaTeX engine: not running" with no way to start it).

The HTTP engine is started with node dist/server.js from the engine folder. The meridian-latex tools in Claude run a
separate stdio server (dist/mcp-server.js) that never listens on the engine port. These tests check that:

1. every not-running text (the LaTeX panel, the Services page and the Home card) names that command from one shared
   constant, and says the Claude tools do not start this server;
2. the extension version the engine sends as a hash (GET /extension-version answers {"hash": ...}) shows in short form;
3. the LaTeX panel and the Services page check again on every show, not only the first;
4. the Settings LaTeX label names the saved engine address, not a fixed one;
5. a left-button drag across a block of lines in the LaTeX panel or on the Services page highlights the whole block,
   and Ctrl+C copies all of it, because each block is one selectable label;
6. the LaTeX panel, the Services page, the Home card and the Settings label lead with Overleaf in the browser, call the
   local engine an optional backup that is off unless you turn it on, keep the exact start command, and add no new
   control.

The loopback engine fakes bind port 0, so the OS picks a free port. No test touches the real engine port. Offscreen Qt
and a temp home, from conftest.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

from _fakes import MemoryKeyring, QuietServer
from meridian_desktop import home_status, latex_client
from meridian_desktop.home_status import ServiceFacts, services_view
from meridian_desktop.latex_client import (
    ENGINE_CONNECTED,
    ENGINE_NOT_RUNNING,
    EXTENSION_VERSION_PATH,
    HEALTH_PATH,
    NOT_REPORTED,
    START_COMMAND,
    START_HELP,
    EngineStatus,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.slot_ports import SLOT_PORTS, SlotPortResult
from meridian_desktop.ui import latex_panel, services_page
from meridian_desktop.ui.latex_panel import CHECKING_TEXT, CONNECTED_TEXT, LatexPanel, TextBlock, engine_start_text
from meridian_desktop.ui.services_page import HealthFacts, ServicesPage, engine_row
from meridian_desktop.ui.settings_tab import LATEX_SECTION, SettingsTab, latex_section_text

# The shapes engine/src/server.ts sends: GET /health -> { ok: true }, GET /extension-version -> { hash } (sha256 hex).
ENGINE_HASH = "3f2a9c1b7d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8"
SERVER_TS_ROUTES = {
    HEALTH_PATH: (200, json.dumps({"ok": True}).encode("utf-8")),
    EXTENSION_VERSION_PATH: (200, json.dumps({"hash": ENGINE_HASH}).encode("utf-8")),
}
SHORT_HASH = f"{ENGINE_HASH[:12]} (hash)"
NOT_RUNNING_STATUS = EngineStatus(ENGINE_NOT_RUNNING, latex_client.NOT_RUNNING_MESSAGE)
CONNECTED_STATUS = EngineStatus(ENGINE_CONNECTED, latex_client.CONNECTED_MESSAGE, NOT_REPORTED, SHORT_HASH)
OLD_WORDS = "the LaTeX engine you installed on this computer"


class _ServerTsEngine:
    """A loopback stand-in for engine/src/server.ts on a port the OS picks. Other paths answer 404 as JSON."""

    def __init__(self, routes=SERVER_TS_ROUTES):
        self.paths = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                engine.paths.append(self.path)
                status, body = routes.get(self.path, (404, b'{"error": "not found"}'))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep test output quiet
                return

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def server_ts_engine():
    engine = _ServerTsEngine()
    assert engine.port != 8471  # the real engine port is never bound here
    yield engine
    engine.close()


class _FakeGet:
    """A request function that answers from a table and records each path."""

    def __init__(self, answers):
        self.answers = answers
        self.paths = []

    def __call__(self, base_url, path, timeout):
        self.paths.append(path)
        status, body = self.answers[path]
        return type("Reply", (), {"status": status, "body": body})()


class _Recorder:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.value


class _FakeEngineClient:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def probe_engine(self, base_url, timeout):
        self.calls.append((base_url, timeout))
        return self.status


def _line(root, key):
    """The text of the line ``key`` in the one TextBlock under ``root`` that holds it."""
    blocks = [block for block in root.findChildren(TextBlock) if key in block.keys()]
    assert len(blocks) == 1, key
    return blocks[0].line(key)


def _panel_settled(panel):
    status = _line(panel, "latex-status")
    return panel.findChild(QPushButton, "latex-check").isEnabled() and status not in (CHECKING_TEXT,)


# ----------------------------------------------------------------- 1. the not-running text names the command


def test_the_shared_start_text_names_the_command_the_folder_and_the_stdio_tools():
    assert START_COMMAND == "node dist/server.js"
    assert START_COMMAND in START_HELP
    assert "engine folder" in START_HELP and "dist" in START_HELP and "extension" in START_HELP
    assert "stdio" in START_HELP and "do not start this one" in START_HELP


def test_the_latex_panel_not_running_text_uses_the_shared_start_text():
    text = engine_start_text(NOT_RUNNING_STATUS, "8471")

    assert START_HELP in text
    assert "port 8471" in text
    assert text.endswith("press Check again to check port 8471.")
    assert OLD_WORDS not in text


def test_the_services_page_not_running_text_uses_the_shared_start_text():
    row = engine_row("http://127.0.0.1:8471", NOT_RUNNING_STATUS)

    assert row.state == services_page.STATE_NOT_RUNNING
    assert START_HELP in row.how_to_start
    assert row.how_to_start.endswith("Then press Check services.")
    assert OLD_WORDS not in row.how_to_start


def test_the_home_card_not_running_text_uses_the_shared_start_text():
    view = services_view(ServiceFacts(engine_url="http://127.0.0.1:8471", engine=NOT_RUNNING_STATUS, ports=()))

    assert view.headline == "LaTeX backup engine off"
    assert START_HELP in view.rows[0].detail
    assert view.rows[0].detail.startswith("Nothing listens on that port. ")
    assert "To turn it on, " + START_HELP in view.rows[0].detail
    assert "Check services" not in view.rows[0].detail  # Home has no Check services button
    assert OLD_WORDS not in view.rows[0].detail


def test_the_home_card_takes_its_text_from_the_one_shared_constant():
    assert home_status._ENGINE_DETAIL[ENGINE_NOT_RUNNING].endswith(START_HELP)


# ----------------------------------------------------------------- 2. the extension hash is shown in short form


def test_the_server_ts_shapes_read_as_connected_with_the_short_hash():
    get = _FakeGet(SERVER_TS_ROUTES)

    status = latex_client.probe_engine("http://127.0.0.1:8471", get=get)

    assert get.paths == [HEALTH_PATH, EXTENSION_VERSION_PATH]
    assert status.state == ENGINE_CONNECTED
    assert status.engine_version == NOT_REPORTED  # /health sends { ok: true }, no version
    assert status.extension_version == SHORT_HASH


@pytest.mark.parametrize(
    "payload, shown",
    [
        ({"hash": ENGINE_HASH}, SHORT_HASH),
        ({"hash": ENGINE_HASH.upper()}, SHORT_HASH),
        ({"hash": "abc1234"}, "abc1234 (hash)"),
        ({"version": "0.9.2", "hash": ENGINE_HASH}, "0.9.2"),  # a real version still wins
        ({"hash": "not-a-hash"}, NOT_REPORTED),
        ({"hash": "<b>3f2a9c1b7d4e</b>"}, NOT_REPORTED),
        ({"hash": "abc12"}, NOT_REPORTED),  # too short to be a hash
        ({"hash": "a" * 200}, NOT_REPORTED),  # too long
        ({"hash": 12345678}, NOT_REPORTED),
        ({}, NOT_REPORTED),
    ],
)
def test_only_a_plain_hex_hash_is_shown_and_only_in_short_form(payload, shown):
    get = _FakeGet({HEALTH_PATH: (200, b'{"ok": true}'), EXTENSION_VERSION_PATH: (200, json.dumps(payload).encode())})

    status = latex_client.probe_engine("http://127.0.0.1:8471", get=get)

    assert status.extension_version == shown


def test_the_real_probe_reads_a_loopback_server_ts_engine_as_connected(server_ts_engine):
    status = latex_client.probe_engine(server_ts_engine.url)

    assert server_ts_engine.paths == [HEALTH_PATH, EXTENSION_VERSION_PATH]
    assert status.connected
    assert status.engine_version == NOT_REPORTED
    assert status.extension_version == SHORT_HASH


def test_the_panel_shows_the_short_hash_from_a_loopback_server_ts_engine(qtbot, settings_path, server_ts_engine):
    SettingsStore(settings_path).save(Settings(engine_url=server_ts_engine.url))
    panel = LatexPanel(SettingsStore(settings_path))
    qtbot.addWidget(panel)

    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: _line(panel, "latex-status") == CONNECTED_TEXT, timeout=5000)

    assert _line(panel, "latex-extension-version") == f"Extension version: {SHORT_HASH}"
    assert _line(panel, "latex-engine-version") == f"Engine version: {NOT_REPORTED}"
    assert ENGINE_HASH not in "\n".join(label.text() for label in panel.findChildren(QLabel))


# ----------------------------------------------------------------- 3. a check on every show


def test_the_latex_panel_checks_again_each_time_it_is_shown(qtbot, settings_path):
    client = _FakeEngineClient(NOT_RUNNING_STATUS)
    panel = LatexPanel(SettingsStore(settings_path), client=client)
    qtbot.addWidget(panel)

    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: len(client.calls) == 1 and _panel_settled(panel), timeout=5000)

    client.status = CONNECTED_STATUS  # the owner started the engine while the panel was hidden
    panel.hide()
    panel.show()
    qtbot.waitUntil(lambda: _line(panel, "latex-status") == CONNECTED_TEXT, timeout=5000)
    assert len(client.calls) == 2

    panel.findChild(QPushButton, "latex-check").click()
    qtbot.waitUntil(lambda: len(client.calls) == 3 and _panel_settled(panel), timeout=5000)


def _services_page(qtbot, settings_path, engine):
    page = ServicesPage(
        SettingsStore(settings_path),
        health=_Recorder(HealthFacts(state="running")),
        engine=engine,
        runner=_Recorder(services_page.runner_not_configured()),
        ports=_Recorder([SlotPortResult(slot, port, False) for slot, port in SLOT_PORTS]),
    )
    qtbot.addWidget(page)
    return page


def test_the_services_page_checks_on_every_show_and_on_the_button(qtbot, settings_path):
    engine = _Recorder(NOT_RUNNING_STATUS)
    page = _services_page(qtbot, settings_path, engine)
    state = page.findChild(TextBlock, "services-engine-text")

    assert engine.calls == []  # building the page checks nothing

    page.show()
    qtbot.waitExposed(page)
    qtbot.waitUntil(lambda: len(engine.calls) == 1 and not page.busy, timeout=5000)
    assert state.line("services-engine") == f"{services_page.ENGINE_TITLE}: not running"

    engine.value = CONNECTED_STATUS
    page.hide()
    page.show()
    qtbot.waitUntil(lambda: len(engine.calls) == 2 and not page.busy, timeout=5000)
    assert state.line("services-engine") == f"{services_page.ENGINE_TITLE}: running"

    page.findChild(QPushButton, "services-check").click()
    qtbot.waitUntil(lambda: len(engine.calls) == 3 and not page.busy, timeout=5000)


# ----------------------------------------------------------------- 4. the Settings label names the saved address


@pytest.mark.parametrize(
    "url, label",
    [
        ("http://127.0.0.1:8471", "LaTeX (Overleaf in the browser; local backup engine on 127.0.0.1:8471)"),
        ("http://127.0.0.1:9999", "LaTeX (Overleaf in the browser; local backup engine on 127.0.0.1:9999)"),
        ("http://localhost:8472/", "LaTeX (Overleaf in the browser; local backup engine on localhost:8472)"),
        ("http://[::1]:8471", "LaTeX (Overleaf in the browser; local backup engine on [::1]:8471)"),
        ("http://127.0.0.1", "LaTeX (Overleaf in the browser; local backup engine on 127.0.0.1)"),
    ],
)
def test_the_settings_latex_label_names_the_saved_loopback_address(url, label):
    assert latex_section_text(url) == label


@pytest.mark.parametrize(
    "url, hidden",
    [
        ("http://someone@127.0.0.1:8471", "someone"),
        ("http://example.com:8471", "example.com"),
        ("https://127.0.0.1:8471", "https"),
        ("not a url", "not a url"),
    ],
)
def test_the_settings_latex_label_never_shows_an_address_that_is_not_loopback(url, hidden):
    label = latex_section_text(url)

    assert label == "LaTeX (Overleaf in the browser; the saved backup engine address is not on this computer)"
    assert hidden not in label


def test_the_default_label_is_the_one_the_window_and_tests_look_for():
    assert LATEX_SECTION == "LaTeX (Overleaf in the browser; local backup engine on 127.0.0.1:8471)"


def test_the_settings_tab_label_reads_the_engine_url_setting(qtbot, settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(engine_url="http://127.0.0.1:9999"))
    tab = SettingsTab(store, secrets=KeyringSecretStore(backend=MemoryKeyring()))
    qtbot.addWidget(tab)

    texts = [label.text() for label in tab.findChildren(QLabel)]
    assert "LaTeX (Overleaf in the browser; local backup engine on 127.0.0.1:9999)" in texts
    assert LATEX_SECTION not in texts

    store.save(Settings(engine_url="http://localhost:8475"))
    tab.refresh()
    assert tab.findChild(QLabel, "settings-latex-section").text() == "LaTeX (Overleaf in the browser; local backup engine on localhost:8475)"


# ----------------------------------------------------------------- 5. one drag selects a whole block, Ctrl+C copies it


def _drag_across(widget):
    """A left-button drag from the top-left corner of ``widget`` to its bottom-right corner, sent as a mouse sends it."""
    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    start, end = QPoint(1, 1), QPoint(widget.width() - 2, widget.height() - 2)

    def send(kind, pos, buttons, button):
        event = QMouseEvent(
            kind, QPointF(pos), QPointF(widget.mapToGlobal(pos)), button, buttons, Qt.KeyboardModifier.NoModifier
        )
        QApplication.sendEvent(widget, event)

    send(QEvent.Type.MouseButtonPress, start, left, left)
    for step in range(1, 9):
        x = start.x() + (end.x() - start.x()) * step // 8
        y = start.y() + (end.y() - start.y()) * step // 8
        send(QEvent.Type.MouseMove, QPoint(x, y), left, none)
    send(QEvent.Type.MouseButtonRelease, end, none, left)


def _ctrl_c(widget):
    """Press Ctrl+C on ``widget`` and return what the clipboard then holds."""
    clipboard = QGuiApplication.clipboard()
    clipboard.setText("<<nothing copied>>")
    widget.setFocus()
    QTest.keyClick(widget, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    return clipboard.text()


def _label_with(root, text):
    """The one label under ``root`` that shows ``text``."""
    found = [label for label in root.findChildren(QLabel) if text in label.text()]
    assert len(found) == 1, [label.objectName() for label in found]
    return found[0]


def _shown_panel(qtbot, settings_path, status):
    panel = LatexPanel(SettingsStore(settings_path), client=_FakeEngineClient(status))
    qtbot.addWidget(panel)
    panel.resize(900, 500)
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: _panel_settled(panel), timeout=5000)
    return panel


@pytest.mark.parametrize("status", [NOT_RUNNING_STATUS, CONNECTED_STATUS])
def test_one_drag_across_the_latex_panel_text_copies_every_line(qtbot, settings_path, status):
    panel = _shown_panel(qtbot, settings_path, status)
    shown = CONNECTED_TEXT if status.connected else latex_client.NOT_RUNNING_MESSAGE
    block = _label_with(panel, shown)

    _drag_across(block)
    copied = _ctrl_c(block)

    assert block.hasSelectedText()
    assert copied == block.text()
    for line in (shown, "Engine version:", "Extension version:", "How to start:"):
        assert line in copied, line
    assert copied.count("\n") >= 4  # the lines are copied as lines


def test_the_latex_panel_text_is_one_plain_selectable_label(qtbot, settings_path):
    panel = _shown_panel(qtbot, settings_path, NOT_RUNNING_STATUS)

    labels = [label for label in panel.findChildren(QLabel) if label.text()]

    assert [label.objectName() for label in labels] == ["latex-text"]
    assert labels[0].textFormat() == Qt.TextFormat.PlainText
    flags = labels[0].textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert flags & Qt.TextInteractionFlag.TextSelectableByKeyboard


def _shown_services_page(qtbot, settings_path, engine_status):
    page = _services_page(qtbot, settings_path, _Recorder(engine_status))
    page.resize(900, 900)
    page.show()
    qtbot.waitExposed(page)
    qtbot.waitUntil(lambda: not page.busy, timeout=5000)
    return page


@pytest.mark.parametrize("prefix, state_line", [
    ("services-health", "Local server health: running"),
    ("services-engine", f"{services_page.ENGINE_TITLE}: not running"),
    ("services-runner", "Runner: could not check"),
])
def test_one_drag_across_a_services_row_copies_the_whole_row(qtbot, settings_path, prefix, state_line):
    page = _shown_services_page(qtbot, settings_path, NOT_RUNNING_STATUS)
    row = page.findChild(QFrame, f"{prefix}-row")
    block = _label_with(row, state_line)

    _drag_across(block)
    copied = _ctrl_c(block)

    assert copied == block.text()
    for line in (state_line, "Port checked:", "Last checked:", "How to start:"):
        assert line in copied, line
    # The whole row is that one label.
    assert [label for label in row.findChildren(QLabel) if label.text()] == [block]


def test_a_text_block_keeps_its_selection_when_its_lines_are_set_to_the_same_text(qtbot):
    block = TextBlock(None, "test-block", [("a", "First line"), ("b", ""), ("c", "Third line")])
    qtbot.addWidget(block)
    block.resize(400, 120)
    block.show()
    qtbot.waitExposed(block)

    assert block.text() == "First line\nThird line"  # an empty line is left out
    _drag_across(block)
    assert block.hasSelectedText()

    block.set_lines({"a": "First line", "c": "Third line"})
    assert block.hasSelectedText()  # nothing changed, so the selection stays

    block.set_lines({"b": "Second line"})
    assert block.text() == "First line\nSecond line\nThird line"
    assert block.line("b") == "Second line"
    with pytest.raises(KeyError):
        block.set_lines({"unknown": "text"})


# ----------------------------------------------------------------- 6. Overleaf in the browser first, the engine a backup


def _leads_with_overleaf(text):
    """The first line names Overleaf in the browser, and the text calls the local engine a backup that is off."""
    first = text.splitlines()[0]
    return "Overleaf" in first and "browser" in first and "backup" in text and "off unless you turn it on" in text


def test_the_shared_texts_lead_with_overleaf_and_call_the_engine_an_optional_backup():
    assert "Overleaf" in latex_client.OVERLEAF_LEAD and "browser" in latex_client.OVERLEAF_LEAD
    note = latex_client.BACKUP_NOTE
    assert "optional backup" in note and "raw .tex files" in note and "local projects" in note
    assert "off unless you turn it on" in note
    assert latex_client.BACKUP_START == f"if you want the local backup, {START_HELP}"


def test_the_latex_panel_leads_with_overleaf_before_it_is_checked(qtbot, settings_path):
    panel = LatexPanel(SettingsStore(settings_path), client=_FakeEngineClient(NOT_RUNNING_STATUS))
    qtbot.addWidget(panel)
    text = panel.findChild(TextBlock, "latex-text").text()

    assert _leads_with_overleaf(text), text
    assert text.splitlines()[:2] == [latex_client.OVERLEAF_LEAD, latex_client.BACKUP_NOTE]
    assert START_COMMAND in _line(panel, "latex-start")


@pytest.mark.parametrize("status", [NOT_RUNNING_STATUS, CONNECTED_STATUS])
def test_the_latex_panel_leads_with_overleaf_after_a_check(qtbot, settings_path, status):
    panel = _shown_panel(qtbot, settings_path, status)
    text = panel.findChild(TextBlock, "latex-text").text()

    assert _leads_with_overleaf(text), text
    assert _line(panel, "latex-status").startswith("Local backup engine: ")


def test_the_latex_panel_not_running_text_offers_the_backup_with_the_exact_command(qtbot, settings_path):
    panel = _shown_panel(qtbot, settings_path, NOT_RUNNING_STATUS)

    assert _line(panel, "latex-status") == "Local backup engine: not running."
    start = _line(panel, "latex-start")
    assert start == f"How to start: {latex_client.BACKUP_START} Then press Check again to check port 8471."
    assert "node dist/server.js" in start and "engine folder" in start
    assert "stdio server" in start  # the Claude meridian-latex tools use a separate server


def test_the_services_engine_row_leads_with_overleaf_and_calls_the_engine_a_backup(qtbot, settings_path):
    page = _services_page(qtbot, settings_path, _Recorder(NOT_RUNNING_STATUS))
    block = page.findChild(TextBlock, "services-engine-text")
    assert _leads_with_overleaf(block.text()), block.text()  # before any check

    page.resize(900, 900)
    page.show()
    qtbot.waitExposed(page)
    qtbot.waitUntil(lambda: not page.busy, timeout=5000)

    assert _leads_with_overleaf(block.text()), block.text()
    assert block.line("services-engine") == "LaTeX backup engine: not running"
    assert block.line("services-engine-start") == (
        f"How to start: {latex_client.BACKUP_START} Then press Check services."
    )
    # The other rows are not about LaTeX, so they carry no Overleaf line.
    assert "Overleaf" not in page.findChild(TextBlock, "services-health-text").text()
    assert "LaTeX backup engine" in page.findChild(QLabel, "services-intro").text()


def test_the_home_card_leads_with_overleaf_and_reads_off_when_the_backup_is_not_running():
    view = services_view(ServiceFacts(engine_url="http://127.0.0.1:8471", engine=NOT_RUNNING_STATUS, ports=()))

    assert view.headline == "LaTeX backup engine off"
    assert view.tone == home_status.TONE_IDLE  # off is the normal state, not a fault
    assert view.rows[0].text == f"{latex_client.OVERLEAF_LEAD} Local backup engine on port 8471."
    assert view.rows[0].detail == (
        f"Nothing listens on that port. {latex_client.BACKUP_NOTE} To turn it on, {START_HELP}"
    )


@pytest.mark.parametrize("engine, failure", [(CONNECTED_STATUS, ""), (None, "OSError")])
def test_the_home_card_leads_with_overleaf_in_every_engine_state(engine, failure):
    view = services_view(
        ServiceFacts(engine_url="http://127.0.0.1:8471", engine=engine, engine_failure=failure, ports=())
    )

    assert view.rows[0].text.startswith(latex_client.OVERLEAF_LEAD)
    assert "backup" in view.rows[0].text


def test_the_settings_latex_label_leads_with_overleaf():
    assert LATEX_SECTION == "LaTeX (Overleaf in the browser; local backup engine on 127.0.0.1:8471)"
    assert latex_section_text("http://example.com:8471") == (
        "LaTeX (Overleaf in the browser; the saved backup engine address is not on this computer)"
    )


def test_no_latex_text_still_says_the_engine_you_installed():
    texts = [engine_start_text(status, "8471") for status in (
        None,
        NOT_RUNNING_STATUS,
        CONNECTED_STATUS,
        EngineStatus(latex_client.ENGINE_NOT_ANSWERING, latex_client.NOT_ANSWERING_MESSAGE),
        EngineStatus(latex_client.ENGINE_ERROR, latex_client.TIMEOUT_MESSAGE),
    )]
    texts += [latex_panel.UNCHECKED_START, services_page.ENGINE_START_UNCHECKED, *services_page.ENGINE_START.values()]
    for text in texts:
        assert "you installed" not in text, text


def test_no_open_overleaf_button_setting_or_start_control_is_added(qtbot, settings_path):
    panel = _shown_panel(qtbot, settings_path, NOT_RUNNING_STATUS)
    page = _shown_services_page(qtbot, settings_path, NOT_RUNNING_STATUS)

    assert [button.text() for button in panel.findChildren(QPushButton)] == ["Check again"]
    assert [button.text() for button in page.findChildren(QPushButton)] == [services_page.CHECK_BUTTON_TEXT]
