"""Where the main window opens, and how big it is. Offscreen Qt, no network.

The window used to open at 1280 x 780 with no check against the screen. On a 1366 x 768 screen, or a scaled laptop
screen (1366 x 768 at 125 percent is 1093 x 615), it was taller than the usable area, so the title bar, the part the
window is dragged by, sat above the top of the screen. These tests pin the fix.
"""

import dataclasses
import json

import pytest
from PySide6.QtCore import QMargins, QRect, QSize

from _fakes import MemoryKeyring
from meridian_desktop import local_client
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tools_status import ToolsStatus
from meridian_desktop.ui.main_window import (
    DEFAULT_WINDOW_SIZE,
    SCREEN_SHARE,
    MainWindow,
    initial_window_rect,
    kept_window_rect,
)
from meridian_desktop.ui.projects_tab import default_sources
from meridian_desktop.zotero_client import API_NOT_RUNNING, NOT_RUNNING_MESSAGE, ApiCheck, ZoteroStatus

PREFERRED = QSize(*DEFAULT_WINDOW_SIZE)
# The frame Windows 11 puts around a normal window at 100 percent: the resize borders and the title bar.
WINDOWS_FRAME = QMargins(8, 31, 8, 8)
# The smallest the real window gets: the full tab bar wide, and the Sessions tab tall.
WINDOW_MINIMUM = QSize(546, 411)
# The four screens named in the report. Each is (width, height) in logical pixels.
SMALL_AND_COMMON_SCREENS = [(1366, 768), (1920, 1080), (1093, 615), (1280, 720)]
TASKBAR = 48


def _frame_of(client: QRect, frame: QMargins) -> QRect:
    return client.marginsAdded(frame)


def _usable(width, height, origin=(0, 0)):
    """A screen's usable area: the screen less a taskbar along the bottom edge."""
    return QRect(origin[0], origin[1], width, height - TASKBAR)


class _OfflineZotero:
    """Zotero is not installed and not running. Nothing reaches 127.0.0.1:23119."""

    def status(self):
        return ZoteroStatus(exe=None, process_running=None, api=ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE))

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE)

    def search_items(self, query):
        return []

    def start_zotero(self, exe):
        return ""


def _offline_sources():
    """The default sources with a probe that says no server is running, so no test opens a socket."""

    def transport(request, timeout):
        return local_client._Reply(200, b"[]")

    real = default_sources(KeyringSecretStore(backend=MemoryKeyring()), server_transport=transport)
    return dataclasses.replace(real, probe=lambda url: local_client.HEALTH_NOT_RUNNING)


def _window(qtbot, settings_path):
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=KeyringSecretStore(backend=MemoryKeyring()),
        data_sources=_offline_sources(),
        zotero=_OfflineZotero(),
        tools_status=lambda: ToolsStatus(rows=(), message="test stand-in"),
    )
    qtbot.addWidget(window)
    return window


def _shown(qtbot, window):
    window.show()
    qtbot.waitExposed(window)
    qtbot.wait(20)  # the check that runs just after the first show
    return window


def _assert_frame_fits(window, available: QRect):
    frame = window.frameGeometry()
    assert frame.top() >= available.top(), f"the title bar is above the usable area: {frame} in {available}"
    assert available.contains(frame), f"the window frame {frame} is not inside the usable area {available}"


# ----------------------------------------------------------- first show (pure)


@pytest.mark.parametrize("width, height", SMALL_AND_COMMON_SCREENS)
@pytest.mark.parametrize("origin", [(0, 0), (1920, 0), (-1366, -300)], ids=["primary", "right-monitor", "upper-left-monitor"])
def test_the_first_frame_is_inside_the_usable_area_with_the_title_bar_visible(width, height, origin):
    available = _usable(width, height, origin)
    client = initial_window_rect(available, WINDOWS_FRAME, PREFERRED, WINDOW_MINIMUM)
    frame = _frame_of(client, WINDOWS_FRAME)

    assert available.contains(frame), f"{frame} leaves {available}"
    assert frame.top() >= available.top(), "the title bar is above the usable area"
    # At most the preferred size, and at most 90 percent of the usable width and height.
    assert client.width() <= PREFERRED.width() and client.height() <= PREFERRED.height()
    assert frame.width() <= int(available.width() * SCREEN_SHARE)
    assert frame.height() <= int(available.height() * SCREEN_SHARE)
    # Centred: the gaps on opposite sides differ by at most one pixel.
    assert abs((frame.left() - available.left()) - (available.right() - frame.right())) <= 1
    assert abs((frame.top() - available.top()) - (available.bottom() - frame.bottom())) <= 1


