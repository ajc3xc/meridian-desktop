"""Tests for the Services tab (meridian_desktop/ui/services_page.py), with pytest-qt.

The three checks are fakes here, except where a test says otherwise. The runner test runs the
real RunnerClient against a fake spawn, and the slot port test runs the real probe_slot_ports
against a fake connect check. Nothing starts a process, opens a socket or reads the keychain.
The settings file is a temp file. Offscreen Qt, from conftest.
"""

import subprocess
import threading

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QToolButton,
)

from meridian_desktop import local_client
from meridian_desktop.runner_client import MUTATING_VERBS, MODULE, RunnerClient, RunnerResult
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.slot_ports import PER_PORT_TIMEOUT, PROBE_HOST, SLOT_PORTS, SlotPortResult, probe_slot_ports
from meridian_desktop.ui import services_page
from meridian_desktop.ui.services_page import (
    CHECK_BUTTON_TEXT,
    HealthFacts,
    ServicesPage,
)

LOOPBACK_URL = "http://127.0.0.1:7999"
# Every input control except the one check button, which the button test checks by name.
WRITE_CONTROLS = (
    QToolButton,
    QCheckBox,
    QRadioButton,
    QLineEdit,
    QComboBox,
    QAbstractSpinBox,
)


class Recorder:
    """A fake check. It records each call and the thread it ran on, and returns a fixed value or raises.

    If ``gate`` is set, the call waits on it first, so a test can hold a check open.
    """

    def __init__(self, value=None, error=None, gate=None):
        self.value = value
        self.error = error
        self.gate = gate
        self.calls = []
        self.threads = []

    def __call__(self, *args):
        self.calls.append(args)
        self.threads.append(threading.current_thread())
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.value


def _runner_ok(state="running"):
    return RunnerResult(verb="status", ok=True, payload={"scope": "scope-a", "child": {"state": state}}, exit_code=0)


def _ports(listening=("fs", "code")):
    return [SlotPortResult(slot, port, slot in listening) for slot, port in SLOT_PORTS]


def _page(qtbot, settings_path, *, health=None, runner=None, ports=None):
    page = ServicesPage(
        SettingsStore(settings_path),
        health=health if health is not None else Recorder(HealthFacts(state="running")),
        runner=runner if runner is not None else Recorder(_runner_ok()),
        ports=ports if ports is not None else Recorder(_ports()),
    )
    qtbot.addWidget(page)
    return page


def _press(qtbot, page):
    """Press Check services and wait until the checks finish and the button is enabled again."""
    button = page.findChild(QPushButton, "services-check")
    button.click()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)


def _text(page, name):
    return page.findChild(QLabel, name).text()


def _all_label_text(page):
    return "\n".join(label.text() for label in page.findChildren(QLabel))


def test_services_tab_shows_probe_states(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(local_url=LOOPBACK_URL))
    health = Recorder(HealthFacts(state="running", engine_version="1.4.2", git_sha="a555660ef312"))
    page = _page(qtbot, settings_path, health=health, runner=Recorder(_runner_ok("running")), ports=Recorder(_ports()))

    _press(qtbot, page)

    assert _text(page, "services-health") == f"Local server health ({LOOPBACK_URL}): running"
    assert _text(page, "services-engine-version") == "Engine version: 1.4.2"
    assert _text(page, "services-git-sha") == "Git SHA: a555660ef312"
    assert _text(page, "services-runner") == "Runner child state: running"
    table = page.findChild(QTableWidget, "services-ports-table")
    assert table.rowCount() == 14
    rows = [[table.item(row, column).text() for column in range(3)] for row in range(table.rowCount())]
    assert rows[0] == ["fs", "8808", "listening"]
    assert rows[1] == ["code", "8809", "listening"]
    assert rows[2] == ["extract", "8810", "not listening"]
    assert [row[1] for row in rows] == [str(port) for _slot, port in SLOT_PORTS]


@pytest.mark.parametrize("state", ["running", "not_running", "not_meridian", "error"])
def test_health_state_is_shown_as_the_probe_printed_it(qtbot, settings_path, state):
    SettingsStore(settings_path).save(Settings(local_url=LOOPBACK_URL))
    page = _page(qtbot, settings_path, health=Recorder(HealthFacts(state=state)))

    _press(qtbot, page)

    assert _text(page, "services-health") == f"Local server health ({LOOPBACK_URL}): {state}"
    # The probe reported no version or SHA, so both read not available.
    assert _text(page, "services-engine-version") == "Engine version: not available"
    assert _text(page, "services-git-sha") == "Git SHA: not available"


