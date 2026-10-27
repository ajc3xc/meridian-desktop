"""The Home tab's card texts, in plain words. Pure Python: no Qt import.

Each card's check runs off the UI thread and hands back a plain result: the local server's facts, the services
facts, a tunnel status, a Zotero status, a tools status or an update check. This module turns one result into a
CardView: a headline, one detail sentence, the time checked, the rows of a compact list, and at most one action.

Nothing here starts, stops or installs anything. Only fixed words and the module's own sentences reach a view, so no
exception text, traceback, path or secret is shown. A card that cannot check says why and what to do, in one sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian_desktop import local_client, overleaf_client, overleaf_engine, tunnel_status, updater
from meridian_desktop.latex_client import (
    BACKUP_NOTE,
    ENGINE_CONNECTED,
    ENGINE_NOT_ANSWERING,
    ENGINE_NOT_RUNNING,
    OVERLEAF_LEAD,
    START_HELP,
    EngineStatus,
    port_text,
)
from meridian_desktop.slot_ports import PROBE_HOST, SlotPortResult
from meridian_desktop.tools_status import (
    NO,
    NOT_SET,
    NOT_STARTED,
    UNCHECKED,
    ToolRow,
    ToolsStatus,
)
from meridian_desktop.tunnel_status import NOT_AVAILABLE_REASON, NOT_RUNNING, RUNNING, UNKNOWN, TunnelStatus
from meridian_desktop.zotero_client import API_DISABLED, API_NOT_RUNNING, API_ON, ZoteroStatus

# Tones: the colour family a card uses. Idle is "not checked" or "not running, which is not an error".
TONE_IDLE = "idle"
TONE_CHECKING = "checking"
TONE_OK = "ok"
TONE_ATTENTION = "attention"
TONE_OFF = "off"

# What a card's action does. The window maps each key to one control.
ACTION_SETTINGS = "settings"
ACTION_LAUNCH_ZOTERO = "launch_zotero"
ACTION_RELEASE_PAGE = "release_page"
ACTION_CHECK_TOOLS = "check_tools"

TIME_FORMAT = "%H:%M:%S"
CHECKED_TEMPLATE = "Checked at {}."
NOT_CHECKED_HEADLINE = "Not checked yet"
CHECKING_HEADLINE = "Checking..."
COULD_NOT_CHECK_HEADLINE = "Could not check"
NO_BUILD_YET_HEADLINE = "No downloadable build yet"
# updater.fetch_latest refuses a release that has no zip yet, and that reason starts with this sentence. Nothing failed,
# so the Updates card gives that one refusal its own headline and keeps the reason as its explanation.
NO_BUILD_YET_START = "No downloadable build is published"
STILL_CHECKING_HEADLINE = "Still checking"
FAILED_TEMPLATE = "The check failed unexpectedly ({}). Press Check now to try again."
STALLED_TEMPLATE = (
    "This check has taken more than {} seconds. It may still finish, and this card updates when it does. "
    "Press Check now to start it again."
)
SET_UP_IN_SETTINGS = "Set up in Settings"
OPEN_SETTINGS_LABEL = "Open Settings"
CHECK_AGAIN_LABEL = "Check again"
LAUNCH_ZOTERO_LABEL = "Launch Zotero"
OPEN_RELEASE_LABEL = "Open release page"
# The tools rows also show in the full Tools table in Settings, which says "press Refresh". Home says "press Check now".
REFRESH_WORDS = "press Refresh"
CHECK_NOW_WORDS = "press Check now"


@dataclass(frozen=True)
class RowView:
    """One line of a compact list. ``action`` is one of the ACTION_* keys, or empty when the row needs nothing."""

    text: str
    detail: str = ""
    action: str = ""
    action_label: str = ""


@dataclass(frozen=True)
class CardView:
    """Everything a card shows. Every field is plain text, shown as it is."""

    headline: str
    detail: str = ""
    checked: str = ""
    tone: str = TONE_IDLE
    rows: tuple[RowView, ...] = ()
    action: str = ""
    action_label: str = ""


# ----------------------------------------------------------------- shared

def checked_text(when: datetime | None) -> str:
    """The time a check finished, or empty when no check has finished."""
    if when is None:
        return ""
    return CHECKED_TEMPLATE.format(when.strftime(TIME_FORMAT))


def checking_view() -> CardView:
    return CardView(headline=CHECKING_HEADLINE, tone=TONE_CHECKING)


def stalled_view(seconds: int) -> CardView:
    return CardView(headline=STILL_CHECKING_HEADLINE, detail=STALLED_TEMPLATE.format(seconds), tone=TONE_ATTENTION)


def failed_view(name: str, checked: str = "") -> CardView:
    """The card for a check that raised. Only the exception's class name is shown."""
    return CardView(
        headline=COULD_NOT_CHECK_HEADLINE,
        detail=FAILED_TEMPLATE.format(name or "unknown error"),
        checked=checked,
        tone=TONE_ATTENTION,
    )


