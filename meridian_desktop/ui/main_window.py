"""Main window: a mode selector on top, then five tabs: Home, Projects, Sessions, Search and Settings.

Home (ui/home_tab.py) has one card for each part of Meridian: the local Meridian server, the tunnel, the services
(the LaTeX engine and the slot ports), Zotero, the AI clients and their tools, and the updates. Each card checks its
part on its own when the window first shows, again every five minutes while Home is showing, and when Check now is
pressed. The tunnel's Start, Stop and Restart and the Zotero launch ask first. Search (ui/search_tab.py) searches the
local server's code, outputs and research, and sends queries only to the Local URL. Settings holds the connection
settings, the RunPod panel, the Zotero library panel, and Diagnostics: the full tools table, the LaTeX engine probe,
and the desktop, tunnel and runner logs. The Projects and Sessions tabs are read-only.

Qt code lives in meridian_desktop/ui/ (and the listed exception tray.py). Settings go through settings_store and the
RunPod key goes through secret_store, and neither imports Qt. The only network calls, apart from the update check
below, are loopback calls: to the local Meridian server at the local_url setting, to the local LaTeX engine at the
engine_url setting, to the local Zotero API on 127.0.0.1:23119, and to 127.0.0.1 slot ports for a connect-only check.
A check makes its call when the window first shows, on the Home tab's five-minute timer while it is showing, or when
its button is pressed. The Search tab calls the local server only when Search or Enter is pressed. Hosted mode calls
only the hosted URL the owner sets: on the Projects tab, on the Sessions tab when it is first shown or its Refresh is
pressed (for the project list, and for the sessions when no local server answers and a hosted token is saved), and
on the Test connection button in Settings.

The Sessions tab (read-only) lists the sessions of one project, picked from the project list. The project list
and the sessions are read when the tab is first shown and when its Refresh button is pressed, through the mode
chosen in the mode selector. While the tab stays visible, a 5-second timer reads only the local server, and only
in local mode, for the picked project. The timer never reads the project list and never calls the hosted host,
and when the local server does not answer it changes nothing on screen. Changing the picked project reads the
local server the same way, in local mode only; in any other mode it clears the table and says to press Refresh.

At a real start, one update check runs on the thread pool, and again every six hours while the app runs. It asks GitHub
for the latest release and its signed checksum list, downloads a newer release only after the signature verifies, and
stages it for the next restart. The status bar shows the download and the staged release, and the Updates card on Home
reads that same one result without asking GitHub again. It is off when the auto_update or check_updates setting is
false, and the app passes no transport under --smoke, so a smoke run makes no request.

Tunnel mode reads projects the same way Hosted does. Its top bar shows one line with the tunnel's
state (from one read-only status call, made when the mode is switched to Tunnel and at start in
Tunnel mode), the tools that need the tunnel, and how to start it by hand. The top bar has no
control that starts, stops or restarts anything.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThreadPool, Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
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
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.home_tab import HomeDeps, HomeTab
from meridian_desktop.ui.latex_panel import LatexPanel
from meridian_desktop.ui.logs_page import LogsPage
from meridian_desktop.ui.projects_tab import ProjectsTab, default_sources
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.search_tab import SearchTab
from meridian_desktop.ui.sessions_panel import SessionsPanel
from meridian_desktop.ui.settings_tab import MODE_ITEMS, SettingsTab
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.ui.tools_panel import ToolsPanel
from meridian_desktop.ui.tunnel_page import TunnelCheckJob
from meridian_desktop.ui.update_notice import UpdateNoticeBar
from meridian_desktop.ui.zotero_panel import ZoteroPanel
from meridian_desktop.version import APP_VERSION

# (tab name, wave that built it). Display order is the tuple order. Five tabs, no more: the rest of the
# areas are cards on Home, the Search tab's kinds, or sections of Settings.
TAB_SPECS = (
    ("Home", 7),
    ("Projects", 2),
    ("Sessions", 3),
    ("Search", 6),
    ("Settings", 2),
)
TAB_NAMES = tuple(name for name, _wave in TAB_SPECS)

LATEX_SECTION = "LaTeX (local engine on 127.0.0.1:8471)"
ZOTERO_SECTION = "Zotero (the Zotero app on this computer, 127.0.0.1:23119)"
DIAGNOSTICS_SECTION = "Diagnostics"
DIAGNOSTICS_INTRO = (
    "Read-only views for finding a fault: the full tools table, the LaTeX engine probe, and the desktop, tunnel "
    "and runner logs. Nothing here starts a service."
)

STATUS_MESSAGE = "Home checks each part of Meridian when this window opens, then every five minutes while Home is showing."

TUNNEL_MODE = "tunnel"
# One plain sentence per mode. The one for the chosen mode shows under the selector.
MODE_DESCRIPTIONS = {
    "hosted": "Hosted: projects load from your hosted Meridian account, with the hosted token saved in Settings.",
    "local": "Local: projects load from the Meridian server or database file on this computer.",
    TUNNEL_MODE: "Tunnel: projects load the same way as Hosted. Hosted tools reach this computer through the tunnel.",
}

# The window opens at this size (width, height). The five tab labels fit at 1280 px, and Home scrolls its cards
# below that height, so no card is cut off on a short window.
DEFAULT_WINDOW_SIZE = (1280, 780)


def _updates_allowed_reader(store: SettingsStore) -> Callable[[], bool]:
    """A reader that says whether automatic updates and the update check are both on. Read at each check, so a change
    in Settings applies at once. It holds the store only, never the window, so the window can still be freed."""

    def allowed() -> bool:
        settings = store.load().settings
        return settings.auto_update and settings.check_updates

    return allowed


class ProjectsUnavailable(Exception):
    """The Projects resolver found no source to read. The Sessions tab then says the projects could not be read,
    in plain words that name no class and no path."""


def _saved_local_url(store: SettingsStore) -> Callable[[], str]:
    """The local URL as saved now, read by the Search tab when a search runs.

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
    """Mode selector above five tabs: Home, Projects, Sessions, Search and Settings."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        secret_store: SecretStore | None = None,
        data_sources: DataSources | None = None,
        zotero: Any | None = None,
        tunnel_runner: Any | None = None,
        tools_status: Callable[[], ToolsStatus] | None = None,
        update_transport: updater.Transport | None = None,
        runner_actions: RunnerActions | None = None,
        update_root: Path | None = None,
        record_apps_entry: updater.AppsEntryWriter | None = None,
    ) -> None:
        super().__init__()
        self._store = settings_store if settings_store is not None else SettingsStore()
        self._secrets = secret_store if secret_store is not None else KeyringSecretStore()
        # The Projects tab reads through these. Tests pass fakes. The app uses the real sources.
        self._sources = data_sources if data_sources is not None else default_sources(self._secrets)
        # The Zotero cards and panel call this object. Tests pass a fake. The app uses the zotero_client module.
        self._zotero = zotero if zotero is not None else zotero_client
        # The tunnel status call (a status(scope) method). Tests pass a fake. The app builds the client
        # from the core Python and folder in Settings. The status call never starts or stops anything. The
        # Start, Stop and Restart controls use runner_actions instead, and they stay off until the owner turns them on.
        self._tunnel_runner = tunnel_runner if tunnel_runner is not None else _ConfiguredRunner(self._store)
        # The runner controls (the Home tunnel card and the Settings runner block). Tests pass fakes. The app reads Settings.
        self._runner_actions = runner_actions if runner_actions is not None else RunnerActions(self._store)
        # The tools read (a no-argument callable that returns ToolsStatus). Tests pass a fake. The app reads
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

        # The status-bar notice holds the one update check of this window, and Home's Updates card reads its result, so
        # it is built before the tabs and added to the status bar below. The bar is hidden unless a release is downloading
        # or staged. A check runs at start and every six hours after, only while auto_update and check_updates are on. No
        # transport means no request is made, and no update_root means nothing is staged or restarted.
        self._update_notice = UpdateNoticeBar(
            APP_VERSION,
            update_transport,
            install_root=update_root,
            enabled=_updates_allowed_reader(self._store),
            report=self.statusBar().showMessage,
            parent=self,
            record_apps_entry=record_apps_entry,
        )

        self.tabs = QTabWidget(central)
        for name, _wave in TAB_SPECS:
            if name == "Home":
                home = HomeTab(self._home_deps(), parent=self.tabs)
                home.settingsRequested.connect(self._show_settings)
                page: QWidget = home
            elif name == "Projects":
                self._projects = ProjectsTab(self._store, self._sources, parent=self.tabs)
                page = self._projects
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
            elif name == "Search":
                # Nothing is searched on build or show. Search or Enter reads the Local URL at that moment.
                page = SearchTab(_saved_local_url(self._store), parent=self.tabs)
            else:
                page = self._build_settings_page()
                self._settings_page = page
            self.tabs.addTab(page, name)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        # Qt hides tabs behind scroll arrows when the bar is narrower than its labels. The window
        # may not shrink below the full bar, so every tab label stays visible.
        self.tabs.setMinimumWidth(self.tabs.tabBar().sizeHint().width())
        self.resize(*DEFAULT_WINDOW_SIZE)

        self.statusBar().showMessage(STATUS_MESSAGE)
        self.statusBar().addPermanentWidget(self._update_notice)
        self._update_notice.start()
        # A saved Tunnel mode starts its status check now. Any other mode only shows its description.
        self._apply_mode_view(self._saved_mode)
        # The core search at start, and any later search or chosen core folder, changes what the tunnel check can reach.
        # A Tunnel mode window checks again once a search has finished.
        self._runner_actions.coreChanged.connect(self._on_core_changed)

    @Slot()
    def _on_core_changed(self) -> None:
        """Re-check the tunnel in Tunnel mode once the core search has finished. Other modes do nothing."""
        if self._runner_actions.locating or self._saved_mode != TUNNEL_MODE:
            return
        self._start_tunnel_check()

    # ----------------------------------------------------------- building

    def _home_deps(self) -> HomeDeps:
        """What the Home cards read and do. Each one is the window's own call, so tests and the app use the same wiring."""
        return HomeDeps(
            store=self._store,
            probe=self._sources.probe,
            tunnel=self._tunnel_runner.status,
            actions=self._runner_actions,
            zotero=self._zotero,
            tools=self._tools_status_read,
            updates=self._update_notice,
            current_version=APP_VERSION,
        )

    def _build_settings_page(self) -> QWidget:
        """Settings: the connection settings and RunPod panel, the Zotero panel, then Diagnostics.

        The page scrolls, so the longest section never hides the others. The RunPod panel stays the page's
        RunPod widget (object name runpod-panel), with the connection settings above its rows.
        """
        runpod = RunPodPanel(self._store, self._secrets, parent=self.tabs)
        # The connection settings go above the RunPod rows, in the panel's own layout.
        self._settings_tab = SettingsTab(
            self._store, secrets=self._secrets, parent=runpod, actions=self._runner_actions,
        )
        runpod.layout().insertWidget(0, self._settings_tab)

        container = QWidget(self.tabs)
        container.setObjectName("settings-page")
        column = QVBoxLayout(container)
        column.addWidget(runpod)
        column.addWidget(make_selectable(QLabel(ZOTERO_SECTION, container)))
        column.addWidget(ZoteroPanel(client=self._zotero, parent=container))
        column.addWidget(make_selectable(QLabel(DIAGNOSTICS_SECTION, container)))
        column.addWidget(make_selectable(QLabel(DIAGNOSTICS_INTRO, container)))
        column.addWidget(make_selectable(QLabel("Tools (full table)", container)))
        column.addWidget(ToolsPanel(self._tools_status_read, parent=container))
        column.addWidget(make_selectable(QLabel(LATEX_SECTION, container)))
        column.addWidget(LatexPanel(self._store, parent=container))
        column.addWidget(LogsPage(parent=container))
        column.addStretch(1)

        scroll = QScrollArea(self.tabs)
        scroll.setObjectName("settings-scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        return scroll

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

    # ----------------------------------------------------------- tabs

    def _show_settings(self) -> None:
        """Switch to the Settings tab. Home's Settings buttons use this."""
        self.tabs.setCurrentWidget(self._settings_page)

    # ----------------------------------------------------------- mode

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

    def show_launch_notes(self, text: str) -> None:
        """Show what the launch-time starts did, on the status bar. Called on the GUI thread by ui/runner_actions.LaunchStarts."""
        self.statusBar().showMessage(text)

    def _revert_mode_selector(self) -> None:
        """Show the last saved mode again. Signals stay blocked, so the change does not save a second time."""
        self.mode_selector.blockSignals(True)
        try:
            self.mode_selector.setCurrentIndex(self.mode_selector.findData(self._saved_mode))
        finally:
            self.mode_selector.blockSignals(False)