def test_a_large_screen_gets_the_full_preferred_size():
    client = initial_window_rect(_usable(1920, 1080), WINDOWS_FRAME, PREFERRED, WINDOW_MINIMUM)
    assert client.size() == PREFERRED


@pytest.mark.parametrize("width, height", [(1366, 768), (1093, 615), (1280, 720)])
def test_a_small_screen_gets_ninety_percent_of_its_usable_area(width, height):
    available = _usable(width, height)
    frame = _frame_of(initial_window_rect(available, WINDOWS_FRAME, PREFERRED, WINDOW_MINIMUM), WINDOWS_FRAME)
    # Shorter than the usable height, by 10 percent less rounding.
    assert frame.height() == int(available.height() * SCREEN_SHARE)
    assert frame.height() < PREFERRED.height() + WINDOWS_FRAME.top() + WINDOWS_FRAME.bottom()


def test_with_a_scaled_frame_the_title_bar_is_still_on_screen():
    # 1366 x 768 at 125 percent: 1093 x 615 logical, with a taller title bar in logical pixels too.
    frame_margins = QMargins(7, 30, 7, 7)
    available = QRect(0, 0, 1093, 615 - 38)
    client = initial_window_rect(available, frame_margins, PREFERRED, WINDOW_MINIMUM)
    frame = _frame_of(client, frame_margins)
    assert available.contains(frame)
    assert client.top() - frame_margins.top() >= available.top()


def test_a_minimum_larger_than_the_screen_keeps_the_title_bar_at_the_top_left():
    available = QRect(100, 50, 500, 400)
    client = initial_window_rect(available, WINDOWS_FRAME, PREFERRED, QSize(700, 600))
    frame = _frame_of(client, WINDOWS_FRAME)
    assert client.size() == QSize(700, 600)
    assert frame.topLeft() == available.topLeft()


def test_a_minimum_that_fits_wins_over_the_ninety_percent_share():
    available = QRect(0, 0, 600, 480)
    client = initial_window_rect(available, WINDOWS_FRAME, PREFERRED, WINDOW_MINIMUM)
    frame = _frame_of(client, WINDOWS_FRAME)
    assert client.size() == WINDOW_MINIMUM
    assert available.contains(frame)


# ----------------------------------------------------------- after a move or a screen change (pure)


def test_a_window_moved_to_a_smaller_monitor_shrinks_to_its_usable_area():
    small = _usable(1093, 615, origin=(1920, 0))
    client = QRect(2000, 40, *DEFAULT_WINDOW_SIZE)
    kept = kept_window_rect(small, client, WINDOWS_FRAME, WINDOW_MINIMUM)
    frame = _frame_of(kept, WINDOWS_FRAME)
    assert frame.width() <= small.width() and frame.height() <= small.height()
    assert small.contains(frame)
    assert frame.top() >= small.top()


def test_a_title_bar_above_the_usable_area_moves_down_and_nothing_else_changes():
    available = _usable(1920, 1080)
    client = QRect(300, -20, 1000, 600)
    kept = kept_window_rect(available, client, WINDOWS_FRAME, WINDOW_MINIMUM)
    assert kept.size() == client.size()
    assert kept.left() == client.left()
    assert kept.top() - WINDOWS_FRAME.top() == available.top()


def test_a_window_that_already_fits_keeps_its_place_and_size():
    available = _usable(1920, 1080)
    client = QRect(150, 120, 900, 600)
    assert kept_window_rect(available, client, WINDOWS_FRAME, WINDOW_MINIMUM) == client


