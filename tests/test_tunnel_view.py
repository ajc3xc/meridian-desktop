"""Tests for the Tunnel tab's slot rows: meridian_desktop/tunnel_view.py and ui/tunnel_page.py.

The snapshots are written by hand in the shape of the core's get_tunnel_diagnostics output
(meridian/routes/tunnel.py, build_tunnel_diagnostics). They hold fake values only: no token,
no URL and no real command. The page runs under pytest-qt on the offscreen platform (conftest).
Nothing here makes a request.
"""

import ast
from pathlib import Path

from PySide6.QtWidgets import QAbstractButton, QLabel, QTableWidget

from meridian_desktop import tunnel_view
from meridian_desktop.tunnel_view import (
    DISABLED_NOTE,
    MISSING_NOTE,
    NO_STATE,
    NOT_REPORTED,
    NOT_RUNNING_NOTE,
    RETIRED_LABEL,
    RETIRED_NOTE,
    RETIRED_SLOT,
    SERVER_STATES,
    UNSHOWN_NOTE,
    SlotDetail,
    SlotRow,
    slot_details,
    slot_rows,
    slots_of,
)
from meridian_desktop.ui.tunnel_page import (
    NO_SLOTS_TEXT,
    NOT_AVAILABLE_TEXT,
    SHOWN_TEXT,
    SLOT_COLUMNS,
    TunnelPage,
)

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "meridian_desktop" / "tunnel_view.py"

BUILTIN_SLOTS = ("fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero", "outputs", "debug")


def _entry(state, *, enabled=True, process_active=True):
    """One slot entry in the shape the core returns. Only the fields the view reads matter here."""
    return {
        "dashboard_configured": {"enabled": enabled, "command": "example-command", "description": "fixture"},
        "process_active": process_active,
        "external_child_state": None,
        "healthy_reported": state == "healthy",
        "health_basis": "default_assumed",
        "health_reported_at": None,
        "health_age_seconds": None,
        "retry_count": None,
        "quarantine_reason": None,
        "last_error": None,
        "state": state,
        "remediation": "No action needed.",
    }


def _snapshot(slots):
    return {
        "run_id": "fixture-run",
        "generated_at": 1_800_000_000.0,
        "tenant_id": "fixture-tenant",
        "authenticated": True,
        "hostname": "fixture-host",
        "tunnel_process": {"any_active": True, "cross_instance_owner": None, "cross_instance_miss": False},
        "slots": slots,
    }


# Every server state appears on a slot other than word. word reports "healthy" here, so the
# test shows that the retired label wins over whatever the server says.
FIVE_STATE_SLOTS = {
    "fs": _entry("healthy"),
    "code": _entry("stale"),
    "extract": _entry("degraded"),
    "ppt": _entry("quarantined"),
    "word": _entry("healthy"),
    "dc": _entry("restart_required"),
    "docs": _entry("healthy"),
    "zotero": _entry("healthy"),
    "outputs": _entry("degraded"),
    "debug": _entry("stale"),
}
FIVE_STATE_SNAPSHOT = _snapshot(FIVE_STATE_SLOTS)

# A realistic snapshot. The core labels a slot that is off in the dashboard "disabled", which is
# not one of the five states. word is always off, so the core reports it that way.
REALISTIC_SLOTS = {
    "fs": _entry("healthy"),
    "code": _entry("disabled", enabled=False, process_active=False),
    "extract": _entry("stale"),
    "ppt": _entry("disabled", enabled=False, process_active=False),
    "word": _entry("disabled", enabled=False, process_active=False),
    "dc": _entry("restart_required"),
    "docs": _entry("degraded"),
    "zotero": _entry("disabled", enabled=False, process_active=False),
    "outputs": _entry("healthy"),
    "debug": _entry("quarantined"),
}
REALISTIC_SNAPSHOT = _snapshot(REALISTIC_SLOTS)


def _state_cells(rows):
    return {row.slot: row.state for row in rows}


