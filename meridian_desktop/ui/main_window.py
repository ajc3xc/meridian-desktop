"""Main window: a mode selector on top, then one tab per area.

The Projects tab is read-only (wave 2). The Settings tab holds the RunPod panel
(wave 2), with the connection settings (local URL, database file, saved mode) above
its rows. The Writing tab holds the LaTeX engine probe under its section label (wave 2).
The Tunnel, Services and Logs tabs are built in wave 3 and are read-only. The Zotero tab
(wave 4) checks the local Zotero app and searches its library over 127.0.0.1:23119, and
starts Zotero only after the owner confirms. Writing and Code are still placeholders. Qt code
lives in meridian_desktop/ui/ (and the listed exception tray.py). Settings go through
settings_store and the RunPod key goes through secret_store, and neither imports Qt. The
only network calls, apart from the update check below, are loopback calls: to the local Meridian
server at the local_url setting, to the local LaTeX engine at the engine_url setting, to the local
Zotero API on 127.0.0.1:23119,
and to 127.0.0.1 slot ports for a connect-only check. The panels and the Projects tab make them
when a tab is first shown and when a button is pressed, except for the Sessions timer described below.
The Services check and the Logs read run only when their button is pressed, and the Tunnel tab runs one
read-only tunnel status call each time it is shown. Hosted mode calls only the hosted URL the owner sets:
on the Projects tab, on the Sessions tab when it is first shown or its Refresh is pressed (for the project
list, and for the sessions when no local server answers and a hosted token is saved), and on the Test
connection button in Settings.

The Sessions tab (read-only) lists the sessions of one project, picked from the project list. The project list
and the sessions are read when the tab is first shown and when its Refresh button is pressed, through the mode
chosen in the mode selector. While the tab stays visible, a 5-second timer reads only the local server, and only
in local mode, for the picked project. The timer never reads the project list and never calls the hosted host,
and when the local server does not answer it changes nothing on screen. Changing the picked project reads the
local server the same way, in local mode only; in any other mode it clears the table and says to press Refresh.
The Tools tab (read-only) shows which Meridian tools each AI client has set up, and whether the local server
and the tunnel are live. It reads when it is first shown and when its Refresh button is pressed. Neither tab
starts, stops or installs anything.

At a real start, one update check runs on the thread pool. It asks GitHub for the latest release JSON and
nothing else, and it shows a status-bar notice when a newer release is published. It is off when the
check_updates setting is false, and the app passes no transport under --smoke, so a smoke run makes no request.

Tunnel mode reads projects the same way Hosted does. Its top bar shows one line with the tunnel's
state (from one read-only status call, made when the mode is switched to Tunnel and at start in
Tunnel mode), the tools that need the tunnel, and how to start it by hand. The top bar has no
control that starts, stops or restarts anything.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThreadPool, Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import token_store, updater, zotero_client
from meridian_desktop.data_source import STATE_OK, DataSources, resolve
from meridian_desktop.runner_client import RunnerClient, RunnerResult
from meridian_desktop.secret_store import KeyringSecretStore, SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tools_status import ToolsStatus, read_tools_status
from meridian_desktop.tunnel_status import BAR_CHECKING_TEXT, DEPENDENCY_TEXT, bar_texts, read_tunnel_status
from meridian_desktop.ui.code_panel import CodePanel
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.latex_panel import LatexPanel
from meridian_desktop.ui.logs_page import LogsPage
from meridian_desktop.ui.outputs_panel import OutputsPanel
from meridian_desktop.ui.research_panel import ResearchPanel
from meridian_desktop.ui.projects_tab import ProjectsTab, default_sources
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.services_page import ServicesPage
from meridian_desktop.ui.sessions_panel import SessionsPanel
from meridian_desktop.ui.settings_tab import MODE_ITEMS, SettingsTab
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.ui.tools_panel import ToolsPanel
from meridian_desktop.ui.tunnel_page import TunnelCheckJob, TunnelPage
from meridian_desktop.ui.update_notice import UpdateNoticeBar
from meridian_desktop.ui.zotero_panel import ZoteroPanel
from meridian_desktop.version import APP_VERSION

# (tab name, wave that builds it). Display order is the tuple order. Sessions and Tools read the local
# server and the tunnel status, so they sit with the wave-3 services work.
TAB_SPECS = (
    ("Projects", 2),
    ("Sessions", 3),
    ("Tools", 3),
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

TUNNEL_MODE = "tunnel"
# One plain sentence per mode. The one for the chosen mode shows under the selector.
MODE_DESCRIPTIONS = {
    "hosted": "Hosted: projects load from your hosted Meridian account, with the hosted token saved in Settings.",
    "local": "Local: projects load from the Meridian server or database file on this computer.",
    TUNNEL_MODE: "Tunnel: projects load the same way as Hosted. Hosted tools reach this computer through the tunnel.",
}

# The window opens at this size (width, height). It fits the full tab bar, which holds twelve labels now
# that Sessions and Tools are tabs, and gives the Settings panel room for its key row and pods table.
DEFAULT_WINDOW_SIZE = (1280, 640)


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


class ProjectsUnavailable(Exception):
    """The Projects resolver found no source to read. The Sessions tab then says the projects could not be read,
    in plain words that name no class and no path."""


def _saved_local_url(store: SettingsStore) -> Callable[[], str]:
    """The provider the Code, Outputs and Research panels call: the local URL as saved now.

    It holds the store, not the window. A bound method of the window kept the window in a reference
    cycle, so a closed window was freed only by the cyclic collector. The collector runs on whichever
    thread allocates, and the panels call this provider on the pool, so a window could be freed on a
    pool worker and its widgets deleted off the GUI thread.
    """

    def read() -> str:
        return store.load().settings.local_url

    return read


def _saved_mode(store: SettingsStore) -> Callable[[], str]:
    """The mode saved in Settings, read by the Sessions tab's timer. Runs on the worker thread, holds no window."""

    def read() -> str:
        return store.load().settings.mode

    return read


