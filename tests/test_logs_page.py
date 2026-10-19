"""Tests for the Logs tab (meridian_desktop/ui/logs_page.py), with pytest-qt.

The tunnel log reads real temp files through log_tail.read_tunnel_log_tail, or a fake read. The
runner read runs the real RunnerClient against a fake spawn, or a fake read. Nothing here starts a
process, opens a socket, reads the real home directory or reads the keychain. Every log file is
under pytest's temp directory, and its path is passed in, so the real MERIDIAN_TUNNEL_LOG is never
read. Fake secrets carry a fake marker and match no live token. Offscreen Qt, from conftest.
"""

import json
import subprocess
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QToolButton,
)

from meridian_desktop.log_tail import (
    DEFAULT_MAX_BYTES,
    NO_LOG_YET_TEXT,
    STATUS_NO_LOG_YET,
    STATUS_OK,
    STATUS_UNREADABLE,
    UNREADABLE_TEXT,
    LogTail,
    read_tunnel_log_tail,
)
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, MODULE, RunnerClient, RunnerResult
from meridian_desktop.ui.logs_page import (
    EMPTY_TUNNEL_TEXT,
    REFRESH_BUTTON_TEXT,
    RUNNER_EMPTY_TEXT,
    RUNNER_NOT_AVAILABLE_PREFIX,
    LogsPage,
    runner_log_text,
    runner_tail_not_configured,
)

FAKE_QUERY_TOKEN = "fake-query-0001"
FAKE_BEARER = "sk_meridian_fake-bearer-0002"
FAKE_BARE = "sk_meridian_fake-bare-0003"
FAKE_RUNNER = "sk_meridian_fake-runner-0004"
SECRETS = (FAKE_QUERY_TOKEN, FAKE_BEARER, FAKE_BARE, FAKE_RUNNER)
# Every input control except the one Refresh button, which the button test checks by name.
WRITE_CONTROLS = (
    QToolButton,
    QCheckBox,
    QRadioButton,
    QLineEdit,
    QComboBox,
    QAbstractSpinBox,
)
# Words a clear, delete, rotate or truncate control would carry, and the runner's mutating verbs.
FORBIDDEN_BUTTON_WORDS = ("clear", "delete", "rotate", "truncate", "remove", "start", "stop", "restart")


class Recorder:
    """A fake read. It records each call and the thread it ran on, and returns a fixed value or raises.

    ``values`` (a list) is used one item per call, in order, when given. If ``gate`` is set, the
    call waits on it first, so a test can hold a read open.
    """

    def __init__(self, value=None, error=None, gate=None, values=None):
        self.value = value
        self.error = error
        self.gate = gate
        self.values = list(values) if values is not None else None
        self.calls = 0
        self.threads = []

    def __call__(self, *args):
        self.calls += 1
        self.threads.append(threading.current_thread())
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        if self.values is not None:
            return self.values.pop(0)
        return self.value


def _tail_ok(lines, path="tunnel.log"):
    return LogTail(STATUS_OK, path, tuple(lines), "")


def _runner_ok(log_tail):
    return RunnerResult(
        verb="tail-log", ok=True, payload={"scope": "scope-a", "log_tail": log_tail}, exit_code=0
    )


def _page(qtbot, *, tunnel, runner=None):
    page = LogsPage(tunnel_log=tunnel, runner=runner if runner is not None else Recorder(_runner_ok("")))
    qtbot.addWidget(page)
    return page


def _press(qtbot, page):
    """Press Refresh and wait until both reads finish and the button is enabled again."""
    button = page.findChild(QPushButton, "logs-refresh")
    button.click()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)


def _text(page, name):
    return page.findChild(QPlainTextEdit, name).toPlainText()


def _label(page, name):
    return page.findChild(QLabel, name).text()


def _all_visible_text(page):
    """Every string a user can read on the page: labels, and the text in each pane."""
    chunks = [label.text() for label in page.findChildren(QLabel)]
    chunks += [edit.toPlainText() for edit in page.findChildren(QPlainTextEdit)]
    return "\n".join(chunks)