def test_slot_rows_show_five_states_and_retired_word():
    rows = slot_rows(FIVE_STATE_SNAPSHOT)

    # One row per key in the slots dict, in the dict's order.
    assert [row.slot for row in rows] == list(FIVE_STATE_SLOTS)
    assert len(rows) == 10

    # The word slot renders as retired, even though the server reports it healthy.
    by_slot = _state_cells(rows)
    assert by_slot[RETIRED_SLOT] == RETIRED_LABEL

    # Every other state cell is one of the five server states, and all five appear.
    shown = {row.state for row in rows if row.slot != RETIRED_SLOT}
    assert shown == set(SERVER_STATES)
    assert SERVER_STATES == ("healthy", "stale", "degraded", "quarantined", "restart_required")


def test_word_row_is_retired_whatever_the_server_reports():
    for reported in ("healthy", "stale", "degraded", "quarantined", "restart_required", "disabled"):
        snapshot = _snapshot({RETIRED_SLOT: _entry(reported)})
        (row,) = slot_rows(snapshot)
        assert row.slot == RETIRED_SLOT
        assert row.state == RETIRED_LABEL


def test_realistic_snapshot_shows_disabled_slots_without_a_state():
    rows = slot_rows(REALISTIC_SNAPSHOT)
    assert [row.slot for row in rows] == list(REALISTIC_SLOTS)

    by_slot = {row.slot: row for row in rows}
    assert by_slot["word"].state == RETIRED_LABEL
    assert by_slot["code"] == SlotRow("code", NO_STATE, DISABLED_NOTE)
    assert by_slot["zotero"].state == NO_STATE
    assert by_slot["fs"] == SlotRow("fs", "healthy")
    assert by_slot["debug"] == SlotRow("debug", "quarantined")


def test_no_state_outside_the_five_is_shown_as_a_state():
    odd_states = {
        "alpha": _entry("disabled"),
        "beta": _entry("mystery_state"),
        "gamma": {"state": 42},
        "delta": {"state": ["healthy"]},
    }
    rows = slot_rows(_snapshot(odd_states))
    for row in rows:
        assert row.state == NO_STATE or row.state in SERVER_STATES
    assert _state_cells(rows)["alpha"] == NO_STATE
    assert _state_cells(rows)["beta"] == NO_STATE
    assert {row.slot: row.note for row in rows}["beta"] == UNSHOWN_NOTE


def test_every_key_gets_a_row_even_when_its_entry_is_unusable():
    slots = {
        "fs": "not a dict",
        "code": {"process_active": True},
        "extract": None,
        "ppt": _entry("stale"),
    }
    rows = slot_rows(_snapshot(slots))
    assert [row.slot for row in rows] == ["fs", "code", "extract", "ppt"]
    by_slot = {row.slot: row for row in rows}
    assert by_slot["fs"] == SlotRow("fs", NO_STATE, MISSING_NOTE)
    assert by_slot["code"] == SlotRow("code", NO_STATE, MISSING_NOTE)
    assert by_slot["extract"] == SlotRow("extract", NO_STATE, MISSING_NOTE)
    assert by_slot["ppt"] == SlotRow("ppt", "stale")


def test_missing_snapshot_or_slots_dict_gives_no_rows():
    assert slot_rows(None) == []
    assert slot_rows("not a snapshot") == []
    assert slot_rows({}) == []
    assert slot_rows({"slots": None}) == []
    assert slot_rows({"slots": ["fs", "code"]}) == []
    assert slots_of(None) is None
    assert slots_of({"slots": {}}) == {}


def test_tunnel_view_module_is_pure_python():
    # The view model holds no Qt import and no network module, so it can be tested without a display.
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "PySide6" not in roots
    assert not roots & {"socket", "urllib", "http", "requests", "httpx", "ssl"}
    assert tunnel_view.RETIRED_SLOT == "word"


def test_builtin_slot_keys_are_the_core_names():
    # The fixtures use the core's built-in slot names, so a rename in the core shows up here.
    assert tuple(REALISTIC_SLOTS) == BUILTIN_SLOTS
    assert tuple(FIVE_STATE_SLOTS) == BUILTIN_SLOTS