def _check_now(text: str) -> str:
    """A tools-module sentence says 'press Refresh'. The Home card says 'press Check now' instead."""
    return text.replace("Press Refresh", "Press Check now").replace(REFRESH_WORDS, CHECK_NOW_WORDS)


# ----------------------------------------------------------------- local server

@dataclass(frozen=True)
class LocalFacts:
    """One read of the local server. ``refusal`` is set when the address is not on this computer, and then no probe ran."""

    url: str
    refusal: str = ""
    health: str = ""


def local_view(facts: LocalFacts | None, failure: str = "", checked: str = "") -> CardView:
    if facts is None:
        return failed_view(failure, checked)
    port = port_text(facts.url)
    if facts.refusal:
        return CardView(
            headline="Not checked",
            detail=f"{facts.refusal} Change the Local URL in Settings to an address on this computer.",
            checked=checked,
            tone=TONE_ATTENTION,
        )
    if facts.health == local_client.HEALTH_RUNNING:
        return CardView(
            headline="Running",
            detail=f"The local Meridian server answers on port {port}.",
            checked=checked,
            tone=TONE_OK,
        )
    if facts.health == local_client.HEALTH_NOT_RUNNING:
        return CardView(
            headline="Not running",
            detail=(
                f"Nothing listens on port {port}. Start Meridian on this computer, then press Check now. "
                "The desktop starts the server only from Settings, after you confirm."
            ),
            checked=checked,
            tone=TONE_ATTENTION,
        )
    if facts.health == local_client.HEALTH_NOT_MERIDIAN:
        return CardView(
            headline="Another program answers",
            detail=(
                f"Port {port} answers, but not with Meridian's health check. Stop that program, or change the "
                "Local URL in Settings, then press Check now."
            ),
            checked=checked,
            tone=TONE_ATTENTION,
        )
    return CardView(
        headline=COULD_NOT_CHECK_HEADLINE,
        detail=f"The check got no usable answer from port {port}. Press Check now to try again.",
        checked=checked,
        tone=TONE_ATTENTION,
    )


# ----------------------------------------------------------------- services

@dataclass(frozen=True)
class ServiceFacts:
    """The LaTeX engine and the slot ports, each read on its own. A check that raised has its class name in its failure."""

    engine_url: str
    engine: EngineStatus | None = None
    engine_failure: str = ""
    ports: tuple[SlotPortResult, ...] | None = None
    ports_failure: str = ""


# LaTeX normally runs in Overleaf in the browser, so the LaTeX row leads with that. The local engine is a backup that is
# off unless the owner turns it on, so off is an idle state, not a fault.
_ENGINE_HEADLINE = {
    ENGINE_CONNECTED: "LaTeX backup engine running",
    ENGINE_NOT_RUNNING: "LaTeX backup engine off",
    ENGINE_NOT_ANSWERING: "Another program answers on the engine port",
}
_ENGINE_DETAIL = {
    ENGINE_CONNECTED: "The backup engine answered its health and extension-version checks.",
    ENGINE_NOT_RUNNING: f"Nothing listens on that port. {BACKUP_NOTE} To turn it on, {START_HELP}",
    ENGINE_NOT_ANSWERING: "Something else answers on that port, not the LaTeX backup engine.",
}
PORTS_NOTE = "A listening port only shows that something accepts a connection. It does not show that Meridian is healthy."


