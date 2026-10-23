"""Tests for meridian_desktop/launch_starts.py: the launch-time starts of amendments A and B. Pure Python, all fakes.

Each test builds LaunchDeps from recording fakes. No Zotero, no runner and no socket is touched. A start happens only
when its setting is on and its guard passes, and each start is called at most once.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from meridian_desktop.launch_starts import (
    FAILED,
    PROGRAM_LOCAL,
    PROGRAM_ZOTERO,
    SKIPPED,
    STARTED,
    LaunchDeps,
    run_launch_starts,
)
from meridian_desktop.local_client import HEALTH_NOT_RUNNING, HEALTH_RUNNING
from meridian_desktop.runner_controls import CONFIRMED, FAILED as CONTROL_FAILED, ControlResult
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING
from meridian_desktop.zotero_client import API_DISABLED, API_ERROR, API_NOT_RUNNING, API_ON, ZoteroError

EXE = "C:/Program Files/Zotero/zotero.exe"


class Recorder:
    """Builds LaunchDeps from fakes, and records every call. Each fake answers with what the test sets."""

    def __init__(self, *, probe=API_NOT_RUNNING, exe=EXE, zotero_error=None, zotero_raises=None,
                 health=HEALTH_NOT_RUNNING, status_state=NOT_RUNNING, local_result=None, local_raises=None,
                 core_ready=True):
        self.calls: list[str] = []
        self._probe = probe
        self._exe = exe
        self._zotero_error = zotero_error
        self._zotero_raises = zotero_raises
        self._health = health
        self._status_state = status_state
        self._local_result = local_result or ControlResult("start", "local", CONFIRMED, "started", RUNNING, 42)
        self._local_raises = local_raises
        self._core_ready = core_ready

    def deps(self) -> LaunchDeps:
        return LaunchDeps(
            zotero_probe=self._zotero_probe,
            zotero_exe=self._zotero_exe,
            zotero_start=self._zotero_start,
            local_health=self._local_health,
            local_status=self._local_status,
            local_start=self._local_start,
            core_ready=self._core_ready,
        )

    def _zotero_probe(self):
        self.calls.append("zotero_probe")
        if isinstance(self._probe, BaseException):
            raise self._probe
        return SimpleNamespace(state=self._probe)

    def _zotero_exe(self):
        self.calls.append("zotero_exe")
        return self._exe

    def _zotero_start(self, exe):
        self.calls.append(f"zotero_start:{exe}")
        if self._zotero_raises is not None:
            raise self._zotero_raises
        if self._zotero_error is not None:
            raise self._zotero_error
        return "Zotero was started"

    def _local_health(self):
        self.calls.append("local_health")
        return self._health

    def _local_status(self):
        self.calls.append("local_status")
        return ControlResult("status", "local", CONFIRMED, "state", self._status_state)

    def _local_start(self):
        self.calls.append("local_start")
        if self._local_raises is not None:
            raise self._local_raises
        return self._local_result


def _run(recorder, *, zotero=False, local=False):
    return run_launch_starts(zotero, local, recorder.deps())


# ------------------------------------------------------------------ off means no call at all


def test_both_switches_off_makes_no_call_and_no_note():
    rec = Recorder()

    assert _run(rec, zotero=False, local=False) == ()
    assert rec.calls == []


# ------------------------------------------------------------------ Zotero at launch (amendment A)


def test_zotero_starts_once_when_nothing_answers_and_it_is_installed():
    rec = Recorder(probe=API_NOT_RUNNING, exe=EXE)

    notes = _run(rec, zotero=True)

    assert [n.outcome for n in notes] == [STARTED]
    assert notes[0].program == PROGRAM_ZOTERO
    assert rec.calls == ["zotero_probe", "zotero_exe", f"zotero_start:{EXE}"]


@pytest.mark.parametrize("state", [API_ON, API_DISABLED, API_ERROR])
def test_zotero_is_not_started_when_something_already_answers_or_cannot_be_checked(state):
    rec = Recorder(probe=state)

    notes = _run(rec, zotero=True)

    assert [n.outcome for n in notes] == [SKIPPED]
    assert not any(call.startswith("zotero_start") for call in rec.calls)


def test_zotero_is_skipped_when_it_is_not_installed_in_a_usual_folder():
    rec = Recorder(probe=API_NOT_RUNNING, exe=None)

    notes = _run(rec, zotero=True)

    assert [n.outcome for n in notes] == [SKIPPED]
    assert "not installed" in notes[0].message
    assert not any(call.startswith("zotero_start") for call in rec.calls)


def test_a_zotero_start_error_is_failed_with_its_own_fixed_message():
    rec = Recorder(zotero_error=ZoteroError("start_failed", "Zotero could not be started (OSError)."))

    notes = _run(rec, zotero=True)

    assert [n.outcome for n in notes] == [FAILED]
    assert "Zotero could not be started (OSError)." in notes[0].message


def test_an_unexpected_zotero_error_is_failed_with_the_class_name_only():
    rec = Recorder(zotero_raises=RuntimeError("a detail that must not be shown"))

    notes = _run(rec, zotero=True)

    assert [n.outcome for n in notes] == [FAILED]
    assert "RuntimeError" in notes[0].message
    assert "a detail that must not be shown" not in notes[0].message


# ------------------------------------------------------------------ the local server at launch (amendment B)


def test_the_local_server_starts_once_when_the_runner_says_not_running_and_nothing_answers():
    rec = Recorder(health=HEALTH_NOT_RUNNING, status_state=NOT_RUNNING)

    notes = _run(rec, local=True)

    assert [n.outcome for n in notes] == [STARTED]
    assert notes[0].program == PROGRAM_LOCAL
    assert rec.calls == ["local_health", "local_status", "local_start"]


def test_the_local_server_is_not_started_when_the_core_is_not_set():
    rec = Recorder(core_ready=False)

    notes = _run(rec, local=True)

    assert [n.outcome for n in notes] == [SKIPPED]
    assert rec.calls == []


@pytest.mark.parametrize("health", [HEALTH_RUNNING, "not_meridian", "error"])
def test_the_local_server_is_not_started_over_something_already_answering_at_the_local_url(health):
    rec = Recorder(health=health, status_state=NOT_RUNNING)

    notes = _run(rec, local=True)

    assert [n.outcome for n in notes] == [SKIPPED]
    assert "already answers" in notes[0].message
    assert "local_start" not in rec.calls


@pytest.mark.parametrize("state", [RUNNING, "unknown"])
def test_the_local_server_is_not_started_unless_the_runner_says_not_running(state):
    rec = Recorder(health=HEALTH_NOT_RUNNING, status_state=state)

    notes = _run(rec, local=True)

    assert [n.outcome for n in notes] == [SKIPPED]
    assert "local_start" not in rec.calls


def test_a_failed_local_start_is_reported_with_its_message():
    rec = Recorder(local_result=ControlResult("start", "local", CONTROL_FAILED, "The local server did not stay running."))

    notes = _run(rec, local=True)

    assert [n.outcome for n in notes] == [FAILED]
    assert "did not stay running" in notes[0].message


def test_an_unexpected_local_error_is_failed_with_the_class_name_only():
    rec = Recorder(local_raises=OSError("a path that must not be shown"))

    notes = _run(rec, local=True)

    assert [n.outcome for n in notes] == [FAILED]
    assert "OSError" in notes[0].message
    assert "a path that must not be shown" not in notes[0].message


# ------------------------------------------------------------------ both, in order, each once


def test_both_starts_run_in_order_each_exactly_once():
    rec = Recorder(probe=API_NOT_RUNNING, status_state=NOT_RUNNING)

    notes = _run(rec, zotero=True, local=True)

    assert [n.program for n in notes] == [PROGRAM_ZOTERO, PROGRAM_LOCAL]
    assert [call for call in rec.calls if call.startswith("zotero_start")] == [f"zotero_start:{EXE}"]
    assert rec.calls.count("local_start") == 1