def test_healthy_row_carries_health_basis_and_age():
    healthy = {**_entry("healthy"), "health_basis": "client_reported", "health_age_seconds": 42.0}
    assumed = {**_entry("healthy"), "health_basis": "default_assumed", "health_age_seconds": None}
    stale = {**_entry("stale"), "health_basis": "client_reported", "health_age_seconds": 9.0}
    word = {**_entry("healthy"), "health_basis": "client_reported", "health_age_seconds": 3.0}
    snapshot = _snapshot({"fs": healthy, "code": assumed, "extract": stale, "word": word})

    details = {detail.row.slot: detail for detail in slot_details(snapshot)}
    assert details["fs"] == SlotDetail(SlotRow("fs", "healthy"), "client_reported", "42")
    assert details["code"] == SlotDetail(SlotRow("code", "healthy"), "default_assumed", NOT_REPORTED)
    # Only a healthy row shows health text. A stale row and the retired word row show none.
    assert details["extract"] == SlotDetail(SlotRow("extract", "stale"))
    assert details["word"] == SlotDetail(SlotRow(RETIRED_SLOT, RETIRED_LABEL, RETIRED_NOTE))


def test_missing_health_values_read_as_not_reported():
    blank_basis = {**_entry("healthy"), "health_basis": "   ", "health_age_seconds": True}
    infinite_age = {**_entry("healthy"), "health_basis": None, "health_age_seconds": float("inf")}
    huge_age = {**_entry("healthy"), "health_basis": None, "health_age_seconds": 10**400}
    details = slot_details(_snapshot({"fs": blank_basis, "code": infinite_age, "ppt": huge_age}))
    assert [detail.row.state for detail in details] == ["healthy", "healthy", "healthy"]
    for detail in details:
        assert detail.health_basis == NOT_REPORTED
        assert detail.health_age == NOT_REPORTED


# ---------------------------------------------------------------- the Qt page


def _cell(table, row, column):
    item = table.item(row, column)
    return item.text() if item is not None else None


def test_page_without_a_snapshot_says_not_available(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)

    status = page.findChild(QLabel, "tunnel-status")
    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert status.text() == NOT_AVAILABLE_TEXT
    assert table.rowCount() == 0
    assert [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())] == list(SLOT_COLUMNS)


def test_page_shows_one_row_per_slot_with_the_five_states_and_retired_word(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)
    page.set_snapshot(FIVE_STATE_SNAPSHOT)

    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert page.findChild(QLabel, "tunnel-status").text() == SHOWN_TEXT
    assert table.rowCount() == len(FIVE_STATE_SLOTS)

    shown_states = set()
    for row, slot in enumerate(FIVE_STATE_SLOTS):
        assert _cell(table, row, 0) == slot
        state = _cell(table, row, 1)
        if slot == RETIRED_SLOT:
            assert state == RETIRED_LABEL
        else:
            assert state in SERVER_STATES
            shown_states.add(state)
    assert shown_states == set(SERVER_STATES)


def test_page_note_column_explains_a_slot_without_a_state(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)
    page.set_snapshot(REALISTIC_SNAPSHOT)

    table = page.findChild(QTableWidget, "tunnel-slot-table")
    rows = {_cell(table, row, 0): (_cell(table, row, 1), _cell(table, row, 2)) for row in range(table.rowCount())}
    assert rows["code"] == (NO_STATE, DISABLED_NOTE)
    assert rows["word"][0] == RETIRED_LABEL


def test_page_shows_slot_text_as_plain_text(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)
    page.set_snapshot(_snapshot({"<b>bold</b>": _entry("healthy")}))

    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert _cell(table, 0, 0) == "<b>bold</b>"


def test_page_with_no_slots_says_so(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)
    page.set_snapshot(_snapshot({}))

    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert page.findChild(QLabel, "tunnel-status").text() == NO_SLOTS_TEXT
    assert table.rowCount() == 0


def test_page_replaces_rows_when_a_new_snapshot_arrives(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)
    page.set_snapshot(FIVE_STATE_SNAPSHOT)
    page.set_snapshot(REALISTIC_SNAPSHOT)

    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert table.rowCount() == len(REALISTIC_SLOTS)
    assert [_cell(table, row, 0) for row in range(table.rowCount())] == list(REALISTIC_SLOTS)
    assert _cell(table, 1, 1) == NO_STATE  # code, now disabled

    page.set_snapshot(None)
    assert table.rowCount() == 0
    assert page.findChild(QLabel, "tunnel-status").text() == NOT_AVAILABLE_TEXT