def _write_log(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def test_logs_page_is_read_only_and_plain_text(qtbot):
    tunnel = Recorder(_tail_ok(["<b>bold</b> tunnel line"]))
    runner = Recorder(_runner_ok("<i>runner</i> line"))
    page = _page(qtbot, tunnel=tunnel, runner=runner)

    buttons = page.findChildren(QPushButton)
    assert [button.objectName() for button in buttons] == ["logs-refresh"]
    assert buttons[0].text() == REFRESH_BUTTON_TEXT
    for word in FORBIDDEN_BUTTON_WORDS:
        assert word not in buttons[0].text().lower()
    for widget_type in WRITE_CONTROLS:
        assert page.findChildren(widget_type) == [], widget_type.__name__
    for pane in page.findChildren(QPlainTextEdit):
        assert pane.isReadOnly(), pane.objectName()

    _press(qtbot, page)

    assert [button.objectName() for button in page.findChildren(QPushButton)] == ["logs-refresh"]
    assert page.findChildren(QTimer) == []
    for pane in page.findChildren(QPlainTextEdit):
        assert pane.isReadOnly(), pane.objectName()
    # The markup stays as typed. Each pane holds plain text, and no label is read as HTML.
    assert _text(page, "logs-tunnel-text") == "<b>bold</b> tunnel line"
    assert _text(page, "logs-runner-text") == "<i>runner</i> line"
    for label in page.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()


def test_nothing_is_read_until_refresh_is_pressed(qtbot):
    tunnel = Recorder(_tail_ok(["line"]))
    runner = Recorder(_runner_ok("line"))
    page = _page(qtbot, tunnel=tunnel, runner=runner)

    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(200)

    assert tunnel.calls == 0
    assert runner.calls == 0
    assert _label(page, "logs-tunnel-heading") == "Tunnel log: not read yet."
    assert _label(page, "logs-runner-heading") == "Runner log (tail-log): not read yet."
    assert _text(page, "logs-tunnel-text") == ""
    assert _text(page, "logs-runner-text") == ""


def test_both_panes_show_the_bounded_tunnel_tail_and_the_runner_tail(qtbot, tmp_path):
    path = tmp_path / "tunnel.log"
    # Far more than the default budget, so only the end of the file is read.
    lines = [f"tunnel-line-{index:07d} " + "x" * 40 for index in range(4000)]
    _write_log(path, "\n".join(lines) + "\n")
    page = _page(
        qtbot,
        tunnel=lambda: read_tunnel_log_tail(path),
        runner=Recorder(_runner_ok("runner-one\nrunner-two")),
    )

    _press(qtbot, page)

    shown = _text(page, "logs-tunnel-text")
    assert "tunnel-line-0003999" in shown
    assert "tunnel-line-0000000" not in shown
    assert len(shown.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert _text(page, "logs-runner-text") == "runner-one\nrunner-two"
    assert _label(page, "logs-tunnel-heading") == f"Tunnel log: {path}"


def test_missing_tunnel_log_shows_no_log_yet(qtbot, tmp_path):
    missing = tmp_path / "never-written" / "tunnel.log"
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(missing))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == NO_LOG_YET_TEXT
    assert "not available" not in _text(page, "logs-tunnel-text").lower()


def test_unreadable_tunnel_log_shows_fixed_text_without_the_error(qtbot, tmp_path):
    # A directory cannot be opened as a file, so the reader reports the log as unreadable.
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(tmp_path))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == UNREADABLE_TEXT
    assert str(tmp_path) not in _text(page, "logs-tunnel-text")


def test_empty_tunnel_log_says_it_is_empty(qtbot, tmp_path):
    path = tmp_path / "tunnel.log"
    _write_log(path, "")
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(path))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == EMPTY_TUNNEL_TEXT


