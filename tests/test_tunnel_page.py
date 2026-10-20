"""Tests for the Tunnel tab's no-source message: ui/tunnel_page.py.

This build has no local source for the tunnel slot states. The core does not write the local
tunnel slot status file (core item MS-104), and the runner status reports only one tunnel label's
lifecycle state. So the page must say the states are not available, name the missing core item,
say what to do, and show no slot row. Nothing here makes a request or reads a real file. The page
runs under pytest-qt on the offscreen platform (conftest).
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QTableWidget

from meridian_desktop.ui.tunnel_page import NOT_AVAILABLE_TEXT, TunnelPage
from meridian_desktop.ui.text import SELECTABLE_TEXT

REPO = Path(__file__).resolve().parents[1]
TUNNEL_SOURCES = (
    REPO / "meridian_desktop" / "ui" / "tunnel_page.py",
    REPO / "meridian_desktop" / "tunnel_view.py",
)


def test_not_available_text_names_the_missing_core_item():
    assert "MS-104" in NOT_AVAILABLE_TEXT
    assert "local tunnel slot status file" in NOT_AVAILABLE_TEXT


def test_not_available_text_says_what_is_missing_and_what_to_do():
    assert "What is missing:" in NOT_AVAILABLE_TEXT
    assert "Nothing to do in the desktop until it ships." in NOT_AVAILABLE_TEXT


def test_not_available_text_never_says_not_built_yet():
    for path in TUNNEL_SOURCES:
        source = path.read_text(encoding="utf-8").lower()
        assert "not built yet" not in source, path.name
    assert "not built yet" not in NOT_AVAILABLE_TEXT.lower()


def test_page_without_a_snapshot_shows_the_missing_core_item(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)

    status = page.findChild(QLabel, "tunnel-status")
    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert status.text() == NOT_AVAILABLE_TEXT
    assert "MS-104" in status.text()
    assert table.rowCount() == 0


def test_not_available_label_is_plain_text_and_selectable(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)

    status = page.findChild(QLabel, "tunnel-status")
    assert status.textFormat() == Qt.TextFormat.PlainText
    assert status.textInteractionFlags() == SELECTABLE_TEXT
