"""Run the Meridian core's client setup command and read what it prints. Pure Python: no Qt import.

The desktop never edits a client's settings file itself. It starts the core's own command in a
subprocess, from the core checkout, with an argument list and no shell, and reads the JSON the
command prints. The command is <core python> -m meridian.client_setup. Plan changes nothing. Apply
and Remove change a file only when they run with --yes, and the core makes a backup first.

The owner ticks the clients to set up (the settings key client_choices). Check, Apply and Remove act
only on the ticked clients: the core gets one --client comma list of the ticked clients it covers, and
nothing is sent for a client that is not ticked. Every outcome is a typed value, so the Settings page
can say what happened in plain words:
  - a core checkout or Python that is not set, or missing, is a refusal and no process starts;
  - a command that fails, times out or cannot start is an error with plain text;
  - a client the core could not set up is a failed row on that client only, with the core's message;
  - only names, paths and actions are read from the output. A file's document or text is never kept,
    and the raw stderr is never shown, because it can hold anything.
The process runner is injected, so tests use a fake and never start the real command.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian_desktop.settings_store import SettingsStore

COMMAND_TIMEOUT_SECONDS = 60
CORE_MODULE = "meridian.client_setup"
MODES = ("apply", "remove")

# The desktop's clients in display order: (id, label). The ids are the core's --client values and the
# names the settings key client_choices keeps.
CLIENTS: tuple[tuple[str, str], ...] = (
    ("claude-desktop", "Claude Desktop"),
    ("codex", "Codex"),
    ("cursor", "Cursor"),
    ("claude-code", "Claude Code"),
)
LABELS = dict(CLIENTS)
# The clients the core's client setup command writes for this desktop. A client outside this set has no command to run here.
CORE_CLIENTS = frozenset({"codex", "claude-desktop", "cursor"})

# Row states, as the Settings page shows them.
IN_PLACE = "in_place"
WOULD_CHANGE = "would_change"
MISSING = "missing"
NOT_READABLE = "not_readable"
NOT_COVERED = "not_covered"
NOT_CHECKED = "not_checked"
NOT_SELECTED = "not_selected"
STATE_TEXT = {
    IN_PLACE: "In place",
    WOULD_CHANGE: "Would change",
    MISSING: "Missing",
    NOT_READABLE: "Not readable",
    NOT_COVERED: "Not covered by the Meridian core",
    NOT_CHECKED: "Not checked yet",
    NOT_SELECTED: "Not selected",
}

# Kinds of command result, so a caller can branch on the kind and not on the text.
KIND_OK = "ok"
KIND_NOT_CONFIGURED = "not_configured"
KIND_NOT_COVERED = "not_covered"
KIND_NOTHING_TICKED = "nothing_ticked"
KIND_TIMEOUT = "timeout"
KIND_CANNOT_START = "cannot_start"
KIND_CORE_ERROR = "core_error"
KIND_FAILED = "failed"
KIND_BAD_OUTPUT = "bad_output"

# The actions the core reports, for a dry run and for a real change. A client the core could not set up reports FAILED_ACTION
# with a message, and that result is shown on that client's row only. A client whose settings folder is missing reports
# NOT_INSTALLED_ACTION: the core writes nothing for it, the other clients in the same command still run, and the row says so.
FAILED_ACTION = "failed"
NOT_INSTALLED_ACTION = "not_installed"
PREVIEW_ACTIONS = frozenset({"unchanged", "would_write", "would_remove", NOT_INSTALLED_ACTION})
CHANGE_ACTIONS = frozenset({"unchanged", "written", "removed", NOT_INSTALLED_ACTION})

NOT_SET_CHECKOUT_TEXT = (
    "Meridian core checkout is not set. Press Find it again, or Choose the Meridian core folder, "
    "and the Python is found after that."
)
NOT_SET_PYTHON_TEXT = (
    "Python for Meridian core is not set. Press Find it again, or pick the python.exe with Browse and Save."
)
CHECKOUT_MISSING_TEXT = (
    "The Meridian core checkout folder does not exist: {path}. In Settings, choose the folder again "
    "and save it."
)
CHECKOUT_NOT_CORE_TEXT = (
    "{path} is not a Meridian core checkout: it has no meridian package. In Settings, choose the "
    "folder that holds meridian/ and save it."
)
PYTHON_MISSING_TEXT = (
    "The Python program for Meridian core does not exist: {path}. In Settings, choose the python.exe "
    "again and save it."
)
TIMEOUT_TEXT = (
    "The Meridian setup command did not finish within {seconds} seconds. Check that the Python for "
    "Meridian core runs, then press Check setup again."
)
CANNOT_START_TEXT = (
    "The Python for Meridian core could not be started ({name}). In Settings, choose a python.exe "
    "that runs."
)
FAILED_TEXT = (
    "The Meridian setup command failed with exit code {code}. Check the Meridian core checkout and "
    "Python, then try again."
)
BAD_OUTPUT_TEXT = (
    "The Meridian setup command printed output this desktop does not understand. Update the Meridian "
    "core checkout, then try again."
)
NOT_COVERED_TEXT = (
    "The Meridian core's client setup does not cover {label} yet, so this desktop cannot set it up. "
    "Update the Meridian core checkout to a version that covers it, then press Check setup."
)
NOT_COVERED_ACTION_TEXT = (
    "{label} is not covered by the Meridian core's client setup, so nothing is changed for it."
)
NONE_COVERED_ACTION_TEXT = (
    "None of the ticked clients is covered by the Meridian core's client setup, so nothing is changed."
)
NONE_COVERED_CHECK_TEXT = (
    "None of the ticked clients is covered by the Meridian core's client setup yet. Nothing was read."
)
NOT_SELECTED_TEXT = "Not ticked. Tick the box to include it in Check setup, Apply and Remove."
NOTHING_TICKED_TEXT = "No client is ticked. Tick the clients to set up, then press Check setup, Apply or Remove."
NO_RESULT_TEXT = "The Meridian core did not report on this client. Press Check setup again."
FAILED_CLIENT_TEXT = "The Meridian core could not set up this client. Check the core checkout and Python, then try again."
NOT_INSTALLED_TEXT = (
    "{label} is not set up on this computer: its settings folder does not exist, so nothing was written for it. "
    "Install the app, then press Check setup."
)
UNKNOWN_ACTION_TEXT = "The Meridian core reported an action this desktop does not know ({action})."
CHECKED_TEXT = "Checked. Nothing was written."
PARTIAL_TEXT = "The Meridian core could not read every client. The rows say why."
BAD_MODE_TEXT = "Unknown setup action."


@dataclass(frozen=True)
class CommandOutput:
    """What a finished process printed. The runner reads only these three fields."""

    returncode: int
    stdout: str
    stderr: str


# The process runner: (argv, working directory, timeout in seconds) -> CommandOutput. It raises
# subprocess.TimeoutExpired on a timeout and OSError when the program cannot start.
CommandRunner = Callable[[Sequence[str], Path, int], CommandOutput]


def run_process(argv: Sequence[str], cwd: Path, timeout: int) -> CommandOutput:
    """Run argv with no shell, from cwd, and wait at most timeout seconds."""
    extra: dict[str, object] = {}
    if sys.platform == "win32":
        # A GUI app must not open a console window for the child process.
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        **extra,
    )
    return CommandOutput(completed.returncode, completed.stdout or "", completed.stderr or "")


@dataclass(frozen=True)
class ClientChange:
    """One client's result from the core: its file, the action, and the names of the entries.

    For a client the core could not set up, action is FAILED_ACTION and message holds the core's words for it.
    """

    client: str
    path: str
    action: str
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    backup: str | None = None
    restart_required: bool = False
    message: str = ""


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one command. ok is True only when the core ran and its output was read.

    When ok is True, changes holds one entry per client the command asked for, and a client the core could not set
    up has FAILED_ACTION. When ok is False, message applies to every client the command asked for.
    """

    ok: bool
    kind: str
    message: str = ""
    changes: tuple[ClientChange, ...] = ()


