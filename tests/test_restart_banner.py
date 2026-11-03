"""Tests for the restart notice strip. The watch is a fake, so no file is read and no timer waits. Offscreen Qt."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from meridian_desktop.restart_check import Restart, banner_text
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.restart_banner import CHECK_INTERVAL_MS, RestartBanner


class FakeWatch:
    """Returns ``next`` from each check. The test sets it before the check it wants."""

    def __init__(self):
        self.next = Restart()
        self.checks = 0

    def check(self):
        self.checks += 1
        return self.next


def _banner(qtbot):
    watch = FakeWatch()
    banner = RestartBanner(watch)
    qtbot.addWidget(banner)
    return banner, watch


def _dismiss(banner):
    return banner.findChild(QPushButton, "restart-banner-dismiss")


def test_the_banner_starts_hidden_and_checks_every_interval(qtbot):
    banner, _watch = _banner(qtbot)
    assert banner.isHidden()
    assert banner._timer.interval() == CHECK_INTERVAL_MS == 30_000
    assert banner._timer.isActive()


def test_a_change_shows_the_banner_with_its_text_and_a_selectable_label(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/ui/main_window.py",))
    banner.check_now()
    assert not banner.isHidden()
    label = banner.findChild(QLabel)
    assert label.text() == banner_text(watch.next)
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse


def test_a_server_only_change_is_shown(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(server=True)
    banner.check_now()
    assert not banner.isHidden()
    assert banner.findChild(QLabel).text() == (
        "Restart needed: the local Meridian server changed on disk since it started."
    )


def test_dismiss_hides_the_banner_and_the_same_change_does_not_bring_it_back(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    banner.check_now()
    _dismiss(banner).click()
    assert banner.isHidden()
    banner.check_now()
    assert banner.isHidden()


def test_a_new_change_after_dismiss_brings_the_banner_back(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    banner.check_now()
    _dismiss(banner).click()
    watch.next = Restart(code=("meridian_desktop/a.py", "meridian_desktop/b.py"))
    banner.check_now()
    assert not banner.isHidden()


def test_no_change_hides_a_shown_banner_again(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    banner.check_now()
    assert not banner.isHidden()
    watch.next = Restart()
    banner.check_now()
    assert banner.isHidden()
    assert banner.restart == Restart()


def test_the_main_window_has_the_notice_strip_hidden_and_its_controls_report_starts(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path))
    qtbot.addWidget(window)
    banner = window.findChild(RestartBanner)
    assert banner is not None
    assert banner.isHidden()
    # Nothing under the test's own folders changed since the window was built, so a check keeps the strip hidden.
    banner.check_now()
    assert banner.isHidden()
    # A local start that the owner confirms goes to the window's own watch.
    assert window._runner_actions._on_started == window._restart_watch.note_server_started