def services_view(facts: ServiceFacts | None, failure: str = "", checked: str = "") -> CardView:
    if facts is None:
        return failed_view(failure, checked)
    rows: list[RowView] = []
    port = port_text(facts.engine_url)
    if facts.engine is None:
        headline, tone = COULD_NOT_CHECK_HEADLINE, TONE_ATTENTION
        rows.append(
            RowView(
                text=f"{OVERLEAF_LEAD} Local backup engine on port {port}: not checked.",
                detail=_failed_engine_text(facts),
            )
        )
    else:
        state = facts.engine.state
        headline = _ENGINE_HEADLINE.get(state, COULD_NOT_CHECK_HEADLINE)
        tone = {ENGINE_CONNECTED: TONE_OK, ENGINE_NOT_RUNNING: TONE_IDLE}.get(state, TONE_ATTENTION)
        detail = _ENGINE_DETAIL.get(state, "The engine check got no usable answer.")
        rows.append(RowView(text=f"{OVERLEAF_LEAD} Local backup engine on port {port}.", detail=detail))
    if facts.ports is None:
        rows.append(RowView(text="Slot ports: not checked.", detail=FAILED_TEMPLATE.format(facts.ports_failure or "unknown error")))
    else:
        listening = sum(1 for result in facts.ports if result.listening)
        rows.append(
            RowView(text=f"Slot ports: {listening} of {len(facts.ports)} listening on {PROBE_HOST}.", detail=PORTS_NOTE)
        )
    return CardView(headline=headline, detail="", checked=checked, tone=tone, rows=tuple(rows))


def _failed_engine_text(facts: ServiceFacts) -> str:
    return FAILED_TEMPLATE.format(facts.engine_failure or "unknown error")


# ----------------------------------------------------------------- tunnel

_TUNNEL_HEADLINE = {RUNNING: "Running", NOT_RUNNING: "Not running"}
TUNNEL_NOT_AVAILABLE_DETAIL = (
    "The tunnel cannot be checked until the Meridian core checkout and its Python are set. "
    "Set them in Settings, then press Check now."
)