@dataclass(frozen=True)
class ClientRow:
    """One client's row on the Settings page."""

    client: str
    label: str
    state: str
    path: str = ""
    detail: str = ""

    @property
    def covered(self) -> bool:
        return self.client in CORE_CLIENTS


@dataclass(frozen=True)
class CheckResult:
    """The whole check: one row per client, and a plain-words message about the check as a whole."""

    rows: tuple[ClientRow, ...]
    message: str = ""
    configured: bool = True


class _NotReady(Exception):
    """The core checkout or Python is not set, or is missing. Carries the plain-words reason."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def ticked_clients(choices: Iterable[str]) -> tuple[str, ...]:
    """The ticked clients in display order. A name that is not a desktop client is dropped."""
    ticked = frozenset(choices)
    return tuple(client for client, _label in CLIENTS if client in ticked)


def covered_ticked(choices: Iterable[str]) -> tuple[str, ...]:
    """The ticked clients the core covers, in display order. These are the only ones sent to the core."""
    return tuple(client for client in ticked_clients(choices) if client in CORE_CLIENTS)


def names_text(change: ClientChange) -> str:
    """'Adds: a, b. Changes: c. Removes: d.' Names only. Says so plainly when nothing changes."""
    parts = []
    if change.added:
        parts.append("Adds: " + ", ".join(change.added) + ".")
    if change.changed:
        parts.append("Changes: " + ", ".join(change.changed) + ".")
    if change.removed:
        parts.append("Removes: " + ", ".join(change.removed) + ".")
    return " ".join(parts) if parts else "No Meridian entry changes."


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def parse_changes(stdout: str) -> tuple[ClientChange, ...]:
    """Read the core's JSON result. Raises ValueError when the shape is not what the core prints."""
    data = json.loads(stdout)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("no results list")
    changes: list[ClientChange] = []
    for item in data["results"]:
        if not isinstance(item, dict):
            raise ValueError("a result is not an object")
        client, path, action = item.get("client"), item.get("path"), item.get("action")
        if not all(isinstance(value, str) for value in (client, path, action)):
            raise ValueError("a result is missing client, path or action")
        names = item.get("changes") if isinstance(item.get("changes"), dict) else {}
        backup = item.get("backup")
        if backup is not None and not isinstance(backup, str):
            raise ValueError("backup is not text")
        message = item.get("message")
        changes.append(
            ClientChange(
                client=client,
                path=path,
                action=action,
                added=_names(names.get("added")),
                changed=_names(names.get("changed")),
                removed=_names(names.get("removed")),
                backup=backup,
                restart_required=item.get("restart_required") is True,
                message=message if isinstance(message, str) else "",
            )
        )
    return tuple(changes)


