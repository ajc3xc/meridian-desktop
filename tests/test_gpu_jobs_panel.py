"""Tests for the GPUs tab (meridian_desktop/ui/gpu_jobs_panel.py), with pytest-qt. Offscreen Qt, no network, no process.

The runner, the host list and the RunPod list are fakes. Each fake records its calls and the thread it ran on, so a test
can prove a read runs off the UI thread, nothing runs before the tab is shown, and a result for another tab is dropped.
"""

import socket
import subprocess
import threading
from datetime import datetime, timezone

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
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

from _shared_delivery import emit, recording_deliveries
from meridian_desktop import gpu_jobs
from meridian_desktop.gpu_jobs import (
    BJOBS_ARGV,
    NVIDIA_APPS_ARGV,
    NVIDIA_GPU_ARGV,
    SQUEUE_ARGV,
    CommandResult,
    GpuSnapshot,
)
from meridian_desktop.ui.delivery import Delivered
from meridian_desktop.ui.gpu_jobs_panel import (
    CHECKED_PREFIX,
    GPU_COLUMNS,
    JOB_COLUMNS,
    LOADING_TEXT,
    NOT_READ_TEXT,
    REFRESH_MS,
    STATUS_COLUMNS,
    GpuJobsPanel,
)

NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
REAL_GPU_LINE = "0, NVIDIA GeForce RTX 3080, GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d, 79, 1734, 20480, 59, 272.21, 320.00\n"
REAL_APPS_LINE = "4, [Insufficient Permissions], [N/A], GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d\n"
RUNPOD = {"ok": True, "pods": [{"id": "pod-1", "name": "trainer", "status": "RUNNING", "gpu": {"id": "A100", "count": 1}}]}


class RecordingRunner:
    """A fake runner. ``gate`` is an optional Event the runner waits on, so a test can hold a read in progress."""

    def __init__(self, answers=None, gate=None, error=None):
        self._answers = answers if answers is not None else {
            NVIDIA_GPU_ARGV: CommandResult(0, REAL_GPU_LINE),
            NVIDIA_APPS_ARGV: CommandResult(0, REAL_APPS_LINE),
            SQUEUE_ARGV: CommandResult(None, missing=True),
            BJOBS_ARGV: CommandResult(None, missing=True),
        }
        self._gate = gate
        self._error = error
        self.calls = 0
        self.threads = []
        self._lock = threading.Lock()

    def __call__(self, argv, timeout):
        with self._lock:
            self.threads.append(threading.get_ident())
            if tuple(argv) in (NVIDIA_GPU_ARGV,):
                self.calls += 1
        if self._gate is not None:
            self._gate.wait(5)
        if self._error is not None:
            raise self._error
        return self._answers.get(tuple(argv), CommandResult(None, missing=True))


def make_panel(qtbot, *, run=None, runpod=lambda: RUNPOD, hosts=lambda: ()):
    panel = GpuJobsPanel(hosts, runpod, run=run if run is not None else RecordingRunner())
    qtbot.addWidget(panel)
    return panel


def table(panel, name):
    return panel.findChild(QTableWidget, name)


def label(panel, name):
    return panel.findChild(QLabel, name)


def button(panel):
    return panel.findChild(QPushButton, "gpu-refresh")


def wait_checked(qtbot, panel):
    qtbot.waitUntil(lambda: label(panel, "gpu-status").text().startswith(CHECKED_PREFIX), timeout=5000)


# ----------------------------------------------------------------- when reads happen