def test_engine_version_and_sha_read_not_available_from_the_real_state_probe(qtbot, settings_path, monkeypatch):
    """The default health check uses local_client.probe_health, which reports the state only."""
    monkeypatch.setattr(local_client, "probe_health", lambda url, timeout: "running")
    page = ServicesPage(SettingsStore(settings_path), ports=Recorder(_ports()), runner=Recorder(_runner_ok()))
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "services-health").endswith("): running")
    assert _text(page, "services-engine-version") == "Engine version: not available"
    assert _text(page, "services-git-sha") == "Git SHA: not available"


def test_default_health_check_sends_nothing_to_a_non_loopback_address(monkeypatch):
    def no_request(*args, **kwargs):
        raise AssertionError("a request was opened")

    monkeypatch.setattr(local_client, "_opener", no_request)

    facts = services_page.probe_health_facts("http://example.com:7878")

    assert facts == HealthFacts(state="error")


def test_health_check_uses_the_local_url_from_settings(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(local_url=LOOPBACK_URL))
    health = Recorder(HealthFacts(state="running"))
    page = _page(qtbot, settings_path, health=health)

    _press(qtbot, page)

    assert health.calls == [(LOOPBACK_URL, services_page.HEALTH_TIMEOUT)]


def test_services_page_has_no_mutating_controls(qtbot, settings_path):
    page = _page(qtbot, settings_path)

    buttons = page.findChildren(QPushButton)
    assert [button.objectName() for button in buttons] == ["services-check"]
    assert buttons[0].text() == CHECK_BUTTON_TEXT
    for verb in MUTATING_VERBS:
        assert verb not in buttons[0].text().lower()
    for widget_type in WRITE_CONTROLS:
        assert page.findChildren(widget_type) == [], widget_type.__name__

    _press(qtbot, page)

    assert [button.objectName() for button in page.findChildren(QPushButton)] == ["services-check"]
    assert page.findChildren(QTimer) == []


def test_checks_run_only_when_the_button_is_pressed(qtbot, settings_path):
    health = Recorder(HealthFacts(state="running"))
    runner = Recorder(_runner_ok())
    ports = Recorder(_ports())
    page = _page(qtbot, settings_path, health=health, runner=runner, ports=ports)

    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(200)  # give any work started on show a chance to run

    assert health.calls == []
    assert runner.calls == []
    assert ports.calls == []
    assert _text(page, "services-health").endswith("not checked yet")

    _press(qtbot, page)

    assert len(health.calls) == 1
    assert len(runner.calls) == 1
    assert len(ports.calls) == 1


def test_checks_run_off_the_ui_thread(qtbot, settings_path):
    health = Recorder(HealthFacts(state="running"))
    runner = Recorder(_runner_ok())
    ports = Recorder(_ports())
    page = _page(qtbot, settings_path, health=health, runner=runner, ports=ports)

    _press(qtbot, page)

    for check in (health, runner, ports):
        assert check.threads and all(thread is not threading.main_thread() for thread in check.threads)


def test_button_is_disabled_while_checks_run(qtbot, settings_path):
    gate = threading.Event()
    health = Recorder(HealthFacts(state="running"), gate=gate)
    page = _page(qtbot, settings_path, health=health)
    button = page.findChild(QPushButton, "services-check")

    button.click()

    assert page.busy
    assert not button.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)
    assert len(health.calls) == 1


def test_one_failed_check_does_not_hide_the_others(qtbot, settings_path):
    secret = "SECRET-IN-AN-ERROR-MESSAGE"
    page = _page(
        qtbot,
        settings_path,
        health=Recorder(HealthFacts(state="running")),
        runner=Recorder(error=RuntimeError(secret)),
        ports=Recorder(_ports()),
    )

    _press(qtbot, page)

    assert _text(page, "services-runner") == "The runner check failed unexpectedly (RuntimeError)."
    assert _text(page, "services-health").endswith("): running")
    assert page.findChild(QTableWidget, "services-ports-table").rowCount() == 14
    assert secret not in _all_label_text(page)


def test_runner_check_passes_only_the_status_verb(qtbot, settings_path):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"scope": "scope-a", "child": {"state": "running"}}', stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = _page(qtbot, settings_path, runner=lambda: client.status("scope-a"))

    _press(qtbot, page)

    assert [argv[3] for argv in spawned] == ["status"]
    assert spawned[0][:3] == ["core-python", "-m", MODULE]
    assert _text(page, "services-runner") == "Runner child state: running"


def test_runner_check_starts_nothing_without_an_interpreter(qtbot, settings_path):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("no process should start")

    client = RunnerClient(spawn=spawn)  # no interpreter configured
    page = _page(qtbot, settings_path, runner=lambda: client.status("scope-a"))

    _press(qtbot, page)

    assert spawned == []
    assert _text(page, "services-runner") == (
        "Runner child state: not available. The Meridian runner command is not available on this computer."
    )