def _tool_list(names: tuple[str, ...]) -> str:
    """Join tool names as a plain list: "a, b and c"."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


# The names come from tunnel_status.DEPENDENT_TOOLS, so this card cannot drift from the tool list.
TUNNEL_DEPENDS = (
    "Hosted tools reach this computer through the tunnel, so they need it running: "
    f"{_tool_list(tunnel_status.DEPENDENT_TOOLS)}."
)


def tunnel_view(status: TunnelStatus | None, failure: str = "", checked: str = "") -> CardView:
    if status is None:
        return CardView(
            headline=COULD_NOT_CHECK_HEADLINE,
            detail=tunnel_status.CHECK_FAILED_REASON.format(failure or tunnel_status.NO_FAILURE_NAME),
            checked=checked or tunnel_status.NO_TIME_TEXT,
            tone=TONE_ATTENTION,
        )
    if status.state == UNKNOWN and status.reason == NOT_AVAILABLE_REASON:
        # No core setup, so nothing was read. That is not a fault, and the card says what to set.
        return CardView(headline="Not checked", detail=TUNNEL_NOT_AVAILABLE_DETAIL, checked=checked, tone=TONE_IDLE)
    if status.state == RUNNING:
        return CardView(headline="Running", detail=f"{status.reason} {TUNNEL_DEPENDS}", checked=checked, tone=TONE_OK)
    if status.state == NOT_RUNNING:
        detail = f"{status.reason} {tunnel_status.START_HINT}"
        return CardView(headline="Not running", detail=detail, checked=checked, tone=TONE_IDLE)
    detail = f"{status.reason} {tunnel_status.CHECK_HINT}"
    return CardView(headline="Unknown", detail=detail, checked=checked, tone=TONE_ATTENTION)


# ----------------------------------------------------------------- zotero

_ZOTERO_API_DETAIL = {
    API_ON: "The local API answers on 127.0.0.1:23119.",
    API_DISABLED: "Zotero is open, but its local API is off. Turn it on in Zotero under Settings, then Advanced.",
    API_NOT_RUNNING: "Zotero is not answering on its local API. Zotero can take a little while to open.",
}
ZOTERO_NOT_INSTALLED = (
    "Zotero was not found in the usual install folders. Install it from zotero.org, then press Check now."
)


def zotero_view(status: ZoteroStatus | None, failure: str = "", checked: str = "") -> CardView:
    if status is None:
        return failed_view(failure, checked)
    if status.exe is None and status.process_running is not True:
        return CardView(
            headline="Not installed",
            detail=ZOTERO_NOT_INSTALLED,
            checked=checked,
            tone=TONE_ATTENTION,
        )
    if status.process_running is True:
        headline, tone = "Running", TONE_OK
    elif status.process_running is False:
        headline, tone = "Not running", TONE_IDLE
    else:
        headline, tone = "Process not checked", TONE_IDLE
    detail = _ZOTERO_API_DETAIL.get(status.api.state, "The local API could not be checked. Press Check now to try again.")
    can_launch = status.exe is not None and status.process_running is not True
    return CardView(
        headline=headline,
        detail=detail,
        checked=checked,
        tone=tone,
        action=ACTION_LAUNCH_ZOTERO if can_launch else "",
        action_label=LAUNCH_ZOTERO_LABEL if can_launch else "",
    )


# ----------------------------------------------------------------- tools

def _tool_action(row: ToolRow) -> tuple[str, str]:
    """The one action a row offers: set it up in Settings when it is missing, open Settings when the core is not set, else check again."""
    if row.configured == NO:
        return ACTION_SETTINGS, SET_UP_IN_SETTINGS
    if row.configured == NOT_SET:
        return ACTION_SETTINGS, OPEN_SETTINGS_LABEL
    return ACTION_CHECK_TOOLS, CHECK_AGAIN_LABEL


def _tool_row(row: ToolRow) -> RowView:
    action, label = _tool_action(row)
    return RowView(
        text=f"{row.client}: {row.tool}. {row.status}.",
        detail=_check_now(row.detail),
        action=action,
        action_label=label,
    )


def tools_view(status: ToolsStatus | None, failure: str = "", checked: str = "") -> CardView:
    if status is None:
        return failed_view(failure, checked)
    if not status.rows:
        return CardView(headline=NOT_CHECKED_HEADLINE, detail=_check_now(status.message), checked=checked)
    needs = [row for row in status.rows if row.configured == NO or row.live == NO]
    unknown = [row for row in status.rows if row.configured in (UNCHECKED, NOT_SET, NOT_STARTED)]
    if needs:
        headline, tone = f"{len(needs)} need attention", TONE_ATTENTION
    elif len(unknown) == len(status.rows):
        headline, tone = NOT_CHECKED_HEADLINE, TONE_IDLE
    else:
        headline, tone = "All set up", TONE_OK
    return CardView(
        headline=headline,
        detail=_check_now(status.message),
        checked=checked,
        tone=tone,
        rows=tuple(_tool_row(row) for row in status.rows),
    )


# ----------------------------------------------------------------- updates

UPDATES_OFF_DETAIL = (
    "Update checks are off. Turn on the automatic update checkbox in Settings (auto_update and check_updates) "
    "to check for a newer release when Meridian Desktop starts."
)
UPDATES_CHECKING_DETAIL = "Checking GitHub for a newer release."
UPDATES_READY_DETAIL = (
    "A newer release is downloaded and its signature is checked. Restart to update is in the status bar, "
    "and it installs only when you press it."
)
UPDATES_CURRENT_DETAIL = "This is the latest published release."


def updates_view(current: str, result: object | None, *, enabled: bool, checking: bool) -> CardView:
    """The Updates card. ``result`` is the one startup check's result (an updater.UpdateResult), or None before it finishes."""
    headline = f"Version {current}"
    if not enabled:
        return CardView(headline=headline, detail=UPDATES_OFF_DETAIL, tone=TONE_OFF)
    if isinstance(result, updater.UpdateResult):
        if result.status == "update_staged" and result.version:
            return CardView(
                headline=f"Update ready: v{result.version}",
                detail=UPDATES_READY_DETAIL,
                tone=TONE_ATTENTION,
                action=ACTION_RELEASE_PAGE,
                action_label=OPEN_RELEASE_LABEL,
            )
        if result.status == "up_to_date":
            return CardView(headline=headline, detail=UPDATES_CURRENT_DETAIL, tone=TONE_OK)
        if result.status == "offline":
            return CardView(
                headline=COULD_NOT_CHECK_HEADLINE,
                detail=f"{result.reason} Try again later.",
                tone=TONE_IDLE,
            )
        if result.reason.startswith(NO_BUILD_YET_START):
            return CardView(headline=NO_BUILD_YET_HEADLINE, detail=result.reason, tone=TONE_IDLE)
        # Refused: an install that cannot run here (a source run, or a copy outside the install folder), or a held release.
        return CardView(headline=COULD_NOT_CHECK_HEADLINE, detail=result.reason, tone=TONE_IDLE)
    if checking:
        return CardView(headline=headline, detail=UPDATES_CHECKING_DETAIL, tone=TONE_CHECKING)
    return CardView(headline=headline, detail=NOT_CHECKED_HEADLINE, tone=TONE_IDLE)


