"""Main window: a mode selector on top, then one tab per area.

The Projects tab is read-only (wave 2). The Settings tab holds the RunPod panel
(wave 2), with the connection settings (local URL, database file, saved mode) above
its rows. The Writing tab holds the LaTeX engine probe under its section label (wave 2).
The other tabs are still placeholders. Qt code lives in meridian_desktop/ui/ (and the
listed exception tray.py). Settings go through settings_store and the RunPod key goes
through secret_store, and neither imports Qt. The only network calls are loopback calls:
to the local Meridian server at the local_url setting, and to the local LaTeX engine at
the engine_url setting. The panels and the Projects tab make them when a tab is first
shown and when a button is pressed. No other host is contacted.
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

from meridian_desktop.data_source import DataSources
from meridian_desktop.secret_store import KeyringSecretStore, SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.latex_panel import LatexPanel
from meridian_desktop.ui.projects_tab import ProjectsTab, default_sources
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.settings_tab import MODE_ITEMS, SettingsTab

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

LATEX_SECTION = "LaTeX (local engine on 127.0.0.1:8471)"
WRITING_SECTIONS = (
    "DOCX (meridian-docs)",
    LATEX_SECTION,
    "Google Docs (approach not decided)",
)

STATUS_MESSAGE = "No services checked yet"

# The window opens at this size (width, height). It fits the full tab bar and gives the
# Settings panel room for its key row and pods table.
DEFAULT_WINDOW_SIZE = (960, 640)


def _placeholder_page(
    name: str,
    wave: int,
    parent: QWidget,
    section_widgets: dict[str, QWidget] | None = None,
) -> QWidget:
    """A tab that is not built yet. ``section_widgets`` maps a Writing section label to a widget
    placed directly below that label, so a built section keeps its label."""
    page = QWidget(parent)
    page.setObjectName(f"placeholder-{name.lower()}")
    layout = QVBoxLayout(page)
    layout.addWidget(QLabel(f"Not built yet (wave {wave})", page))
    if name == "Writing":
        for section in WRITING_SECTIONS:
            layout.addWidget(QLabel(section, page))
            if section_widgets and section in section_widgets:
                layout.addWidget(section_widgets[section])
    layout.addStretch(1)
    return page


class MainWindow(QMainWindow):
    """Mode selector above one tab per area. Projects is read-only. Settings holds the RunPod panel."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        secret_store: SecretStore | None = None,
        data_sources: DataSources | None = None,
    ) -> None:
        super().__init__()
        self._store = settings_store if settings_store is not None else SettingsStore()
        self._secrets = secret_store if secret_store is not None else KeyringSecretStore()
        # The Projects tab reads through these. Tests pass fakes. The app uses the real sources.
        self._sources = data_sources if data_sources is not None else default_sources(self._secrets)
        self.setWindowTitle("Meridian")

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addLayout(self._build_mode_bar(central))

        self.tabs = QTabWidget(central)
        for name, wave in TAB_SPECS:
            if name == "Projects":
                self._projects = ProjectsTab(self._store, self._sources, parent=self.tabs)
                page: QWidget = self._projects
            elif name == "Settings":
                page = RunPodPanel(self._store, self._secrets, parent=self.tabs)
                # The connection settings go above the RunPod rows, in the panel's own layout,
                # so the Settings page is still the RunPod panel.
                self._settings_tab = SettingsTab(self._store, parent=page)
                page.layout().insertWidget(0, self._settings_tab)
            elif name == "Writing":
                # The LaTeX probe sits under its section label. Makes no request until shown.
                latex = LatexPanel(self._store, parent=self.tabs)
                page = _placeholder_page(name, wave, self.tabs, {LATEX_SECTION: latex})
            else:
                page = _placeholder_page(name, wave, self.tabs)
            self.tabs.addTab(page, name)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        # Qt hides tabs behind scroll arrows when the bar is narrower than its labels. The window
        # may not shrink below the full bar, so every tab label stays visible.
        self.tabs.setMinimumWidth(self.tabs.tabBar().sizeHint().width())
        self.resize(*DEFAULT_WINDOW_SIZE)

        self.statusBar().showMessage(STATUS_MESSAGE)

    def _build_mode_bar(self, parent: QWidget) -> QHBoxLayout:
        result = self._store.load()
        # The mode the selector last saved, or loaded at start. A failed save reverts to it.
        self._saved_mode = result.settings.mode
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
            self._revert_mode_selector()
            self.settings_warning.setText(f"Settings: could not save mode ({exc.__class__.__name__})")
            self.settings_warning.show()
            return
        self._saved_mode = mode
        self.settings_warning.hide()
        # The Settings tab shows the saved mode, so it reads the new one too.
        self._settings_tab.refresh()
        # The Projects tab reads from the source the new mode names.
        self._projects.refresh()

    def _revert_mode_selector(self) -> None:
        """Show the last saved mode again. Signals stay blocked, so the change does not save a second time."""
        self.mode_selector.blockSignals(True)
        try:
            self.mode_selector.setCurrentIndex(self.mode_selector.findData(self._saved_mode))
        finally:
            self.mode_selector.blockSignals(False)