def _core_error(stderr: str) -> str | None:
    """The core's own {"error": "..."} message, or None when stderr is not that shape."""
    try:
        data = json.loads(stderr)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"].strip():
        return data["error"].strip()
    return None


def _argv(python: str, mode: str, clients: Sequence[str], *, write: bool) -> list[str]:
    argv = [python, "-m", CORE_MODULE, mode, "--client", ",".join(clients)]
    if write:
        argv.append("--yes")
    return argv


def _plan_argv(python: str, clients: Sequence[str]) -> list[str]:
    return [python, "-m", CORE_MODULE, "plan", "--client", ",".join(clients)]


def _not_covered_row(client: str, label: str) -> ClientRow:
    return ClientRow(client, label, NOT_COVERED, detail=NOT_COVERED_TEXT.format(label=label))


def _not_selected_row(client: str, label: str) -> ClientRow:
    return ClientRow(client, label, NOT_SELECTED, detail=NOT_SELECTED_TEXT)


def unchecked_rows(choices: Iterable[str] | None = None) -> tuple[ClientRow, ...]:
    """One row per client before any check.

    With choices None every client is shown with no tick state. With the owner's ticked clients, a client that is
    not ticked shows as Not selected, and a ticked client the core does not cover shows as not covered.
    """
    ticked = None if choices is None else frozenset(choices)
    rows: list[ClientRow] = []
    for client, label in CLIENTS:
        if ticked is not None and client not in ticked:
            rows.append(_not_selected_row(client, label))
        elif client not in CORE_CLIENTS:
            rows.append(_not_covered_row(client, label))
        else:
            rows.append(ClientRow(client, label, NOT_CHECKED))
    return tuple(rows)


def row_from_change(change: ClientChange, label: str) -> ClientRow:
    """Turn one core result into a row. Missing is decided by whether the file exists now."""
    if change.action == FAILED_ACTION:
        return ClientRow(
            change.client,
            label,
            NOT_READABLE,
            path=change.path,
            detail=change.message or FAILED_CLIENT_TEXT,
        )
    if change.action == "unchanged":
        return ClientRow(change.client, label, IN_PLACE, path=change.path)
    if change.action == NOT_INSTALLED_ACTION:
        # The core wrote nothing: the client's folder is missing, so the app is not here. Missing says so on the row.
        return ClientRow(change.client, label, MISSING, path=change.path, detail=NOT_INSTALLED_TEXT.format(label=label))
    if change.action == "would_write":
        state = WOULD_CHANGE if Path(change.path).exists() else MISSING
        return ClientRow(change.client, label, state, path=change.path, detail=names_text(change))
    return ClientRow(
        change.client,
        label,
        NOT_READABLE,
        path=change.path,
        detail=UNKNOWN_ACTION_TEXT.format(action=change.action),
    )