# ----------------------------------------------------------------- overleaf (LaTeX)

# The card reads the engine only on an explicit press, except for one local port check when Home is shown. Its states
# are kept by HomeTab and shown here: unchecked (the port has not been looked at yet), running, down, or failed (the
# check itself raised).
OVERLEAF_TITLE = "Overleaf (LaTeX)"
OVERLEAF_ENGINE_UNCHECKED = "unchecked"
OVERLEAF_ENGINE_RUNNING = "running"
OVERLEAF_ENGINE_DOWN = "down"
OVERLEAF_ENGINE_FAILED = "failed"

ENGINE_NOT_RUNNING_HEADLINE = "Engine not running"
ENGINE_RUNNING_UNCHECKED_HEADLINE = "Engine running, session not checked yet"
SIGNED_IN_HEADLINE = "Signed in"
SESSION_EXPIRED_HEADLINE = "Session expired: click Log in"
NO_SESSION_HEADLINE = "No saved session: click Log in"
UNREACHABLE_OVERLEAF_HEADLINE = "Could not reach Overleaf: try again later"
NO_CHROME_HEADLINE = "Chrome not found: install Chrome to log in"
ENGINE_DOWN_DETAIL = (
    "Nothing listens on port {}. LaTeX normally runs in Overleaf in your browser. The engine is only a backup, "
    "and it is off until it starts."
)
ENGINE_FAILED_DETAIL = "The engine check failed unexpectedly. Press Check now to try again."
OVERLEAF_UNCHECKED_DETAIL = (
    "Check sends your saved session to Overleaf once, to see if you are signed in. Nothing is sent before you press Check."
)
SIGNED_IN_DETAIL = "Your saved Overleaf session works."
SESSION_BAD_ANSWER_DETAIL = "The LaTeX engine sent an answer the desktop could not read. Press Check to try again."
SESSION_CHECK_FAILED_TEMPLATE = "The session check failed unexpectedly ({}). Press Check to try again."

LOGIN_OPENED_TEXT = "Login window opened: sign in there, then click Check."
LOGIN_ALREADY_OPEN_TEXT = "The login window is already open."
LOGIN_NO_CHROME_TEXT = "Chrome not found: install Chrome to log in."
LOGIN_ENGINE_DOWN_TEXT = "Start the engine first."
LOGIN_TIMEOUT_TEXT = "The LaTeX engine did not answer in time. Try again in a moment."
LOGIN_FAILED_TEXT = "The login window could not be opened. Try again later."

