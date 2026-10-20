"""Main window: a mode selector on top, then one tab per area.

The Projects tab is read-only (wave 2). The Settings tab holds the RunPod panel
(wave 2), with the connection settings (local URL, database file, saved mode) above
its rows. The Writing tab holds the LaTeX engine probe under its section label (wave 2).
The Tunnel, Services and Logs tabs are built in wave 3 and are read-only. The Zotero tab
(wave 4) checks the local Zotero app and searches its library over 127.0.0.1:23119, and
starts Zotero only after the owner confirms. Writing and Code are still placeholders. Qt code
lives in meridian_desktop/ui/ (and the listed exception tray.py). Settings go through
settings_store and the RunPod key goes through secret_store, and neither imports Qt. The
only network calls are loopback calls: to the local Meridian server at the local_url setting,
to the local LaTeX engine at the engine_url setting, to the local Zotero API on 127.0.0.1:23119,
and to 127.0.0.1 slot ports for a connect-only check. The panels and the Projects tab make them
when a tab is first shown and when a button is pressed. The Services check and the Logs
read run only when their button is pressed, and the Tunnel tab makes no request. Hosted
mode calls only the hosted URL the owner sets: on the Projects tab, and on the Test connection
button in Settings.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import zotero_client
from meridian_desktop.data_source import DataSources
from meridian_desktop.secret_store import KeyringSecretStore, SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.code_panel import CodePanel
from meridian_desktop.ui.latex_panel import LatexPanel
from meridian_desktop.ui.logs_page import LogsPage
from meridian_desktop.ui.outputs_panel import OutputsPanel
from meridian_desktop.ui.research_panel import ResearchPanel
from meridian_desktop.ui.projects_tab import ProjectsTab, default_sources
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.services_page import ServicesPage
from meridian_desktop.ui.settings_tab import MODE_ITEMS, SettingsTab
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.ui.tunnel_page import TunnelPage
from meridian_desktop.ui.zotero_panel import ZoteroPanel

# (tab name, wave that builds it). Display order is the tuple order.
TAB_SPECS = (
    ("Projects", 2),
    ("Writing", 5),
    ("Zotero", 4),
    ("Tunnel", 3),
    ("Services", 3),
    ("Code", 6),
    ("Outputs", 6),
    ("Research", 6),
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

# What a placeholder tab says is missing, in plain words. Nothing here says "not built yet".
PLACEHOLDER_REASONS = {
    "Writing": "DOCX and Google Docs writing are not in this build yet. The LaTeX section below works.",
}

STATUS_MESSAGE = "No services checked yet. Press Check services on the Services tab."

# The window opens at this size (width, height). It fits the full tab bar and gives the
# Settings panel room for its key row and pods table.
DEFAULT_WINDOW_SIZE = (960, 640)


def _placeholder_page(
    name: str,
    parent: QWidget,
    section_widgets: dict[str, QWidget] | None = None,
) -> QWidget:
    """A tab that is not built in this release. It says what is missing. ``section_widgets`` maps a
    Writing section label to a widget placed directly below that label, so a built section keeps its label."""
    page = QWidget(parent)
    page.setObjectName(f"placeholder-{name.lower()}")
    layout = QVBoxLayout(page)
    layout.addWidget(make_selectable(QLabel(PLACEHOLDER_REASONS[name], page)))
    if name == "Writing":
        for section in WRITING_SECTIONS:
            layout.addWidget(make_selectable(QLabel(section, page)))
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
        zotero: Any | None = None,
    ) -> None:
        super().__init__()
        self._store = settings_store if settings_store is not None else SettingsStore()
        self._secrets = secret_store if secret_store is not None else KeyringSecretStore()
        # The Projects tab reads through these. Tests pass fakes. The app uses the real sources.
        self._sources = data_sources if data_sources is not None else default_sources(self._secrets)
        # The Zotero tab calls this object. Tests pass a fake. The app uses the zotero_client module.
        self._zotero = zotero if zotero is not None else zotero_client
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
                self._settings_tab = SettingsTab(self._store, secrets=self._secrets, parent=page)
                page.layout().insertWidget(0, self._settings_tab)
            elif name == "Writing":
                # The LaTeX probe sits under its section label. Makes no request until shown.
                latex = LatexPanel(self._store, parent=self.tabs)
                page = _placeholder_page(name, self.tabs, {LATEX_SECTION: latex})
            elif name == "Tunnel":
                # No slot-state source in this build, so the page says the states are not available.
                page = TunnelPage(self.tabs)
            elif name == "Services":
                # The checks run only when Check services is pressed. Nothing is probed on build or show.
                page = ServicesPage(self._store, parent=self.tabs)
            elif name == "Logs":
                # The reads run only when Refresh is pressed. Nothing is read on build or show.
                page = LogsPage(parent=self.tabs)
            elif name == "Zotero":
                # The status check runs when the tab is first shown and when Refresh is pressed.
                # Zotero starts only after the owner confirms in the dialog.
                page = ZoteroPanel(client=self._zotero, parent=self.tabs)
            elif name == "Code":
                # Search runs only when the owner presses Search. The local URL is read at that moment.
                page = CodePanel(self._current_local_url, parent=self.tabs)
            elif name == "Outputs":
                page = OutputsPanel(self._current_local_url, parent=self.tabs)
            elif name == "Research":
                page = ResearchPanel(self._current_local_url, parent=self.tabs)
            else:
                page = _placeholder_page(name, self.tabs)
            self.tabs.addTab(page, name)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        # Qt hides tabs behind scroll arrows when the bar is narrower than its labels. The window
        # may not shrink below the full bar, so every tab label stays visible.
        self.tabs.setMinimumWidth(self.tabs.tabBar().sizeHint().width())
        self.resize(*DEFAULT_WINDOW_SIZE)

        self.statusBar().showMessage(STATUS_MESSAGE)

    def _current_local_url(self) -> str:
        """The local server URL as saved now. The Code, Outputs and Research panels call this when they search."""
        return self._store.load().settings.local_url

    def _build_mode_bar(self, parent: QWidget) -> QHBoxLayout:
        result = self._store.load()
        # The mode the selector last saved, or loaded at start. A failed save reverts to it.
        self._saved_mode = result.settings.mode
        bar = QHBoxLayout()
        bar.addWidget(make_selectable(QLabel("Mode:", parent)))

        self.mode_selector = QComboBox(parent)
        self.mode_selector.setObjectName("mode-selector")
        for value, label in MODE_ITEMS:
            self.mode_selector.addItem(label, value)
        self.mode_selector.setCurrentIndex(self.mode_selector.findData(result.settings.mode))
        self.mode_selector.currentIndexChanged.connect(self._on_mode_changed)
        bar.addWidget(self.mode_selector)

        self.settings_warning = make_selectable(QLabel(parent))
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