def _saved_hosted(store: SettingsStore, secrets: SecretStore) -> Callable[[], tuple[str, str | None]]:
    """The hosted URL in Settings and the saved hosted token, for the Sessions tab. Runs on the worker thread.

    The token goes to the Sessions client for one request. The tab never shows it. Like _saved_local_url, this
    holds the store and the secret store, never the window, so no reference cycle keeps a closed window alive.
    """

    def read() -> tuple[str, str | None]:
        return store.load().settings.hosted_url, token_store.get_token(store=secrets)

    return read


def _saved_projects(store: SettingsStore, sources: DataSources) -> Callable[[], list[tuple[str, str]]]:
    """The projects the Projects tab resolves, as (id, name) pairs, through the same resolver and Settings.

    When the resolver found no source to read, the provider raises ProjectsUnavailable. The Sessions tab then
    says the projects could not be read, rather than that no projects exist.
    """

    def read() -> list[tuple[str, str]]:
        outcome = resolve(store.load().settings, sources)
        if outcome.state != STATE_OK:
            raise ProjectsUnavailable(outcome.message)
        pairs: list[tuple[str, str]] = []
        for row in outcome.rows:
            pid = row.get("id")
            if isinstance(pid, str) and pid.strip():
                name = row.get("name")
                pairs.append((pid, name if isinstance(name, str) else ""))
        return pairs

    return read


def _saved_tools_status(store: SettingsStore) -> Callable[[], ToolsStatus]:
    """One tools read, through the core Python and checkout saved in Settings. Runs on the worker thread.

    The tunnel row uses only the status call of a RunnerClient built from those two settings. With either one
    unset, that call is None and the tunnel row says Not checked. No tool is started or stopped.
    """

    def read() -> ToolsStatus:
        settings = store.load().settings
        tunnel_call = None
        if settings.core_python.strip() and settings.core_checkout.strip():
            client = RunnerClient(python=settings.core_python, cwd=settings.core_checkout)
            tunnel_call = client.status
        return read_tools_status(store, tunnel_status_call=tunnel_call)

    return read


