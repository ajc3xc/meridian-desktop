"""Tests for the RunPod panel (meridian_desktop/ui/runpod_panel.py), with pytest-qt.

The store and the local client are fakes, so nothing touches the keychain or the network.
The settings file is a temp file. Offscreen Qt, from conftest.
"""

import copy
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTableWidget, QWidget

from meridian_desktop.local_client import HEALTH_ERROR, HEALTH_NOT_MERIDIAN, HEALTH_NOT_RUNNING, HEALTH_RUNNING, LocalClientError
from meridian_desktop.secret_store import SecretStoreError
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.runpod_panel import (
    KEY_MISSING_TEXT,
    KEY_STORED_TEXT,
    POD_COLUMNS,
    RunPodPanel,
)

FAKE_KEY = "test-key-not-real"
LOCAL_URL = "http://127.0.0.1:7878"

POD_A = {
    "id": "pod-a1",
    "name": "trainer",
    "status": "RUNNING",
    "gpu": {"id": "NVIDIA RTX A4000", "count": 1},
    "costPerHour": 0.34,
    "uptimeSeconds": 7200,
}
POD_B = {
    "id": "pod-b2",
    "name": "idle-box",
    "status": "EXITED",
    "gpu": None,
    "costPerHour": 0.0,
    "uptimeSeconds": None,
}


class FakeStore:
    """An in-memory secret store. It records each call and can be told to fail."""

    def __init__(self, value=None):
        self.value = value
        self.failure = None
        self.calls = []

    def _enter(self, operation):
        self.calls.append(operation)
        if self.failure is not None:
            raise self.failure

    def get_secret(self, service, account):
        self._enter("get")
        return self.value

    def set_secret(self, service, account, value):
        self._enter("set")
        cleaned = value.strip()
        if not cleaned:
            raise SecretStoreError("secret is empty")
        self.value = cleaned

    def delete_secret(self, service, account):
        self._enter("delete")
        if self.value is None:
            return False
        self.value = None
        return True


class FakeClient:
    """A local client that returns canned answers and records each call and its thread."""

    def __init__(self):
        self.state = HEALTH_RUNNING
        self.check = {
            "ok": True,
            "credential_source": "keyring",
            "pod_count": 2,
            "billing_available": True,
            "error": None,
        }
        self.listing = {
            "ok": True,
            "credential_source": "keyring",
            "pods": [POD_A, POD_B],
            "pod_count": 2,
            "has_more_pods": False,
            "error": None,
        }
        self.raise_on = {}
        self.gate = None
        self.calls = []
        self.threads = []

    def _note(self, name, base_url, timeout):
        self.calls.append((name, base_url, timeout))
        self.threads.append(threading.current_thread())
        if name in self.raise_on:
            raise self.raise_on[name]

    def probe_health(self, base_url, timeout):
        self._note("probe_health", base_url, timeout)
        if self.gate is not None:
            self.gate.wait(5)
        return self.state

    def runpod_check(self, base_url, timeout):
        self._note("runpod_check", base_url, timeout)
        return copy.deepcopy(self.check)

    def runpod_list_pods(self, base_url, timeout):
        self._note("runpod_list_pods", base_url, timeout)
        return copy.deepcopy(self.listing)


def wait_idle(qtbot, panel):
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)


def press(qtbot, panel, name):
    """Click a named button, then wait for its job to finish."""
    panel.findChild(QPushButton, name).click()
    wait_idle(qtbot, panel)


def text_of(panel, name):
    return panel.findChild(QLabel, name).text()


def all_text(panel):
    """Every string a user can read in the panel: labels, buttons, fields, and table cells."""
    chunks = []
    for widget in [panel, *panel.findChildren(QWidget)]:
        for getter in ("text", "placeholderText", "windowTitle", "toolTip"):
            method = getattr(widget, getter, None)
            if callable(method):
                try:
                    value = method()
                except TypeError:
                    continue
                if isinstance(value, str):
                    chunks.append(value)
    table = panel.findChild(QTableWidget)
    if table is not None:
        for column in range(table.columnCount()):
            header = table.horizontalHeaderItem(column)
            if header is not None:
                chunks.append(header.text())
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    chunks.append(item.text())
    return "\n".join(chunks)


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def make_panel(qtbot, settings_path):
    def build(store, client):
        SettingsStore(settings_path).save(Settings(local_url=LOCAL_URL))
        panel = RunPodPanel(SettingsStore(settings_path), store, client)
        qtbot.addWidget(panel)
        wait_idle(qtbot, panel)
        return panel

    return build


# ----------------------------------------------------------------- key status


def test_status_shows_missing_when_no_key_is_stored(make_panel, store, client):
    panel = make_panel(store, client)
    assert text_of(panel, "runpod-key-status") == KEY_MISSING_TEXT


def test_status_shows_stored_without_showing_the_value(make_panel, client):
    panel = make_panel(FakeStore(FAKE_KEY), client)
    assert text_of(panel, "runpod-key-status") == KEY_STORED_TEXT
    assert FAKE_KEY not in all_text(panel)