def test_slot_rows_never_show_saved_setting_as_active(qtbot):
    # The dashboard has the slot on (dashboard_configured.enabled true), but no process is running.
    # The saved setting must not make the row read as active, so no state may show healthy here.
    for reported in (*SERVER_STATES, "disabled", "mystery_state"):
        entry = _entry(reported, enabled=True, process_active=False)
        (row,) = slot_rows(_snapshot({"fs": entry}))
        assert row.state != "healthy", reported
        assert row.state in (NO_STATE, "stale", "degraded", "quarantined", "restart_required"), reported

    saved_but_off = _snapshot({"fs": _entry("healthy", enabled=True, process_active=False)})
    assert slot_rows(saved_but_off) == [SlotRow("fs", NO_STATE, NOT_RUNNING_NOTE)]

    # A missing process_active counts as not running.
    no_process_field = _entry("healthy")
    del no_process_field["process_active"]
    assert slot_rows(_snapshot({"fs": no_process_field})) == [SlotRow("fs", NO_STATE, NOT_RUNNING_NOTE)]

    # A slot that is running keeps its healthy state.
    assert slot_rows(_snapshot({"fs": _entry("healthy", enabled=True, process_active=True)})) == [
        SlotRow("fs", "healthy")
    ]

    # The page: the saved-but-not-running rows never show healthy, and they show no health text.
    mixed = _snapshot(
        {
            "fs": _entry("healthy", enabled=True, process_active=False),
            "code": _entry("healthy", enabled=True, process_active=True),
        }
    )
    page = TunnelPage()
    qtbot.addWidget(page)
    page.set_snapshot(mixed)

    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert [_cell(table, row, 1) for row in range(table.rowCount())] == [NO_STATE, "healthy"]
    assert _cell(table, 0, 2) == NOT_RUNNING_NOTE
    assert (_cell(table, 0, 3), _cell(table, 0, 4)) == ("", "")


def test_page_shows_health_basis_and_age_beside_healthy_rows(qtbot):
    healthy = {**_entry("healthy"), "health_basis": "client_reported", "health_age_seconds": 42.0}
    stale = {**_entry("stale"), "health_basis": "client_reported", "health_age_seconds": 9.0}
    page = TunnelPage()
    qtbot.addWidget(page)
    page.set_snapshot(_snapshot({"fs": healthy, "code": stale}))

    table = page.findChild(QTableWidget, "tunnel-slot-table")
    headers = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
    assert headers[3:] == ["Health basis", "Health age (s)"]
    assert (_cell(table, 0, 3), _cell(table, 0, 4)) == ("client_reported", "42")
    assert (_cell(table, 1, 3), _cell(table, 1, 4)) == ("", "")


def _page_buttons(page):
    """Every button under the page, except the select-all corner button Qt puts inside a QTableWidget.

    That corner button belongs to the table and does nothing with NoSelection, so it is not a
    control the page offers. Any other button counts.
    """
    table = page.findChild(QTableWidget, "tunnel-slot-table")
    buttons = page.findChildren(QAbstractButton)
    for button in buttons:
        if button.objectName() == "qt_tableview_cornerbutton":
            assert button.parent() is table
    return [button for button in buttons if button.objectName() != "qt_tableview_cornerbutton"]


def test_tunnel_page_has_only_the_tunnel_start_stop_and_restart_buttons(qtbot):
    # The Tunnel tab's controls are the three buttons of ScopeControls for the tunnel (contract rules 2 and 9). No other
    # button is on the page, before or after a snapshot.
    expected = {"tunnel-start", "tunnel-stop", "tunnel-restart"}
    page = TunnelPage()
    qtbot.addWidget(page)
    assert {button.objectName() for button in _page_buttons(page)} == expected

    page.set_snapshot(FIVE_STATE_SNAPSHOT)
    assert {button.objectName() for button in _page_buttons(page)} == expected

    # The module imports no button class either, so the page cannot add a button of its own without this failing.
    page_path = REPO / "meridian_desktop" / "ui" / "tunnel_page.py"
    tree = ast.parse(page_path.read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not {name for name in imported if name.endswith("Button")}