def test_tunnel_log_secrets_are_redacted_on_screen(qtbot, tmp_path):
    path = tmp_path / "tunnel.log"
    _write_log(
        path,
        f"connect https://example.test/mcp?token={FAKE_QUERY_TOKEN}\n"
        f"Authorization: Bearer {FAKE_BEARER}\n"
        f"key {FAKE_BARE} in use\n",
    )
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(path))

    _press(qtbot, page)

    shown = _all_visible_text(page)
    for secret in SECRETS[:3]:
        assert secret not in shown
    assert "token=[redacted]" in shown
    assert "Bearer [redacted]" in shown
    assert "[redacted] in use" in shown


def test_runner_output_gets_the_same_redaction_as_the_tunnel_log(qtbot):
    runner_text = (
        f"start ?token={FAKE_QUERY_TOKEN}\n"
        f"Bearer {FAKE_BEARER}\n"
        f"{FAKE_RUNNER} is the key\n"
        "plain line\n"
    )
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])), runner=Recorder(_runner_ok(runner_text)))

    _press(qtbot, page)

    shown = _text(page, "logs-runner-text")
    for secret in SECRETS:
        assert secret not in shown
    assert shown == (
        "start ?token=[redacted]\n"
        "Bearer [redacted]\n"
        "[redacted] is the key\n"
        "plain line"
    )


def test_page_redacts_a_tunnel_source_that_skipped_the_reader(qtbot):
    # A source that does not go through log_tail still reaches the screen redacted.
    page = _page(qtbot, tunnel=Recorder(_tail_ok([f"raw {FAKE_BARE}"])))

    _press(qtbot, page)

    assert FAKE_BARE not in _text(page, "logs-tunnel-text")
    assert "raw [redacted]" in _text(page, "logs-tunnel-text")


def test_runner_not_configured_reads_not_available_and_starts_nothing(qtbot):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("no process should start")

    client = RunnerClient(spawn=spawn)  # no interpreter configured
    page = LogsPage(tunnel_log=Recorder(_tail_ok([])), runner=lambda: client.tail_log("scope-a"))
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert spawned == []
    assert _text(page, "logs-runner-text") == (
        "Runner log not available. The Meridian runner command is not available on this computer."
    )


def test_default_runner_read_is_not_configured(qtbot):
    page = LogsPage(tunnel_log=Recorder(_tail_ok([])))
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == runner_log_text(runner_tail_not_configured())
    assert _text(page, "logs-runner-text").startswith(RUNNER_NOT_AVAILABLE_PREFIX)


def test_runner_pane_passes_only_the_tail_log_verb(qtbot):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        body = json.dumps({"scope": "scope-a", "log_tail": "runner line"}).encode("utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=body, stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = LogsPage(tunnel_log=Recorder(_tail_ok([])), runner=lambda: client.tail_log("scope-a"))
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert [argv[3] for argv in spawned] == ["tail-log"]
    assert spawned[0][:3] == ["core-python", "-m", MODULE]
    assert _text(page, "logs-runner-text") == "runner line"


def test_runner_empty_tail_reads_no_log_yet(qtbot):
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])), runner=Recorder(_runner_ok("")))

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == RUNNER_EMPTY_TEXT


def test_runner_reply_without_a_log_tail_reads_not_available(qtbot):
    reply = RunnerResult(verb="tail-log", ok=True, payload={"scope": "scope-a"}, exit_code=0)
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])), runner=Recorder(reply))

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == (
        "Runner log not available. The runner reply did not include a log tail."
    )


def test_runner_error_text_is_shown_as_the_runner_client_wrote_it(qtbot):
    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout=b'{"scope": "scope-a"}', stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = LogsPage(tunnel_log=Recorder(_tail_ok([])), runner=lambda: client.tail_log("scope-a"))
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == "Runner log not available. The runner command exited with code 3."


def test_runner_log_text_for_a_failed_result_uses_its_fixed_message():
    result = RunnerResult(verb="tail-log", ok=False, error_code=ERR_NOT_AVAILABLE, error_message="fixed text")

    assert runner_log_text(result) == "Runner log not available. fixed text"