def test_runner_reads_not_available_when_no_interpreter_is_configured(qtbot, settings_path):
    page = ServicesPage(SettingsStore(settings_path), health=Recorder(HealthFacts(state="running")), ports=Recorder(_ports()))
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "services-runner").startswith("Runner child state: not available.")


def test_runner_error_text_is_shown_as_the_runner_client_wrote_it(qtbot, settings_path):
    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout=b'{"scope": "scope-a"}', stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = _page(qtbot, settings_path, runner=lambda: client.status("scope-a"))

    _press(qtbot, page)

    assert _text(page, "services-runner") == (
        "Runner child state: not available. The runner command exited with code 3."
    )


def test_slot_ports_come_from_the_real_probe_with_a_fake_connect(qtbot, settings_path):
    calls = []

    def fake_connect(host, port, timeout):
        calls.append((host, port, timeout))
        return port == 8820  # outputs

    page = _page(qtbot, settings_path, ports=lambda: probe_slot_ports(check=fake_connect))

    _press(qtbot, page)

    assert calls == [(PROBE_HOST, port, PER_PORT_TIMEOUT) for _slot, port in SLOT_PORTS]
    table = page.findChild(QTableWidget, "services-ports-table")
    states = {table.item(row, 0).text(): table.item(row, 2).text() for row in range(table.rowCount())}
    assert states["outputs"] == "listening"
    assert states["fs"] == "not listening"


def test_labels_are_plain_text(qtbot, settings_path):
    page = _page(qtbot, settings_path, health=Recorder(HealthFacts(state="<b>running</b>")))

    _press(qtbot, page)

    health = page.findChild(QLabel, "services-health")
    assert health.text().endswith("): <b>running</b>")
    for label in page.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_page_shows_no_results_before_the_first_press(qtbot, settings_path):
    page = _page(qtbot, settings_path)

    assert _text(page, "services-health") == "Local server health: not checked yet"
    assert _text(page, "services-runner") == "Runner child state: not checked yet"
    assert page.findChild(QTableWidget, "services-ports-table").rowCount() == 0


FAILURE_CASES = [
    ("health", "services-health", "The local server health check failed unexpectedly (ValueError)."),
    ("runner", "services-runner", "The runner check failed unexpectedly (ValueError)."),
    ("ports", "services-ports-status", "The slot port check failed unexpectedly (ValueError)."),
]


@pytest.mark.parametrize(("failing", "label", "expected"), FAILURE_CASES)
def test_each_failed_check_shows_its_own_failure_and_the_others_still_show(
    qtbot, settings_path, failing, label, expected
):
    secret = f"SECRET-IN-{failing}-MESSAGE"
    checks = {
        "health": Recorder(HealthFacts(state="running")),
        "runner": Recorder(_runner_ok()),
        "ports": Recorder(_ports()),
    }
    checks[failing] = Recorder(error=ValueError(secret))
    page = _page(qtbot, settings_path, health=checks["health"], runner=checks["runner"], ports=checks["ports"])

    _press(qtbot, page)

    assert _text(page, label) == expected
    assert secret not in _all_label_text(page)
    if failing != "health":
        assert _text(page, "services-health").endswith("): running")
    if failing != "runner":
        assert _text(page, "services-runner") == "Runner child state: running"
    if failing != "ports":
        assert page.findChild(QTableWidget, "services-ports-table").rowCount() == 14
    if failing == "health":
        assert _text(page, "services-engine-version") == "Engine version: not available"
        assert _text(page, "services-git-sha") == "Git SHA: not available"


def test_an_unexpected_failure_of_the_whole_pass_is_reported_without_its_text(qtbot, settings_path):
    page = _page(qtbot, settings_path)

    def broken_pass():
        raise KeyError("SECRET-KEY-TEXT")

    page._collect = broken_pass
    _press(qtbot, page)

    assert _text(page, "services-health") == "The services check failed unexpectedly (KeyError)."
    assert "SECRET-KEY-TEXT" not in _all_label_text(page)
    assert page.findChild(QTableWidget, "services-ports-table").rowCount() == 0


def test_a_runner_reply_without_a_child_state_reads_not_available(qtbot, settings_path):
    reply = RunnerResult(verb="status", ok=True, payload={"scope": "scope-a"}, exit_code=0)
    page = _page(qtbot, settings_path, runner=Recorder(reply))

    _press(qtbot, page)

    assert _text(page, "services-runner") == (
        "Runner child state: not available. The runner reply did not include a child state."
    )