START_TITLE = "Start LaTeX engine"
# The manual Start dialog (Amendment G, rule 2). ``run`` is the command, working folder and starter that the start runs.
START_QUESTION = "Start the LaTeX engine on {address} in the background?\n\n{run}"
# The lines every dialog that starts the engine names: the exact command, the working folder and the starter path. The command
# is overleaf_engine.COMMAND_TEXT, the same text start_engine's argument list is built from.
ENGINE_RUN_TEMPLATE = "Command: {command}\nWorking folder: {folder}\nStarter: {starter}"
START_CANCELLED_TEXT = "Start cancelled. Nothing was started."
START_WORKING_TEXT = "Starting the LaTeX engine in the background..."
START_RUNNING_TEXT = "The LaTeX engine is running. Press Check to see if you are signed in."
START_TIMEOUT_TEMPLATE = "The LaTeX engine did not answer within {} seconds. Press Check now in a moment."
START_FAILED_TEMPLATE = "The LaTeX engine could not be started ({})."

# The launch-time checks, the engine folder and the two switches on the card. Each is one plain sentence, and none names a
# folder: the folder is never shown, only the words around it.
ENGINE_FOLDER_NOT_SET_TEXT = "Engine folder not set: choose it to start the engine automatically"
# A folder is saved but the owner has not confirmed it in this card (Amendment G, rule 3). The launch start does not run.
ENGINE_FOLDER_NOT_CONFIRMED_TEXT = "Engine folder not confirmed: confirm it in this card before the engine starts at launch"
ENGINE_FOLDER_CONFIRM_TITLE = "Confirm the LaTeX engine folder"
ENGINE_FOLDER_CONFIRM_QUESTION = (
    "Meridian will run this starter for the LaTeX engine:\n\n{starter}\n\n"
    "It runs through wscript.exe //B //nologo, hidden and in the background. Meridian starts it only when you say yes "
    "to Start engine, or at launch when Start the engine when Meridian opens is on.\n\n"
    "Confirm this folder?"
)
ENGINE_FOLDER_CONFIRM_CANCELLED_TEXT = "Engine folder not saved: it was not confirmed. Nothing was saved."
AUTO_START_CONFIRM_TITLE = "Start the LaTeX engine when Meridian opens"
AUTO_START_CONFIRM_QUESTION = (
    "Meridian will start the LaTeX engine when it opens, if the engine is not already running.\n\n{where}\n\n"
    "Turn this on?"
)
AUTO_START_NOT_TURNED_ON_TEXT = "Start at launch stays off. Nothing was saved."
LAUNCH_STARTING_TEXT = "Starting the engine..."
LAUNCH_STARTED_TEXT = "Engine started"
LAUNCH_NOT_STARTED_TEXT = "Engine did not start"
NO_STARTER_CHOSEN_TEXT = "That folder has no start_server_hidden.vbs"
ENGINE_FOLDER_SAVED_TEXT = "Engine folder saved. Nothing was started."
OPTION_NOT_SAVED_TEXT = "Not saved: the settings file could not be written."
ENGINE_FOLDER_DIALOG_TITLE = "Choose the LaTeX engine folder"
CHOOSE_FOLDER_TEXT = "Choose engine folder"
CHOOSE_FOLDER_TOOLTIP = "Pick the folder that holds the engine's starter. Choosing a folder starts nothing."
AUTO_START_LABEL = "Start the engine when Meridian opens"
AUTO_START_TOOLTIP = "When Meridian opens, starts the LaTeX engine if it is down and its folder is chosen and confirmed."
AUTO_CHECK_LABEL = "Check the Overleaf session when Meridian opens"
AUTO_CHECK_TOOLTIP = "When Meridian opens, checks the saved Overleaf session once, if the engine is running."
# The hourly session check (docs/local-trust-contract.md, Amendment I, proposed and not yet accepted by the owner). It is off
# until the owner says yes to the question below. Each check is one request to the engine, and the engine asks Overleaf once.
HOURLY_CHECK_LABEL = "Check the Overleaf session every hour while Home is showing"
HOURLY_CHECK_TOOLTIP = (
    "Asks the LaTeX engine to check the saved Overleaf session once an hour, while Home is showing. Off until you say yes."
)
HOURLY_CHECK_CONFIRM_TITLE = "Check the Overleaf session every hour"
HOURLY_CHECK_CONFIRM_QUESTION = (
    "While the Home tab is showing, Meridian will check your saved Overleaf session once an hour. Each check sends your "
    "saved session to Overleaf once, through the LaTeX engine, to see if you are signed in. The check starts nothing. If the "
    "engine is not running, nothing is sent to Overleaf.\n\n"
    "Overleaf's terms restrict automated access to its service. Turn the hourly check on?"
)
HOURLY_CHECK_NOT_TURNED_ON_TEXT = "The hourly check stays off. Nothing was saved."