def test_status_reports_a_failed_read_in_plain_words(make_panel, client):
    store = FakeStore()
    store.failure = SecretStoreError("keyring read failed (RuntimeError)")
    panel = make_panel(store, client)
    assert text_of(panel, "runpod-key-status") == (
        "Key storage could not be read. keyring read failed (RuntimeError)"
    )


# ----------------------------------------------------------------- save


def test_save_stores_the_key_clears_the_field_and_hides_the_value(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    field = panel.findChild(QLineEdit, "runpod-key-input")
    field.setText(FAKE_KEY)

    press(qtbot, panel, "runpod-save-key")

    assert store.value == FAKE_KEY
    assert field.text() == ""
    assert text_of(panel, "runpod-key-status") == KEY_STORED_TEXT
    assert text_of(panel, "runpod-result") == "Key saved in the keychain."
    assert FAKE_KEY not in all_text(panel)


def test_save_strips_surrounding_whitespace(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    panel.findChild(QLineEdit, "runpod-key-input").setText(
        f"  {FAKE_KEY}  "
    )
    press(qtbot, panel, "runpod-save-key")
    assert store.value == FAKE_KEY


def test_save_with_blank_field_makes_no_store_call(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    panel.findChild(QLineEdit, "runpod-key-input").setText("   ")
    press(qtbot, panel, "runpod-save-key")
    assert store.calls == ["get"]  # only the start-up status read
    assert text_of(panel, "runpod-result") == "Type a key first, then press Save key."


def test_save_failure_shows_the_store_message_and_keeps_the_field(qtbot, make_panel, client):
    store = FakeStore()
    panel = make_panel(store, client)
    field = panel.findChild(QLineEdit, "runpod-key-input")
    field.setText(FAKE_KEY)
    store.failure = SecretStoreError("keyring save failed (ValueError)")

    press(qtbot, panel, "runpod-save-key")

    assert text_of(panel, "runpod-result") == "keyring save failed (ValueError)"
    assert store.value is None
    assert field.text() == FAKE_KEY


# ----------------------------------------------------------------- remove


def test_remove_reports_that_the_key_was_removed(qtbot, make_panel, client):
    store = FakeStore(FAKE_KEY)
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-remove-key")
    assert store.value is None
    assert text_of(panel, "runpod-key-status") == KEY_MISSING_TEXT
    assert text_of(panel, "runpod-result") == "Key removed from the keychain."


def test_remove_reports_when_nothing_was_stored(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-remove-key")
    assert text_of(panel, "runpod-result") == "No key was stored, so nothing was removed."


def test_remove_failure_shows_the_store_message(qtbot, make_panel, client):
    store = FakeStore(FAKE_KEY)
    panel = make_panel(store, client)
    store.failure = SecretStoreError("keyring delete failed (OSError)")
    press(qtbot, panel, "runpod-remove-key")
    assert text_of(panel, "runpod-result") == "keyring delete failed (OSError)"
    assert store.value == FAKE_KEY


# ----------------------------------------------------------------- test key


def test_test_key_does_not_call_meridian_when_the_server_is_not_running(qtbot, make_panel, store, client):
    client.state = HEALTH_NOT_RUNNING
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert [call[0] for call in client.calls] == ["probe_health"]
    assert text_of(panel, "runpod-result") == "Request not sent: the local Meridian server is not running."
    assert text_of(panel, "runpod-server-status") == (
        "Local Meridian server: not running at http://127.0.0.1:7878. Start Meridian on this computer."
    )


def test_test_key_reports_when_something_else_answers(qtbot, make_panel, store, client):
    client.state = HEALTH_NOT_MERIDIAN
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == "Request not sent: the server at the local address is not Meridian."
    assert text_of(panel, "runpod-server-status") == (
        "Local Meridian server: something answers at http://127.0.0.1:7878, but it is not Meridian."
    )


def test_test_key_reports_a_probe_error(qtbot, make_panel, store, client):
    client.state = HEALTH_ERROR
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == "Request not sent: the local Meridian server could not be checked."
    assert text_of(panel, "runpod-server-status") == (
        "Local Meridian server: could not be checked at http://127.0.0.1:7878."
    )


def test_test_key_reports_success_with_the_key_source(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == (
        "RunPod key works. Key source: keyring. Billing: available. Pods on the first page: 2."
    )
    assert FAKE_KEY not in all_text(panel)


def test_test_key_shows_meridian_failure_text_as_plain_words(qtbot, make_panel, store, client):
    client.check = {
        "ok": False,
        "credential_source": None,
        "pod_count": 0,
        "billing_available": False,
        "error": {"code": "no_credential", "message": "No RunPod API key was found in the sources tried."},
    }
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == (
        "RunPod key check failed: No RunPod API key was found in the sources tried."
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("local_only", "Meridian accepts RunPod requests only from this computer"),
        ("http_404", "This Meridian server has no RunPod routes (HTTP 404)."),
        ("timeout", "The local Meridian server did not answer in time."),
        ("not_running", "The local Meridian server is not running."),
    ],
)
def test_test_key_maps_local_error_codes_to_fixed_text(qtbot, make_panel, store, client, code, expected):
    client.raise_on["runpod_check"] = LocalClientError(code, "SENTINEL-BODY-TEXT")
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    shown = text_of(panel, "runpod-result")
    assert shown.startswith(expected)
    assert "SENTINEL-BODY-TEXT" not in all_text(panel)


def test_test_key_uses_the_local_url_from_settings(qtbot, make_panel, store, client, settings_path):
    panel = make_panel(store, client)
    SettingsStore(settings_path).save(Settings(local_url="http://127.0.0.1:9999"))
    press(qtbot, panel, "runpod-test-key")
    assert client.calls[-1][1] == "http://127.0.0.1:9999"


# ----------------------------------------------------------------- refresh pods


def _cells(table, row):
    return tuple(table.item(row, column).text() for column in range(table.columnCount()))


def test_refresh_fills_the_table_with_the_pod_summaries(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    table = panel.findChild(QTableWidget, "runpod-pods")
    headers = tuple(table.horizontalHeaderItem(column).text() for column in range(table.columnCount()))
    assert headers == POD_COLUMNS
    assert table.rowCount() == 2
    assert _cells(table, 0) == ("pod-a1", "trainer", "RUNNING", "NVIDIA RTX A4000 x1", "0.34", "7200")
    assert _cells(table, 1) == ("pod-b2", "idle-box", "EXITED", "", "0.0", "")
    assert text_of(panel, "runpod-result") == "Listed 2 pod(s)."


def test_refresh_says_when_more_pods_exist(qtbot, make_panel, store, client):
    client.listing["has_more_pods"] = True
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    assert text_of(panel, "runpod-result") == "Listed 2 pod(s). More pods exist than this page shows."


def test_refresh_failure_clears_the_table_and_says_why(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    client.listing = {
        "ok": False,
        "credential_source": None,
        "pods": [],
        "pod_count": 0,
        "has_more_pods": False,
        "error": {"code": "unauthorized", "message": "RunPod rejected the API key (HTTP 401)."},
    }
    press(qtbot, panel, "runpod-refresh-pods")
    assert panel.findChild(QTableWidget, "runpod-pods").rowCount() == 0
    assert text_of(panel, "runpod-result") == "RunPod pod list failed: RunPod rejected the API key (HTTP 401)."


def test_refresh_with_server_down_clears_the_table(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    client.state = HEALTH_NOT_RUNNING
    press(qtbot, panel, "runpod-refresh-pods")
    assert panel.findChild(QTableWidget, "runpod-pods").rowCount() == 0
    assert "not running" in text_of(panel, "runpod-result")


def test_refresh_local_error_clears_the_table(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    client.raise_on["runpod_list_pods"] = LocalClientError("timeout", "slow")
    press(qtbot, panel, "runpod-refresh-pods")
    assert panel.findChild(QTableWidget, "runpod-pods").rowCount() == 0
    assert text_of(panel, "runpod-result") == "The local Meridian server did not answer in time."


def test_pod_names_and_messages_are_plain_text_not_html(qtbot, make_panel, store, client):
    client.listing["pods"] = [dict(POD_A, name="<b>bold</b>")]
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    table = panel.findChild(QTableWidget, "runpod-pods")
    assert table.item(0, 1).text() == "<b>bold</b>"

    client.check = {"ok": False, "error": {"code": "x", "message": "<i>not italic</i>"}}
    press(qtbot, panel, "runpod-test-key")
    result = panel.findChild(QLabel, "runpod-result")
    assert result.textFormat() == Qt.TextFormat.PlainText
    assert result.text() == "RunPod key check failed: <i>not italic</i>"


def test_an_unexpected_exception_is_reported_without_its_text(qtbot, make_panel, store, client):
    client.raise_on["runpod_check"] = RuntimeError(f"leaked {FAKE_KEY}")
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == "Unexpected RuntimeError. Details were not shown."
    assert FAKE_KEY not in all_text(panel)


# ----------------------------------------------------------------- threading and busy state


def test_every_client_call_runs_off_the_ui_thread(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    press(qtbot, panel, "runpod-refresh-pods")
    assert client.threads
    assert all(thread is not threading.main_thread() for thread in client.threads)


def test_buttons_are_disabled_while_a_job_runs(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    client.gate = threading.Event()
    test_button = panel.findChild(QPushButton, "runpod-test-key")

    test_button.click()

    assert panel.busy
    assert not test_button.isEnabled()
    client.gate.set()
    wait_idle(qtbot, panel)
    assert test_button.isEnabled()


def test_idle_is_emitted_when_work_finishes(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    with qtbot.waitSignal(panel.idle, timeout=5000):
        panel.findChild(QPushButton, "runpod-refresh-pods").click()


def test_showing_the_panel_probes_the_server_once(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    panel.show()
    qtbot.waitUntil(lambda: text_of(panel, "runpod-server-status").startswith("Local Meridian server: running"), timeout=5000)
    panel.hide()
    panel.show()
    wait_idle(qtbot, panel)
    assert [call[0] for call in client.calls].count("probe_health") == 1
