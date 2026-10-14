"""Main window: a mode selector on top, then one placeholder tab per area.

Qt code lives in meridian_desktop/ui/ (and the listed exception tray.py).
Settings go through settings_store, which has no Qt import. Wave 1 makes no
network calls.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.settings_store import SettingsStore

# (tab name, wave that builds it). Display order is the tuple order.
TAB_SPECS = (
    ("Projects", 2),
    ("Writing", 5),
    ("Zotero", 4),
    ("Tunnel", 3),
    ("Services", 3),
    ("Code", 6),
    ("Logs", 3),
    ("Settings", 2),
)
TAB_NAMES = tuple(name for name, _wave in TAB_SPECS)

WRITING_SECTIONS = (
    "DOCX (meridian-docs)",
    "LaTeX (local engine on 127.0.0.1:8471)",
    "Google Docs (approach not decided)",
)

STATUS_MESSAGE = "No services checked (wave 1 makes no network calls)"

# Mode selector entries in display order: (settings value, label).
MODE_ITEMS = (("hosted", "Hosted"), ("local", "Local"))


def _placeholder_page(name: str, wave: int, parent: QWidget) -> QWidget:
    page = QWidget(parent)
    page.setObjectName(f"placeholder-{name.lower()}")
    layout = QVBoxLayout(page)
    layout.addWidget(QLabel(f"Not built yet (wave {wave})", page))
    if name == "Writing":
        for section in WRITING_SECTIONS:
            layout.addWidget(QLabel(section, page))
    layout.addStretch(1)
    return page


class MainWindow(QMainWindow):
    """Mode selector above one tab per area. Placeholders only in wave 1."""

    def __init__(self, settings_store: SettingsStore | None = None) -> None:
        super().__init__()
        self._store = settings_store if settings_store is not None else SettingsStore()
        self.setWindowTitle("Meridian")

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addLayout(self._build_mode_bar(central))

        self.tabs = QTabWidget(central)
        for name, wave in TAB_SPECS:
            self.tabs.addTab(_placeholder_page(name, wave, self.tabs), name)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.statusBar().showMessage(STATUS_MESSAGE)

    def _build_mode_bar(self, parent: QWidget) -> QHBoxLayout:
        result = self._store.load()
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Mode:", parent))

        self.mode_selector = QComboBox(parent)
        self.mode_selector.setObjectName("mode-selector")
        for value, label in MODE_ITEMS:
            self.mode_selector.addItem(label, value)
        self.mode_selector.setCurrentIndex(self.mode_selector.findData(result.settings.mode))
        self.mode_selector.currentIndexChanged.connect(self._on_mode_changed)
        bar.addWidget(self.mode_selector)

        self.settings_warning = QLabel(parent)
        self.settings_warning.setObjectName("settings-warning")
        if result.warning:
            self.settings_warning.setText(f"Settings: {result.warning}")
        else:
            self.settings_warning.hide()
        bar.addWidget(self.settings_warning, 1)
        return bar

    def _on_mode_changed(self, index: int) -> None:
        mode = self.mode_selector.itemData(index)
        try:
            self._store.set_mode(mode)
        except (OSError, ValueError) as exc:
            self.settings_warning.setText(f"Settings: could not save mode ({exc.__class__.__name__})")
            self.settings_warning.show()
            return
        self.settings_warning.hide()