@dataclass(frozen=True)
class OverleafFacts:
    """Everything the Overleaf card shows, as HomeTab holds it. ``session`` is the last Check answer, or None before one.
    ``checked`` is the local time of that answer (HH:MM:SS), ``checking`` is True while a session check runs, and
    ``port`` is the engine's port text."""

    engine: str = OVERLEAF_ENGINE_UNCHECKED
    session: overleaf_client.SessionStatus | None = None
    checked: str = ""
    checking: bool = False
    port: str = ""


def _engine_down_view(port: str) -> CardView:
    return CardView(headline=ENGINE_NOT_RUNNING_HEADLINE, detail=ENGINE_DOWN_DETAIL.format(port), tone=TONE_IDLE)


def _session_view(session: overleaf_client.SessionStatus, checked: str, port: str) -> CardView:
    state = session.state
    if state == overleaf_client.STATE_LOGGED_IN:
        headline = f"{SIGNED_IN_HEADLINE} (checked {checked})" if checked else SIGNED_IN_HEADLINE
        return CardView(headline=headline, detail=SIGNED_IN_DETAIL, tone=TONE_OK)
    if state == overleaf_client.STATE_ENGINE_NOT_RUNNING:
        return _engine_down_view(port)
    if state == overleaf_client.STATE_UNKNOWN:
        return CardView(headline=UNREACHABLE_OVERLEAF_HEADLINE, tone=TONE_ATTENTION)
    if state in (overleaf_client.STATE_EXPIRED, overleaf_client.STATE_MISSING):
        # A session that cannot be used because Chrome is missing is named as that, since installing Chrome is the fix.
        if session.chrome_found is False:
            return CardView(headline=NO_CHROME_HEADLINE, tone=TONE_ATTENTION)
        headline = SESSION_EXPIRED_HEADLINE if state == overleaf_client.STATE_EXPIRED else NO_SESSION_HEADLINE
        return CardView(headline=headline, tone=TONE_ATTENTION)
    return CardView(
        headline=COULD_NOT_CHECK_HEADLINE,
        detail=session.error or SESSION_BAD_ANSWER_DETAIL,
        tone=TONE_ATTENTION,
    )


def overleaf_view(facts: OverleafFacts) -> CardView:
    """The Overleaf card's headline, detail and tone. Its buttons and their notes are HomeTab's."""
    if facts.checking or facts.engine == OVERLEAF_ENGINE_UNCHECKED:
        return CardView(headline=CHECKING_HEADLINE, tone=TONE_CHECKING)
    if facts.engine == OVERLEAF_ENGINE_FAILED:
        return CardView(headline=COULD_NOT_CHECK_HEADLINE, detail=ENGINE_FAILED_DETAIL, tone=TONE_ATTENTION)
    if facts.engine == OVERLEAF_ENGINE_DOWN:
        return _engine_down_view(facts.port)
    if facts.session is None:
        return CardView(
            headline=ENGINE_RUNNING_UNCHECKED_HEADLINE,
            detail=OVERLEAF_UNCHECKED_DETAIL,
            tone=TONE_IDLE,
        )
    return _session_view(facts.session, facts.checked, facts.port)


