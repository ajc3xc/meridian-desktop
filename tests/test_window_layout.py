"""Default size and tab bar of the main window. Offscreen Qt, no network.

At the old default (about 506 px wide) the eight tab labels did not fit, so Qt hid some
tabs behind scroll arrows and cut off one label. The window also opened too small for the
Settings panel. These tests pin the fix.
"""

from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import MainWindow

# The Settings panel's key row and pods table need at least this much room on first show.
MIN_DEFAULT_WIDTH = 900
MIN_DEFAULT_HEIGHT = 600


def _shown_window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    return window


def _clipped_tabs(bar):
    """Labels whose tab sits outside the bar, or is too narrow for its text."""
    clipped = []
    for index in range(bar.count()):
        rect = bar.tabRect(index)
        label = bar.tabText(index)
        inside = 0 <= rect.left() and rect.right() < bar.width()
        if not inside or rect.width() < bar.fontMetrics().horizontalAdvance(label):
            clipped.append(label)
    return clipped


def test_default_window_is_large_enough(qtbot, settings_path):
    window = _shown_window(qtbot, settings_path)
    assert window.width() >= MIN_DEFAULT_WIDTH
    assert window.height() >= MIN_DEFAULT_HEIGHT


def test_every_tab_label_is_visible_at_the_default_size(qtbot, settings_path):
    window = _shown_window(qtbot, settings_path)
    bar = window.tabs.tabBar()
    assert bar.sizeHint().width() <= bar.width()  # no scroll arrows are needed
    assert _clipped_tabs(bar) == []


def test_window_cannot_shrink_below_the_tab_bar(qtbot, settings_path):
    window = _shown_window(qtbot, settings_path)
    window.resize(200, 200)
    qtbot.wait(50)
    bar = window.tabs.tabBar()
    assert bar.sizeHint().width() <= bar.width()
    assert _clipped_tabs(bar) == []