def _nothing_to_plan_text(ticked: Sequence[str]) -> str:
    return NOTHING_TICKED_TEXT if not ticked else NONE_COVERED_CHECK_TEXT


class ClientSetupRunner:
    """Check, preview and change the clients' setup through the core command. The run function is injected."""

    def __init__(self, settings_store: SettingsStore, run: CommandRunner | None = None) -> None:
        self._store = settings_store
        self._run = run if run is not None else run_process

    # ----------------------------------------------------------- core location

    def _location(self) -> tuple[Path, str]:
        """The core checkout and Python, checked. Raises _NotReady with plain words when either is not usable."""
        settings = self._store.load().settings
        checkout = settings.core_checkout.strip()
        python = settings.core_python.strip()
        if not checkout:
            raise _NotReady(NOT_SET_CHECKOUT_TEXT)
        if not python:
            raise _NotReady(NOT_SET_PYTHON_TEXT)
        checkout_path = Path(checkout)
        if not checkout_path.is_dir():
            raise _NotReady(CHECKOUT_MISSING_TEXT.format(path=checkout))
        if not (checkout_path / "meridian" / "__init__.py").is_file():
            raise _NotReady(CHECKOUT_NOT_CORE_TEXT.format(path=checkout))
        if not Path(python).is_file():
            raise _NotReady(PYTHON_MISSING_TEXT.format(path=python))
        return checkout_path, python

    # ----------------------------------------------------------- running

    def _invoke(self, argv: list[str], cwd: Path) -> CommandResult:
        """Run one command and read its result. Every failure becomes a typed result, never a raise."""
        try:
            output = self._run(list(argv), cwd, COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return CommandResult(False, KIND_TIMEOUT, TIMEOUT_TEXT.format(seconds=COMMAND_TIMEOUT_SECONDS))
        except OSError as exc:
            return CommandResult(False, KIND_CANNOT_START, CANNOT_START_TEXT.format(name=exc.__class__.__name__))
        if output.returncode != 0:
            # The core prints one result for every client it was asked about, even when one of them failed, and then
            # exits 1. Those results are read, so a failure stays on its own client's row.
            try:
                changes = parse_changes(output.stdout)
            except ValueError:
                changes = ()
            if changes:
                return CommandResult(True, KIND_OK, changes=changes)
            core_message = _core_error(output.stderr)
            if core_message is not None:
                return CommandResult(False, KIND_CORE_ERROR, core_message)
            return CommandResult(False, KIND_FAILED, FAILED_TEXT.format(code=output.returncode))
        try:
            changes = parse_changes(output.stdout)
        except ValueError:
            return CommandResult(False, KIND_BAD_OUTPUT, BAD_OUTPUT_TEXT)
        return CommandResult(True, KIND_OK, changes=changes)

    # ----------------------------------------------------------- check

    def check(self, clients: Iterable[str] | None = None) -> CheckResult:
        """Plan for the ticked clients the core covers. Changes nothing on disk.

        A list is the clients to plan. clients None means the owner's saved ticks (client_choices), so a caller that
        passes no list, such as the Home tools card, reads only what the owner ticked. The core is never asked about a
        client that is not ticked, and a plan with nothing to ask sends nothing to the core.
        """
        ticked = ticked_clients(self._store.load().settings.client_choices if clients is None else clients)
        try:
            checkout, python = self._location()
        except _NotReady as exc:
            return CheckResult(rows=unchecked_rows(ticked), message=exc.message, configured=False)
        planned = covered_ticked(ticked)
        if not planned:
            return CheckResult(rows=unchecked_rows(ticked), message=_nothing_to_plan_text(ticked))
        result = self._invoke(_plan_argv(python, planned), checkout)
        if result.ok:
            return self._rows_from_changes(result.changes, ticked)
        if result.kind == KIND_CORE_ERROR:
            return self._check_one_client_at_a_time(checkout, python, ticked)
        return CheckResult(rows=unchecked_rows(ticked), message=result.message)

    def _rows_from_changes(self, changes: tuple[ClientChange, ...], ticked: tuple[str, ...]) -> CheckResult:
        by_client = {change.client: change for change in changes}
        rows: list[ClientRow] = []
        for client, label in CLIENTS:
            if client not in ticked:
                rows.append(_not_selected_row(client, label))
            elif client not in CORE_CLIENTS:
                rows.append(_not_covered_row(client, label))
            elif client in by_client:
                rows.append(row_from_change(by_client[client], label))
            else:
                rows.append(ClientRow(client, label, NOT_CHECKED, detail=NO_RESULT_TEXT))
        return CheckResult(rows=tuple(rows), message=CHECKED_TEXT)

    def _check_one_client_at_a_time(self, checkout: Path, python: str, ticked: tuple[str, ...]) -> CheckResult:
        """Used when 'plan' fails with the core's own error. One unreadable file must not hide the others.

        Each ticked core client is planned on its own. A client that still fails gets a 'not readable' row
        that carries the core's message. Claude Code has no command here, so its row is never planned.
        """
        rows: list[ClientRow] = []
        for client, label in CLIENTS:
            if client not in ticked:
                rows.append(_not_selected_row(client, label))
                continue
            if client not in CORE_CLIENTS:
                rows.append(_not_covered_row(client, label))
                continue
            result = self._invoke(_plan_argv(python, (client,)), checkout)
            match = next((change for change in result.changes if change.client == client), None)
            if result.ok and match is not None:
                rows.append(row_from_change(match, label))
            else:
                rows.append(ClientRow(client, label, NOT_READABLE, detail=result.message or NO_RESULT_TEXT))
        return CheckResult(rows=tuple(rows), message=PARTIAL_TEXT)

    # ----------------------------------------------------------- preview and change

    def describe_command(self, clients: Sequence[str], mode: str) -> str:
        """The command a Yes would run, for the confirmation dialog. It is text only and runs nothing."""
        try:
            checkout, python = self._location()
        except _NotReady as exc:
            return exc.message
        command = subprocess.list2cmdline(_argv(python, mode, covered_ticked(clients), write=True))
        return f"{command}  (run in {checkout})"

    def preview(self, clients: Sequence[str], mode: str) -> CommandResult:
        """A dry run of apply or remove for the ticked clients. It writes nothing."""
        return self._act(clients, mode, write=False)

    def run_change(self, clients: Sequence[str], mode: str) -> CommandResult:
        """Apply or remove the ticked clients, with --yes. The core makes a backup first. Call only after a yes."""
        return self._act(clients, mode, write=True)

    def _act(self, clients: Sequence[str], mode: str, *, write: bool) -> CommandResult:
        if mode not in MODES:
            return CommandResult(False, KIND_BAD_OUTPUT, BAD_MODE_TEXT)
        ticked = ticked_clients(clients)
        if not ticked:
            return CommandResult(False, KIND_NOTHING_TICKED, NOTHING_TICKED_TEXT)
        targets = covered_ticked(ticked)
        if not targets:
            if len(ticked) == 1:
                message = NOT_COVERED_ACTION_TEXT.format(label=LABELS[ticked[0]])
            else:
                message = NONE_COVERED_ACTION_TEXT
            return CommandResult(False, KIND_NOT_COVERED, message)
        try:
            checkout, python = self._location()
        except _NotReady as exc:
            return CommandResult(False, KIND_NOT_CONFIGURED, exc.message)
        result = self._invoke(_argv(python, mode, targets, write=write), checkout)
        if not result.ok:
            return result
        allowed = CHANGE_ACTIONS if write else PREVIEW_ACTIONS
        reported = [change.client for change in result.changes]
        if len(reported) != len(set(reported)) or set(reported) != set(targets):
            return CommandResult(False, KIND_BAD_OUTPUT, BAD_OUTPUT_TEXT)
        if any(change.action not in allowed and change.action != FAILED_ACTION for change in result.changes):
            return CommandResult(False, KIND_BAD_OUTPUT, BAD_OUTPUT_TEXT)
        return result


__all__ = [
    "CLIENTS",
    "CORE_CLIENTS",
    "CORE_MODULE",
    "COMMAND_TIMEOUT_SECONDS",
    "CheckResult",
    "ClientChange",
    "ClientRow",
    "ClientSetupRunner",
    "CommandOutput",
    "CommandResult",
    "IN_PLACE",
    "MISSING",
    "NOT_CHECKED",
    "NOT_COVERED",
    "NOT_READABLE",
    "NOT_SELECTED",
    "STATE_TEXT",
    "WOULD_CHANGE",
    "covered_ticked",
    "names_text",
    "parse_changes",
    "run_process",
    "ticked_clients",
    "unchecked_rows",
]