def test_nothing_is_read_when_the_tab_is_built(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    assert run.calls == 0
    assert run.threads == []
    assert label(panel, "gpu-status").text() == NOT_READ_TEXT
    assert table(panel, "gpu-gpus").rowCount() == 0


def test_showing_the_tab_reads_once_and_fills_the_tables(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.calls == 1
    assert table(panel, "gpu-gpus").rowCount() == 1
    assert table(panel, "gpu-gpus").item(0, 2).text() == "NVIDIA GeForce RTX 3080"
    assert label(panel, "gpu-summary").text() == "1 GPU on 1 host. 1 busy. 1.7 of 20.0 GB GPU memory in use."
    jobs = table(panel, "gpu-jobs")
    assert jobs.rowCount() == 2
    assert jobs.item(0, 0).text() == "GPU process"
    assert jobs.item(1, 0).text() == "RunPod"
    statuses = table(panel, "gpu-statuses")
    assert statuses.rowCount() >= 4
    assert label(panel, "gpu-gpus-empty").isHidden()
    assert label(panel, "gpu-jobs-empty").isHidden()


def test_the_read_runs_off_the_ui_thread(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.threads
    assert all(thread != threading.get_ident() for thread in run.threads)


def test_the_saved_hosts_are_read_on_the_worker_thread_too(qtbot):
    seen = []

    def hosts():
        seen.append(threading.get_ident())
        return ()

    panel = make_panel(qtbot, hosts=hosts)
    panel.show()
    wait_checked(qtbot, panel)
    assert seen and all(thread != threading.get_ident() for thread in seen)


def test_refresh_reads_again_and_the_button_is_off_while_a_read_runs(qtbot):
    gate = threading.Event()
    run = RecordingRunner(gate=gate)
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: run.calls == 1, timeout=5000)
    assert button(panel).isEnabled() is False
    assert label(panel, "gpu-status").text() == LOADING_TEXT
    # A press while a read runs starts nothing.
    button(panel).click()
    assert run.calls == 1
    gate.set()
    qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)
    wait_checked(qtbot, panel)
    button(panel).click()
    qtbot.waitUntil(lambda: run.calls == 2, timeout=5000)
    wait_checked(qtbot, panel)


def test_a_timer_reads_only_while_the_tab_is_visible(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    timer = panel.findChild(QTimer)
    assert timer is not None
    assert timer.interval() == REFRESH_MS
    assert timer.isActive() is False
    panel.show()
    assert timer.isActive() is True
    wait_checked(qtbot, panel)
    panel.hide()
    assert timer.isActive() is False


def test_a_tick_on_an_idle_tab_starts_a_read(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.calls == 1
    panel.findChild(QTimer).timeout.emit()
    qtbot.waitUntil(lambda: run.calls == 2, timeout=5000)
    wait_checked(qtbot, panel)


def test_refresh_while_a_read_runs_starts_nothing(qtbot):
    gate = threading.Event()
    run = RecordingRunner(gate=gate)
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: run.calls == 1, timeout=5000)
    panel.refresh()
    assert run.calls == 1
    gate.set()
    wait_checked(qtbot, panel)


def test_a_tick_while_a_read_runs_starts_nothing(qtbot):
    gate = threading.Event()
    run = RecordingRunner(gate=gate)
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: run.calls == 1, timeout=5000)
    panel.findChild(QTimer).timeout.emit()
    assert run.calls == 1
    gate.set()
    wait_checked(qtbot, panel)


def test_a_result_for_another_tab_is_dropped(qtbot):
    panel = make_panel(qtbot)
    other = gpu_jobs.GpuSnapshot(checked_at=NOW, statuses=(gpu_jobs.SourceStatus("x", "GPUs", "ok", "from elsewhere"),))
    with recording_deliveries() as emitted:
        emit(Delivered(object(), other))
    assert emitted  # the delivery did go out, to every page
    assert table(panel, "gpu-statuses").rowCount() == 0
    assert label(panel, "gpu-status").text() == NOT_READ_TEXT


def test_a_failed_read_says_so_plainly_and_the_button_comes_back(qtbot):
    secret = "SECRET-RUNNER-DETAIL"
    run = RecordingRunner(error=RuntimeError(secret))
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)
    statuses = table(panel, "gpu-statuses")
    assert statuses.rowCount() == 1
    assert statuses.item(0, 2).text() == "The GPU read failed. Press Refresh to try again."
    visible = " ".join(label(panel, name).text() for name in ("gpu-status", "gpu-summary"))
    assert secret not in visible
    assert secret not in statuses.item(0, 2).text()


