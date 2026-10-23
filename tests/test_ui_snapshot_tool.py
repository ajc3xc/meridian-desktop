"""tools/ui_snapshot.py runs the Meridian core search once after the window shows, as ui/app.py does.

Without that search the runner controls read the core as not set, so the saved PNGs would show controls that the real
app does not show. The search itself is replaced here, so no test probes a Python or starts a process.
"""

import importlib.util
from pathlib import Path

from meridian_desktop.ui import runner_actions

TOOL = Path(__file__).resolve().parents[1] / "tools" / "ui_snapshot.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("ui_snapshot_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_runs_the_core_search_once_the_way_the_app_does(monkeypatch, tmp_path):
    searches = []
    monkeypatch.setattr(runner_actions.RunnerActions, "find_core", lambda self: searches.append(self))

    assert _load_tool().main(["ui_snapshot", str(tmp_path / "out")]) == 0

    assert len(searches) == 1
    assert (tmp_path / "out" / "00-home-after-boot.png").is_file()