def test_refresh_reads_again_on_each_press(qtbot):
    tunnel = Recorder(values=[_tail_ok(["first read"]), _tail_ok(["second read"])])
    runner = Recorder(values=[_runner_ok("one"), _runner_ok("two")])
    page = _page(qtbot, tunnel=tunnel, runner=runner)

    _press(qtbot, page)
    assert _text(page, "logs-tunnel-text") == "first read"

    _press(qtbot, page)
    assert _text(page, "logs-tunnel-text") == "second read"
    assert _text(page, "logs-runner-text") == "two"
    assert tunnel.calls == 2
    assert runner.calls == 2


def test_reads_run_off_the_ui_thread(qtbot):
    tunnel = Recorder(_tail_ok([]))
    runner = Recorder(_runner_ok(""))
    page = _page(qtbot, tunnel=tunnel, runner=runner)

    _press(qtbot, page)

    for read in (tunnel, runner):
        assert read.threads and all(thread is not threading.main_thread() for thread in read.threads)


def test_refresh_button_is_disabled_while_reading(qtbot):
    gate = threading.Event()
    tunnel = Recorder(_tail_ok([]), gate=gate)
    page = _page(qtbot, tunnel=tunnel)
    button = page.findChild(QPushButton, "logs-refresh")

    button.click()

    assert page.busy
    assert not button.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)
    assert tunnel.calls == 1


def test_a_failed_read_shows_its_class_name_and_the_other_pane_still_shows(qtbot):
    secret = "SECRET-IN-A-READ-ERROR"
    page = _page(
        qtbot,
        tunnel=Recorder(error=RuntimeError(secret)),
        runner=Recorder(_runner_ok("runner still shows")),
    )

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == "The tunnel log check failed unexpectedly (RuntimeError)."
    assert _text(page, "logs-runner-text") == "runner still shows"
    assert secret not in _all_visible_text(page)


def test_a_failed_runner_read_does_not_hide_the_tunnel_log(qtbot):
    secret = "SECRET-RUNNER-READ-ERROR"
    page = _page(qtbot, tunnel=Recorder(_tail_ok(["tunnel still shows"])), runner=Recorder(error=OSError(secret)))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == "tunnel still shows"
    assert _text(page, "logs-runner-text") == "The runner log check failed unexpectedly (OSError)."
    assert secret not in _all_visible_text(page)


def test_an_unexpected_failure_of_the_whole_pass_is_reported_without_its_text(qtbot):
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])))

    def broken_pass():
        raise KeyError("SECRET-KEY-TEXT")

    page._collect = broken_pass
    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == "The logs check failed unexpectedly (KeyError)."
    assert "SECRET-KEY-TEXT" not in _all_visible_text(page)


def test_refresh_never_changes_the_logs_on_disk(qtbot, tmp_path):
    path = tmp_path / "tunnel.log"
    backup = tmp_path / "tunnel.log.1"
    _write_log(path, "alpha\nbeta\n")
    _write_log(backup, "older\n")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(path))

    _press(qtbot, page)
    _press(qtbot, page)

    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert after == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tunnel.log", "tunnel.log.1"]
    assert "alpha" in _text(page, "logs-tunnel-text")


def test_status_values_map_to_their_fixed_text(qtbot):
    """Each status the reader can return has its own visible text, and none shows an exception."""
    cases = [
        (LogTail(STATUS_NO_LOG_YET, "x", (), NO_LOG_YET_TEXT), NO_LOG_YET_TEXT),
        (LogTail(STATUS_UNREADABLE, "x", (), UNREADABLE_TEXT), UNREADABLE_TEXT),
    ]
    for tail, expected in cases:
        page = _page(qtbot, tunnel=Recorder(tail))
        _press(qtbot, page)
        assert _text(page, "logs-tunnel-text") == expected