def overleaf_login_text(login: overleaf_client.LoginStatus) -> str:
    """The one line the card shows after Log in, from the engine's answer."""
    if login.started:
        return LOGIN_OPENED_TEXT
    reason = login.reason
    if reason == overleaf_client.REASON_ALREADY_OPEN:
        return LOGIN_ALREADY_OPEN_TEXT
    if reason == overleaf_client.REASON_CHROME_MISSING:
        return LOGIN_NO_CHROME_TEXT
    if reason == overleaf_client.REASON_ENGINE_NOT_RUNNING:
        return LOGIN_ENGINE_DOWN_TEXT
    if reason == overleaf_client.REASON_TIMEOUT:
        return LOGIN_TIMEOUT_TEXT
    return LOGIN_FAILED_TEXT


def overleaf_start_text(running: bool, reason: str, wait_seconds: float) -> str:
    """The one line the card shows after Start engine. ``reason`` is the fixed text of a start that did not happen."""
    if reason:
        return reason
    if running:
        return START_RUNNING_TEXT
    return START_TIMEOUT_TEMPLATE.format(int(wait_seconds))


def launch_start_text(running: bool, reason: str) -> str:
    """The one line the card shows after the launch-time start. ``reason`` is fixed text, set only when the start did not run."""
    if running:
        return LAUNCH_STARTED_TEXT
    return f"{LAUNCH_NOT_STARTED_TEXT}. {reason}" if reason else LAUNCH_NOT_STARTED_TEXT


def engine_folder_question(starter: str) -> str:
    """The confirmation dialog's text for a chosen folder. ``starter`` is the exact start_server_hidden.vbs path it names."""
    return ENGINE_FOLDER_CONFIRM_QUESTION.format(starter=starter)


def engine_run_lines(folder: str) -> str:
    """The exact command, the working folder and the starter path that a start from ``folder`` runs. ``folder`` is the
    owner's own path, shown in a dialog only. It is never empty here: the callers check for a folder first."""
    working = str(Path(folder))
    starter = str(Path(folder) / overleaf_engine.STARTER_NAME)
    return ENGINE_RUN_TEMPLATE.format(command=overleaf_engine.COMMAND_TEXT, folder=working, starter=starter)


def engine_start_question(address: str, folder: str) -> str:
    """The manual Start engine dialog's text: the address, then the command, working folder and starter it will run."""
    return START_QUESTION.format(address=address, run=engine_run_lines(folder))


def auto_start_question(folder: str, confirmed_folder: str) -> str:
    """The confirmation dialog's text for turning the launch-time start on. When a folder is saved, it names the exact command,
    the working folder and the starter, and says what is still missing when the folder is not confirmed. With no folder, it
    names the command and says that nothing starts until a folder is chosen and confirmed. The folder text is the owner's
    own path, in a dialog."""
    if not folder:
        where = (
            "No engine folder is chosen yet, so nothing starts until you choose one and confirm it. "
            "Once you confirm a folder, this command runs at launch, in that folder:\n"
            f"Command: {overleaf_engine.COMMAND_TEXT}"
        )
    else:
        where = engine_run_lines(folder)
        if folder != confirmed_folder:
            where += "\nThis folder is not confirmed yet. Confirm it with Choose engine folder, or nothing starts at launch."
    return AUTO_START_CONFIRM_QUESTION.format(where=where)


__all__ = [
    "ENGINE_NOT_RUNNING_HEADLINE",
    "OVERLEAF_ENGINE_DOWN",
    "OVERLEAF_ENGINE_FAILED",
    "OVERLEAF_ENGINE_RUNNING",
    "OVERLEAF_ENGINE_UNCHECKED",
    "OVERLEAF_TITLE",
    "OverleafFacts",
    "launch_start_text",
    "overleaf_login_text",
    "overleaf_start_text",
    "overleaf_view",
    "ACTION_CHECK_TOOLS",
    "ACTION_LAUNCH_ZOTERO",
    "ACTION_RELEASE_PAGE",
    "ACTION_SETTINGS",
    "CardView",
    "LocalFacts",
    "RowView",
    "ServiceFacts",
    "TONE_ATTENTION",
    "TONE_CHECKING",
    "TONE_IDLE",
    "TONE_OFF",
    "TONE_OK",
    "checked_text",
    "checking_view",
    "failed_view",
    "local_view",
    "services_view",
    "stalled_view",
    "tools_view",
    "tunnel_view",
    "updates_view",
    "zotero_view",
]