class _ConfiguredRunner:
    """The runner's read-only status call, through the core Python and folder saved in Settings.

    The client is built on each call, so a change in Settings is used at once. With no core Python
    saved, the client reports not available and starts nothing. This class has no start, stop or
    restart method, and the client never passes one of those verbs.
    """

    def __init__(self, settings_store: SettingsStore) -> None:
        self._store = settings_store

    def status(self, scope: str) -> RunnerResult:
        settings = self._store.load().settings
        client = RunnerClient(python=settings.core_python or None, cwd=settings.core_checkout or None)
        return client.status(scope)


class MainWindow(QMainWindow):
    """Mode selector above one tab per area. Projects is read-only. Settings holds the RunPod panel."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        secret_store: SecretStore | None = None,
        data_sources: DataSources | None = None,
        zotero: Any | None = None,
        tunnel_runner: Any | None = None,
        tools_status: Callable[[], ToolsStatus] | None = None,
        update_transport: updater.Transport | None = None,
    ) -> None:
        super().__init__()
        self._store = settings_store if settings_store is not None else SettingsStore()
        self._secrets = secret_store if secret_store is not None else KeyringSecretStore()
        # The Projects tab reads through these. Tests pass fakes. The app uses the real sources.
        self._sources = data_sources if data_sources is not None else default_sources(self._secrets)
        # The Zotero tab calls this object. Tests pass a fake. The app uses the zotero_client module.
        self._zotero = zotero if zotero is not None else zotero_client
        # The tunnel status call (a status(scope) method). Tests pass a fake. The app builds the client
        # from the core Python and folder in Settings. Neither one can start or stop the tunnel.
        self._tunnel_runner = tunnel_runner if tunnel_runner is not None else _ConfiguredRunner(self._store)
        # The Tools tab's read (a no-argument callable that returns ToolsStatus). Tests pass a fake. The app reads
        # the core Python and folder in Settings. It starts and stops nothing.
        self._tools_status_read = tools_status if tools_status is not None else _saved_tools_status(self._store)
        # Each tunnel check takes the next number. A result with an older number is dropped.
        self._tunnel_generation = 0
        # A plain token that identifies this window's checks. A result carries it, so the window keeps only its own.
        self._owner = object()
        shared_delivery().delivered.connect(self._on_delivered)
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
            elif name == "Sessions":
                # Nothing is read on build. The first show and the Refresh button read the project list and the
                # sessions. While the tab stays visible, a 5-second timer reads only the local server, and only in
                # local mode. The providers run on the worker thread, never on the UI thread, and hold no window.
                page = SessionsPanel(
                    _saved_local_url(self._store),
                    _saved_hosted(self._store, self._secrets),
                    _saved_projects(self._store, self._sources),
                    _saved_mode(self._store),
                    parent=self.tabs,
                )
            elif name == "Tools":
                # The read runs when the tab is first shown and when Refresh is pressed. No tool is started or stopped.
                page = ToolsPanel(self._tools_status_read, parent=self.tabs)
            elif name == "Writing":
                # The LaTeX probe sits under its section label. Makes no request until shown.
                latex = LatexPanel(self._store, parent=self.tabs)
                page = _placeholder_page(name, self.tabs, {LATEX_SECTION: latex})
            elif name == "Tunnel":
                # The same read-only status call as the top bar. The page says whether the tunnel
                # runs, and the slot states are not available in this build.
                page = TunnelPage(self.tabs, status=self._tunnel_runner.status)
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
                page = CodePanel(_saved_local_url(self._store), parent=self.tabs)
            elif name == "Outputs":
                page = OutputsPanel(_saved_local_url(self._store), parent=self.tabs)
            elif name == "Research":
                page = ResearchPanel(_saved_local_url(self._store), parent=self.tabs)
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
        # The update notice sits at the right of the status bar, hidden until a check finds a newer release.
        # One check per window, and only when check_updates is on. No transport means no request is made.
        self._update_notice = UpdateNoticeBar(APP_VERSION, update_transport, parent=self)
        self.statusBar().addPermanentWidget(self._update_notice)
        self._update_notice.start(enabled=self._store.load().settings.check_updates)
        # A saved Tunnel mode starts its status check now. Any other mode only shows its description.
        self._apply_mode_view(self._saved_mode)

    def _build_mode_bar(self, parent: QWidget) -> QVBoxLayout:
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

        # The one-line tunnel status. It is shown only in Tunnel mode (see _apply_mode_view).
        self.tunnel_status_label = make_selectable(QLabel(parent))
        self.tunnel_status_label.setObjectName("tunnel-mode-status")
        self.tunnel_status_label.setTextFormat(Qt.TextFormat.PlainText)
        bar.addWidget(self.tunnel_status_label)

        self.settings_warning = make_selectable(QLabel(parent))
        self.settings_warning.setObjectName("settings-warning")
        if result.warning:
            self.settings_warning.setText(f"Settings: {result.warning}")
        else:
            self.settings_warning.hide()
        bar.addWidget(self.settings_warning, 1)

        # The chosen mode's description. In Tunnel mode, the detail line names the tools that need the
        # tunnel and, when the tunnel is not running, how to start it by hand.
        self.mode_description = make_selectable(QLabel(parent))
        self.mode_description.setObjectName("mode-description")
        self.mode_description.setTextFormat(Qt.TextFormat.PlainText)
        self.mode_description.setWordWrap(True)
        self.tunnel_detail = make_selectable(QLabel(parent))
        self.tunnel_detail.setObjectName("tunnel-mode-detail")
        self.tunnel_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.tunnel_detail.setWordWrap(True)

        column = QVBoxLayout()
        column.addLayout(bar)
        column.addWidget(self.mode_description)
        column.addWidget(self.tunnel_detail)
        return column

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
        self._apply_mode_view(mode)

    def _apply_mode_view(self, mode: str) -> None:
        """Show the description of ``mode``, and the tunnel lines only in Tunnel mode.

        Switching to Tunnel mode starts one read-only status check. Nothing here starts, stops or
        restarts the tunnel.
        """
        self.mode_description.setText(MODE_DESCRIPTIONS.get(mode, ""))
        is_tunnel = mode == TUNNEL_MODE
        self.tunnel_status_label.setVisible(is_tunnel)
        self.tunnel_detail.setVisible(is_tunnel)
        if is_tunnel:
            self._start_tunnel_check()
        else:
            # A check still running for Tunnel mode is dropped when it finishes.
            self._tunnel_generation += 1

    def _start_tunnel_check(self) -> None:
        """One read-only tunnel status call on the thread pool. The UI thread never waits on it.

        The job holds the runner, this window's owner token and the shared delivery object, never the window. Its
        result comes back to _on_delivered on the GUI thread. A window that is closed while the check runs is never
        touched by the worker.
        """
        self._tunnel_generation += 1
        generation = self._tunnel_generation
        runner = self._tunnel_runner
        owner = self._owner
        delivery = shared_delivery()
        self.tunnel_status_label.setText(BAR_CHECKING_TEXT)
        self.tunnel_detail.setText(DEPENDENCY_TEXT)
        QThreadPool.globalInstance().start(
            TunnelCheckJob(
                lambda: read_tunnel_status(runner.status),
                lambda payload: delivery.delivered.emit(Delivered(owner, (generation, payload))),
            )
        )

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        """Runs on the GUI thread. A result for another window, an older check, or one from before a mode change is dropped."""
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (status, failure) = item.value
        if generation != self._tunnel_generation or self._saved_mode != TUNNEL_MODE:
            return  # an older check, or the mode changed after this check started
        line, detail = bar_texts(status, failure)
        self.tunnel_status_label.setText(line)
        self.tunnel_detail.setText(detail)

    def _revert_mode_selector(self) -> None:
        """Show the last saved mode again. Signals stay blocked, so the change does not save a second time."""
        self.mode_selector.blockSignals(True)
        try:
            self.mode_selector.setCurrentIndex(self.mode_selector.findData(self._saved_mode))
        finally:
            self.mode_selector.blockSignals(False)
