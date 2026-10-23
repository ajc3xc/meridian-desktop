"""The LaTeX engine texts and re-checks (symptom F: "LaTeX engine: not running" with no way to start it).

The HTTP engine is started with node dist/server.js from the engine folder. The meridian-latex tools in Claude run a
separate stdio server (dist/mcp-server.js) that never listens on the engine port. These tests check that:

1. every not-running text (the LaTeX panel, the Services page and the Home card) names that command from one shared
   constant, and says the Claude tools do not start this server;
2. the extension version the engine sends as a hash (GET /extension-version answers {"hash": ...}) shows in short form;
3. the LaTeX panel and the Services page check again on every show, not only the first;
4. the Settings LaTeX label names the saved engine address, not a fixed one.

The loopback engine fakes bind port 0, so the OS picks a free port. No test touches the real engine port. Offscreen Qt
and a temp home, from conftest.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

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
from meridian_desktop.ui import services_page
from meridian_desktop.ui.latex_panel import CHECKING_TEXT, CONNECTED_TEXT, LatexPanel, engine_start_text
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


def _panel_settled(panel):
    status = panel.findChild(QLabel, "latex-status").text()
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

    assert view.headline == "LaTeX engine not running"
    assert START_HELP in view.rows[0].detail
    assert view.rows[0].detail.startswith("Nothing listens on that port. To start it, ")
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
    qtbot.waitUntil(lambda: panel.findChild(QLabel, "latex-status").text() == CONNECTED_TEXT, timeout=5000)

    assert panel.findChild(QLabel, "latex-extension-version").text() == f"Extension version: {SHORT_HASH}"
    assert panel.findChild(QLabel, "latex-engine-version").text() == f"Engine version: {NOT_REPORTED}"
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
    qtbot.waitUntil(lambda: panel.findChild(QLabel, "latex-status").text() == CONNECTED_TEXT, timeout=5000)
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
    state = page.findChild(QLabel, "services-engine")

    assert engine.calls == []  # building the page checks nothing

    page.show()
    qtbot.waitExposed(page)
    qtbot.waitUntil(lambda: len(engine.calls) == 1 and not page.busy, timeout=5000)
    assert state.text() == "LaTeX engine: not running"

    engine.value = CONNECTED_STATUS
    page.hide()
    page.show()
    qtbot.waitUntil(lambda: len(engine.calls) == 2 and not page.busy, timeout=5000)
    assert state.text() == "LaTeX engine: running"

    page.findChild(QPushButton, "services-check").click()
    qtbot.waitUntil(lambda: len(engine.calls) == 3 and not page.busy, timeout=5000)


# ----------------------------------------------------------------- 4. the Settings label names the saved address


@pytest.mark.parametrize(
    "url, label",
    [
        ("http://127.0.0.1:8471", "LaTeX (local engine on 127.0.0.1:8471)"),
        ("http://127.0.0.1:9999", "LaTeX (local engine on 127.0.0.1:9999)"),
        ("http://localhost:8472/", "LaTeX (local engine on localhost:8472)"),
        ("http://[::1]:8471", "LaTeX (local engine on [::1]:8471)"),
        ("http://127.0.0.1", "LaTeX (local engine on 127.0.0.1)"),
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

    assert label == "LaTeX (local engine; the saved engine address is not on this computer)"
    assert hidden not in label


def test_the_default_label_is_the_one_the_window_and_tests_look_for():
    assert LATEX_SECTION == "LaTeX (local engine on 127.0.0.1:8471)"


def test_the_settings_tab_label_reads_the_engine_url_setting(qtbot, settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(engine_url="http://127.0.0.1:9999"))
    tab = SettingsTab(store, secrets=KeyringSecretStore(backend=MemoryKeyring()))
    qtbot.addWidget(tab)

    texts = [label.text() for label in tab.findChildren(QLabel)]
    assert "LaTeX (local engine on 127.0.0.1:9999)" in texts
    assert LATEX_SECTION not in texts

    store.save(Settings(engine_url="http://localhost:8475"))
    tab.refresh()
    assert tab.findChild(QLabel, "settings-latex-section").text() == "LaTeX (local engine on localhost:8475)"