def test_an_empty_read_shows_the_empty_lines_and_no_rows(qtbot):
    run = RecordingRunner(
        answers={
            NVIDIA_GPU_ARGV: CommandResult(0, ""),
            NVIDIA_APPS_ARGV: CommandResult(0, ""),
            SQUEUE_ARGV: CommandResult(None, missing=True),
            BJOBS_ARGV: CommandResult(None, missing=True),
        }
    )
    panel = make_panel(qtbot, run=run, runpod=None)
    panel.show()
    wait_checked(qtbot, panel)
    assert table(panel, "gpu-gpus").rowCount() == 0
    assert not label(panel, "gpu-gpus-empty").isHidden()
    assert not label(panel, "gpu-jobs-empty").isHidden()
    assert label(panel, "gpu-summary").text() == "No GPUs were read."


def test_runpod_not_wired_is_shown_as_not_connected(qtbot):
    panel = make_panel(qtbot, runpod=None)
    panel.show()
    wait_checked(qtbot, panel)
    statuses = table(panel, "gpu-statuses")
    texts = [statuses.item(row, 2).text() for row in range(statuses.rowCount())]
    assert gpu_jobs.RUNPOD_NOT_CONNECTED_TEXT in texts


def test_a_runpod_provider_that_raises_is_not_connected(qtbot):
    def unreachable():
        raise OSError("connection refused")

    panel = make_panel(qtbot, runpod=unreachable)
    panel.show()
    wait_checked(qtbot, panel)
    statuses = table(panel, "gpu-statuses")
    texts = [statuses.item(row, 2).text() for row in range(statuses.rowCount())]
    assert gpu_jobs.RUNPOD_NOT_CONNECTED_TEXT in texts
    assert all("refused" not in text for text in texts)


def panel_owner(qtbot, panel):
    """The owner token this panel sends with its reads, taken from one read it has started."""
    with recording_deliveries() as emitted:
        panel.show()
        wait_checked(qtbot, panel)
    return emitted[0].owner


def test_a_non_delivery_object_is_ignored(qtbot):
    panel = make_panel(qtbot)
    owner = panel_owner(qtbot, panel)
    emit("not a delivery")
    emit(Delivered(owner, "not a snapshot"))
    assert label(panel, "gpu-status").text().startswith(CHECKED_PREFIX)
    assert table(panel, "gpu-statuses").rowCount() > 0


def test_a_snapshot_for_this_tab_fills_the_tables_from_its_own_rows(qtbot):
    panel = make_panel(qtbot)
    owner = panel_owner(qtbot, panel)
    snapshot = GpuSnapshot(
        checked_at=NOW,
        gpus=gpu_jobs.parse_gpus(REAL_GPU_LINE, "gpu-box")[0],
        statuses=(gpu_jobs.SourceStatus("gpu-box", "GPUs", "ok", "1 GPU read."),),
    )
    emit(Delivered(owner, snapshot))
    assert table(panel, "gpu-gpus").item(0, 0).text() == "gpu-box"
    assert label(panel, "gpu-status").text().startswith(CHECKED_PREFIX)


# ----------------------------------------------------------------- read-only and no sockets


def test_the_tab_has_one_control_and_no_write_controls(qtbot):
    panel = make_panel(qtbot)
    buttons = panel.findChildren(QPushButton)
    assert [widget.objectName() for widget in buttons] == ["gpu-refresh"]
    for widget_type in (QToolButton, QCheckBox, QRadioButton, QLineEdit, QComboBox, QAbstractSpinBox):
        assert panel.findChildren(widget_type) == [], widget_type.__name__


def test_every_table_is_read_only_and_its_cells_are_not_editable(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    for name, columns in (("gpu-gpus", GPU_COLUMNS), ("gpu-jobs", JOB_COLUMNS), ("gpu-statuses", STATUS_COLUMNS)):
        widget = table(panel, name)
        assert widget.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers, name
        assert widget.columnCount() == len(columns), name
        for row in range(widget.rowCount()):
            for column in range(widget.columnCount()):
                item = widget.item(row, column)
                assert item is not None, (name, row, column)
                assert not (item.flags() & Qt.ItemFlag.ItemIsEditable), (name, row, column)


def test_the_tab_opens_no_socket_and_starts_no_process_with_fakes(qtbot, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the GPUs tab must not reach the network or start a process in this test")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.calls == 1