def test_a_window_part_off_the_side_is_not_pulled_back_while_it_fits():
    # A window dragged half onto the next monitor is left where the owner put it. Only its size and title bar are kept.
    available = _usable(1920, 1080)
    client = QRect(1500, 200, 900, 600)
    assert kept_window_rect(available, client, WINDOWS_FRAME, WINDOW_MINIMUM) == client


# ----------------------------------------------------------- the real window


def test_the_real_window_frame_fits_the_usable_area_after_show(qtbot, settings_path):
    window = _shown(qtbot, _window(qtbot, settings_path))
    available = window.screen().availableGeometry()
    _assert_frame_fits(window, available)
    # The offscreen screen is 800 x 800, smaller than 1280 x 780, so the window took 90 percent of it.
    assert window.frameGeometry().width() <= int(available.width() * SCREEN_SHARE)
    assert window.frameGeometry().height() <= int(available.height() * SCREEN_SHARE)


def test_the_real_window_can_shrink_to_fit_a_small_scaled_laptop_screen(qtbot, settings_path):
    # 1366 x 768 at 125 percent is 1093 x 615 logical pixels, less the taskbar.
    window = _shown(qtbot, _window(qtbot, settings_path))
    minimum = window.minimumSizeHint().expandedTo(window.minimumSize())
    assert minimum.width() <= int(1093 * SCREEN_SHARE)
    assert minimum.height() <= int((615 - TASKBAR) * SCREEN_SHARE) - WINDOWS_FRAME.top() - WINDOWS_FRAME.bottom()


def test_a_long_settings_warning_wraps_and_does_not_widen_the_window(qtbot, settings_path):
    # Every value wrong: the warning on the top bar joins one problem per key, over 600 characters.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    bad = dict.fromkeys(
        ("mode", "local_url", "hosted_url", "db_path", "autostart", "core_checkout", "core_python", "key_names",
         "check_updates", "auto_update", "tunnel_control_enabled"),
        5,
    )
    settings_path.write_text(json.dumps(bad), encoding="utf-8")
    window = _window(qtbot, settings_path)
    assert len(window.settings_warning.text()) > 500
    _shown(qtbot, window)

    assert window.minimumSizeHint().expandedTo(window.minimumSize()).width() <= int(1093 * SCREEN_SHARE)
    _assert_frame_fits(window, window.screen().availableGeometry())
    # The whole warning is still there, on more than one line.
    assert window.settings_warning.wordWrap()
    assert window.settings_warning.height() > window.settings_warning.fontMetrics().height()


def test_moving_to_another_screen_shrinks_the_window_and_brings_the_title_bar_down(qtbot, settings_path):
    window = _shown(qtbot, _window(qtbot, settings_path))
    screen = window.screen()
    available = screen.availableGeometry()
    # As if the window came from a larger monitor: wider and taller than this screen, its title bar above the top.
    window.setGeometry(QRect(available.left() + 10, available.top() - 60, *DEFAULT_WINDOW_SIZE))
    qtbot.wait(20)
    assert not available.contains(window.frameGeometry())

    window.windowHandle().screenChanged.emit(screen)

    _assert_frame_fits(window, available)


def test_a_change_to_the_usable_area_keeps_the_title_bar_on_screen(qtbot, settings_path):
    window = _shown(qtbot, _window(qtbot, settings_path))
    screen = window.screen()
    available = screen.availableGeometry()
    window.move(window.x(), available.top() - 40)
    qtbot.wait(20)
    assert window.frameGeometry().top() < available.top()

    screen.availableGeometryChanged.emit(available)

    assert window.frameGeometry().top() >= available.top()


def test_a_size_asked_for_before_the_first_show_is_kept_within_the_usable_area(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    window.resize(600, 500)
    _shown(qtbot, window)
    assert window.size() == QSize(600, 500)
    _assert_frame_fits(window, window.screen().availableGeometry())


def test_a_maximised_window_is_left_to_the_system(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    window.showMaximized()
    qtbot.waitExposed(window)
    qtbot.wait(20)
    assert window.isMaximized()
    window.windowHandle().screenChanged.emit(window.screen())
    assert window.isMaximized()
